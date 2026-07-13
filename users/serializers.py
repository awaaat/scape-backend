from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.utils import timezone
from rest_framework import serializers

from .models import UserSignup
from .phone_utils import normalize_kenyan_phone, InvalidKenyanPhone


class UserSignupCreateSerializer(serializers.ModelSerializer):
    """
    Inbound signup form. consent_given must be explicitly true — same
    "no silent opt-in" rule as jobs.JobApplicationSerializer.
    """

    consent_given = serializers.BooleanField(write_only=True)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    class Meta:
        model = UserSignup
        fields = ["full_name", "email", "phone", "consent_given", "privacy_policy_version", "password"]

    def validate_consent_given(self, value):
        if not value:
            raise serializers.ValidationError("Consent is required to sign up.")
        return value

    def validate_password(self, value):
        django_validate_password(value)
        return value

    def validate_email(self, value):
        User = get_user_model()
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        try:
            normalized = normalize_kenyan_phone(value)
        except InvalidKenyanPhone as exc:
            raise serializers.ValidationError(str(exc)) from exc

        if UserSignup.objects.filter(phone=normalized).exists():
            raise serializers.ValidationError(
                "An account with this phone number already exists."
            )
        return normalized

    def validate_full_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def create(self, validated_data):
        request = self.context.get("request")
        password = validated_data.pop("password")
        validated_data["consent_given_at"] = timezone.now()

        if request is not None:
            validated_data["ip_address"] = request.META.get("REMOTE_ADDR")
            validated_data["user_agent"] = request.META.get("HTTP_USER_AGENT", "")
            visitor = getattr(request, "visitor", None)  # set by visitors/middleware.py
            if visitor is not None:
                validated_data["visitor"] = visitor

        User = get_user_model()
        auth_user = User.objects.create_user(
            username=validated_data["email"],
            email=validated_data["email"],
            password=password,
            first_name=validated_data["full_name"].split(" ")[0],
        )
        validated_data["user"] = auth_user

        return super().create(validated_data)


class UserSignupReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserSignup
        fields = [
            "id",
            "full_name",
            "email",
            "phone",
            "email_verified",
            "created_at",
        ]
        read_only_fields = fields


class EmailVerificationRequestSerializer(serializers.Serializer):
    """POST body for re-sending a verification email."""

    email = serializers.EmailField()


class EmailVerificationConfirmSerializer(serializers.Serializer):
    """POST body for confirming a verification link click."""

    id = serializers.UUIDField()
    token = serializers.CharField(max_length=64)


class LoginSerializer(serializers.Serializer):
    """POST body for /api/users/login/."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class ChangePasswordSerializer(serializers.Serializer):
    """POST body for /api/users/change-password/."""
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_new_password(self, value):
        django_validate_password(value)
        return value
