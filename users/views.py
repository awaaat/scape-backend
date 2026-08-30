from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import get_object_or_404
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from . import storage
from .emails import send_password_reset_email, send_verification_email, send_welcome_email
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


class SignupView(APIView):
    """POST /api/users/signup/ — create a signup record and email a
    verification link. Mirrors property_intel's pin-submission view:
    validate → persist → fire the side effect → return a read serializer."""

    def post(self, request):
        serializer = UserSignupCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        signup = serializer.save()

        raw_token = signup.generate_verification_token()
        send_verification_email(signup, raw_token)

        return Response(UserSignupReadSerializer(signup).data, status=status.HTTP_201_CREATED)


class ResendVerificationView(APIView):
    """POST /api/users/verify-email/resend/ — issues a fresh token and
    re-sends. Doesn't reveal whether the email exists in the system
    (always 202), same information-disclosure posture as OTP resend
    in property_intel."""

    def post(self, request):
        serializer = EmailVerificationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        try:
            signup = UserSignup.objects.get(email__iexact=email, is_active=True)
        except UserSignup.DoesNotExist:
            return Response(status=status.HTTP_202_ACCEPTED)

        if not signup.email_verified:
            raw_token = signup.generate_verification_token()
            send_verification_email(signup, raw_token)

        return Response(status=status.HTTP_202_ACCEPTED)


class VerifyEmailView(APIView):
    """POST /api/users/verify-email/confirm/ — the link the person clicks
    lands on a frontend page that then POSTs {id, token} here."""

    def post(self, request):
        serializer = EmailVerificationConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        signup = get_object_or_404(UserSignup, id=serializer.validated_data["id"])

        if not signup.verify_token(serializer.validated_data["token"]):
            return Response(
                {"detail": "This verification link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        signup.mark_verified()
        send_welcome_email(signup)
        return Response(UserSignupReadSerializer(signup).data, status=status.HTTP_200_OK)


class LoginView(APIView):
    """POST /api/users/login/ — email + password, returns a JWT pair plus
    the caller's profile. Mirrors SignupView's response shape."""

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_active:
            return Response(
                {"detail": "Invalid email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        signup = getattr(user, "signup_profile", None)
        refresh = RefreshToken.for_user(user)

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSignupReadSerializer(signup).data if signup else None,
        })


class LogoutView(APIView):
    """POST /api/users/logout/ — blacklists the given refresh token so it
    can't be used again. Access tokens just expire naturally (30 min)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            RefreshToken(request.data.get("refresh")).blacklist()
        except Exception:
            pass
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """GET /api/users/me/ — requires a valid access token.
    PATCH /api/users/me/ — edit full_name/phone (email is intentionally
    not editable here; changing it would need re-verification, out of
    scope for this pass)."""

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

        # first_name on the auth user backs Overview's greeting — keep it
        # in sync so "Hi, {first name}" doesn't go stale after a rename.
        if "full_name" in serializer.validated_data:
            request.user.first_name = serializer.validated_data["full_name"].split(" ")[0]
            request.user.save(update_fields=["first_name"])

        return Response(UserSignupReadSerializer(signup).data)


class AvatarUploadView(APIView):
    """POST /api/users/me/avatar/ — multipart, field name "avatar".
    Uploads via users/storage.py (Supabase), overwriting any existing one."""

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
            return Response({"detail": "Couldn't upload that image right now — try again shortly."}, status=status.HTTP_502_BAD_GATEWAY)

        signup.avatar_url = avatar_url
        signup.save(update_fields=["avatar_url", "updated_at"])
        return Response(UserSignupReadSerializer(signup).data)


class AccountDeleteView(APIView):
    """POST /api/users/me/delete/ — body: {password}. Soft-delete: the
    auth user is deactivated (can never log in again) and the profile
    is deactivated + anonymized, rather than hard-deleted — same
    "untick is_active rather than destroy the row" reasoning as
    UserSignup.is_active's own help_text, and it keeps FK integrity
    intact for anything referencing this user (contracts.client_user
    etc. are all on_delete=SET_NULL, but a hard delete would still
    orphan historical records unnecessarily)."""

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
    """POST /api/users/password-reset/request/ — emails a reset link if
    the address belongs to an active account. Always 202 regardless of
    outcome — same "don't reveal whether the email exists" posture as
    ResendVerificationView."""

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        User = get_user_model()
        try:
            user = User.objects.get(username__iexact=email, is_active=True)
        except User.DoesNotExist:
            return Response(status=status.HTTP_202_ACCEPTED)

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        raw_token = default_token_generator.make_token(user)
        send_password_reset_email(user, uid, raw_token)

        return Response(status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(APIView):
    """POST /api/users/password-reset/confirm/ — the link the person
    clicks lands on a frontend page that then POSTs {uid, token,
    new_password} here. Uses Django's own default_token_generator, so
    the token is single-use in effect: it's derived from the user's
    current password hash + last login, so it stops validating the
    instant the password actually changes."""

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        User = get_user_model()
        try:
            uid = force_str(urlsafe_base64_decode(serializer.validated_data["uid"]))
            user = User.objects.get(pk=uid, is_active=True)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response(
                {"detail": "This reset link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not default_token_generator.check_token(user, serializer.validated_data["token"]):
            return Response(
                {"detail": "This reset link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password updated."}, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    """POST /api/users/change-password/ — requires current password to
    confirm identity before setting a new one."""

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