from django.contrib.auth import authenticate
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import send_verification_email, send_welcome_email
from .models import UserSignup
from .serializers import (
    ChangePasswordSerializer,
    EmailVerificationConfirmSerializer,
    EmailVerificationRequestSerializer,
    LoginSerializer,
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
    """GET /api/users/me/ — requires a valid access token."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        signup = getattr(request.user, "signup_profile", None)
        if signup is None:
            return Response({"detail": "No profile found for this account."}, status=status.HTTP_404_NOT_FOUND)
        return Response(UserSignupReadSerializer(signup).data)


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
