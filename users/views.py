import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from . import storage
from .app_registry import build_url, detect_source, resolve_source
from .emails import send_password_reset_email, send_verification_email, send_welcome_email
from .memberships import record_app_membership
from .models import UserSignup
from .serializers import (
    ChangePasswordSerializer,
    DeleteAccountSerializer,
    EmailVerificationConfirmSerializer,
    EmailVerificationRequestSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ProfileUpdateSerializer,
    UserSignupCreateSerializer,
    UserSignupReadSerializer,
)

logger = logging.getLogger(__name__)

ACCOUNT_EXISTS_MESSAGE = (
    "You already have a Scape account. Sign in with the same email and password - "
    "one account works across all Scape apps."
)


def _cooldown_ok(scope, key):
    """
    True if this action may proceed; False if it was already done within the cooldown.
    Stops someone using resend/reset as an email-bomb. Fails OPEN if the cache is
    down - a cache outage must never block real users from getting their email.
    """
    seconds = getattr(settings, "SCAPE_RESEND_COOLDOWN_SECONDS", 60)
    if not seconds or seconds <= 0:
        return True
    try:
        return bool(cache.add(f"users:cooldown:{scope}:{key}", 1, seconds))
    except Exception:
        logger.warning("Cooldown cache unavailable (%s) - allowing", scope, exc_info=True)
        return True


class SignupView(APIView):
    """POST /api/users/signup/ - create the account, record which app it came
    from, and email a verification link that returns them to THAT app."""

    def post(self, request):
        serializer = UserSignupCreateSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            body = dict(serializer.errors)
            email_errors = serializer.errors.get("email") or []
            if any(getattr(e, "code", None) == "account_exists" for e in email_errors):
                body["code"] = "account_exists"
                body["detail"] = ACCOUNT_EXISTS_MESSAGE
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        try:
            signup = serializer.save()
        except IntegrityError:
            # Lost a uniqueness race between validation and insert. Friendly 400, not a 500.
            return Response(
                {"detail": ACCOUNT_EXISTS_MESSAGE, "code": "account_exists"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_token = signup.generate_verification_token()
        _cooldown_ok("verify", signup.pk)  # starts the resend cooldown
        # on_commit: if an outer transaction rolls back, no email goes out for a ghost account.
        transaction.on_commit(lambda: send_verification_email(signup, raw_token))

        return Response(UserSignupReadSerializer(signup).data, status=status.HTTP_201_CREATED)


class ResendVerificationView(APIView):
    """POST /api/users/verify-email/resend/ - always 202 (never reveals whether
    the email exists). The new link goes to the app the person is using NOW."""

    def post(self, request):
        serializer = EmailVerificationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        signup = UserSignup.objects.filter(email__iexact=email, is_active=True).first()
        if signup is None or signup.email_verified:
            return Response(status=status.HTTP_202_ACCEPTED)

        if not _cooldown_ok("verify", signup.pk):
            return Response(status=status.HTTP_202_ACCEPTED)

        source = resolve_source(request, serializer.validated_data.get("source"), fallback=signup.signup_source)
        raw_token = signup.generate_verification_token()
        send_verification_email(signup, raw_token, source=source)
        return Response(status=status.HTTP_202_ACCEPTED)


class VerifyEmailView(APIView):
    """POST /api/users/verify-email/confirm/ - the front-end page behind the
    emailed link POSTs {id, token} here.

    Idempotent and race-safe: double-clicks, link prefetchers, or two tabs can
    never send the welcome email twice. Wrong id / bad token / deactivated
    account all return the same generic 400 (no account enumeration)."""

    def post(self, request):
        serializer = EmailVerificationConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        invalid = Response(
            {"detail": "This verification link is invalid or has expired."},
            status=status.HTTP_400_BAD_REQUEST,
        )

        with transaction.atomic():
            signup = (
                UserSignup.objects.select_for_update()
                .filter(id=serializer.validated_data["id"], is_active=True)
                .first()
            )
            if signup is None or not signup.verify_token(serializer.validated_data["token"]):
                return invalid
            newly_verified = not signup.email_verified
            if newly_verified:
                signup.mark_verified()

        if newly_verified:
            transaction.on_commit(lambda: send_welcome_email(signup))

        data = dict(UserSignupReadSerializer(signup).data)
        data["login_url"] = build_url(signup.signup_source, "login")
        return Response(data, status=status.HTTP_200_OK)


class LoginView(APIView):
    """POST /api/users/login/ - email + password -> JWT pair + profile. An
    account created in ANY Scape app can sign in to any other; the app the
    login came from is recorded (optional `source`, else Origin/Referer)."""

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_active:
            return Response({"detail": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)

        source = detect_source(request, serializer.validated_data.get("source"))
        if source:
            record_app_membership(user, source)

        signup = getattr(user, "signup_profile", None)
        refresh = RefreshToken.for_user(user)

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSignupReadSerializer(signup).data if signup else None,
        })


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            RefreshToken(request.data.get("refresh")).blacklist()
        except Exception:
            pass
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        signup = getattr(request.user, "signup_profile", None)
        if signup is None:
            return Response({"detail": "No profile found for this account."}, status=status.HTTP_404_NOT_FOUND)
        return Response(UserSignupReadSerializer(signup).data)

    def patch(self, request):
        signup = getattr(request.user, "signup_profile", None)
        if signup is None:
            return Response({"detail": "No profile found for this account."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ProfileUpdateSerializer(instance=signup, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        for field, value in serializer.validated_data.items():
            setattr(signup, field, value)
        signup.save(update_fields=list(serializer.validated_data.keys()) + ["updated_at"])

        if "full_name" in serializer.validated_data:
            request.user.first_name = serializer.validated_data["full_name"].split(" ")[0]
            request.user.save(update_fields=["first_name"])

        return Response(UserSignupReadSerializer(signup).data)


class AvatarUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        signup = getattr(request.user, "signup_profile", None)
        if signup is None:
            return Response({"detail": "No profile found for this account."}, status=status.HTTP_404_NOT_FOUND)

        uploaded_file = request.FILES.get("avatar")
        if uploaded_file is None:
            return Response({"detail": "No file provided."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            avatar_url = storage.upload_avatar(uploaded_file, signup.user_id)
        except storage.InvalidAvatarUpload as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except storage.StorageUploadFailed:
            return Response(
                {"detail": "Couldn't upload that image right now — try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        signup.avatar_url = avatar_url
        signup.save(update_fields=["avatar_url", "updated_at"])
        return Response(UserSignupReadSerializer(signup).data)


class AccountDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeleteAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        if not user.check_password(serializer.validated_data["password"]):
            return Response({"detail": "Incorrect password."}, status=status.HTTP_400_BAD_REQUEST)

        signup = getattr(user, "signup_profile", None)
        if signup is not None:
            signup.is_active = False
            signup.save(update_fields=["is_active", "updated_at"])

        user.is_active = False
        user.save(update_fields=["is_active"])

        return Response(status=status.HTTP_205_RESET_CONTENT)


class PasswordResetRequestView(APIView):
    """POST /api/users/password-reset/request/ - always 202. The reset link
    goes to the app the request came from (else the account's own app)."""

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        User = get_user_model()
        user = User.objects.filter(username__iexact=email, is_active=True).first()
        if user is None:
            return Response(status=status.HTTP_202_ACCEPTED)

        if not _cooldown_ok("reset", user.pk):
            return Response(status=status.HTTP_202_ACCEPTED)

        signup = getattr(user, "signup_profile", None)
        source = resolve_source(
            request, serializer.validated_data.get("source"),
            fallback=getattr(signup, "signup_source", None),
        )
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        raw_token = default_token_generator.make_token(user)
        send_password_reset_email(user, uid, raw_token, source=source)

        return Response(status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(APIView):
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        User = get_user_model()
        try:
            uid = force_str(urlsafe_base64_decode(serializer.validated_data["uid"]))
            user = User.objects.get(pk=uid, is_active=True)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response({"detail": "This reset link is invalid or has expired."}, status=status.HTTP_400_BAD_REQUEST)

        if not default_token_generator.check_token(user, serializer.validated_data["token"]):
            return Response({"detail": "This reset link is invalid or has expired."}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password updated."}, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            return Response({"detail": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password updated."}, status=status.HTTP_200_OK)
