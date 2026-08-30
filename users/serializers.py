from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.utils import timezone
from rest_framework import serializers

from .models import UserSignup, KENYA_COUNTRY_CODE
from .phone_utils import normalize_kenyan_phone, InvalidKenyanPhone


class UserSignupCreateSerializer(serializers.ModelSerializer):
    """
    Inbound signup form. consent_given must be explicitly true — same
    "no silent opt-in" rule as jobs.JobApplicationSerializer.

    `country` drives whether `phone` is required/validated as a Kenyan
    number (see validate()) and, downstream, which currency this user's
    wallet operates in (payments.UserWallet) — see payments/views.py.
    """

    consent_given = serializers.BooleanField(write_only=True)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    # Not required/not validated-as-Kenyan at the field level on purpose —
    # whether it's required at all depends on `country`, decided below in
    # validate() once both fields are available together.
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True, default="")
    country = serializers.CharField(max_length=2)

    class Meta:
        model = UserSignup
        fields = ["full_name", "email", "phone", "country", "consent_given", "privacy_policy_version", "password"]

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

    def validate_country(self, value):
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise serializers.ValidationError("Enter a valid two-letter country code, e.g. KE or US.")
        return value

    def validate_full_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate(self, attrs):
        """
        Phone is required and validated as a Kenyan number only for
        country="KE". Everywhere else it's optional free-form text (or
        omitted entirely) — see users/models.py's module-level comment on
        KENYA_COUNTRY_CODE for why this same rule is duplicated rather
        than shared with the model's clean(), which the API path never
        calls.
        """
        country = attrs.get("country", KENYA_COUNTRY_CODE)
        phone = (attrs.get("phone") or "").strip()

        if country == KENYA_COUNTRY_CODE:
            if not phone:
                raise serializers.ValidationError({"phone": ["Phone number is required for Kenyan accounts."]})
            try:
                normalized = normalize_kenyan_phone(phone)
            except InvalidKenyanPhone as exc:
                raise serializers.ValidationError({"phone": [str(exc)]}) from exc
            if UserSignup.objects.filter(phone=normalized).exists():
                raise serializers.ValidationError(
                    {"phone": ["An account with this phone number already exists."]}
                )
            attrs["phone"] = normalized
        else:
            if phone:
                if UserSignup.objects.filter(phone=phone).exists():
                    raise serializers.ValidationError(
                        {"phone": ["An account with this phone number already exists."]}
                    )
                attrs["phone"] = phone
            else:
                # Store NULL, not "" — see the field's comment in models.py
                # on why blank strings would collide under the unique index.
                attrs["phone"] = None

        return attrs

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
            "country",
            "avatar_url",
            "email_verified",
            "created_at",
        ]
        read_only_fields = fields


class ProfileUpdateSerializer(serializers.Serializer):
    """PATCH body for /api/users/me/ — only full_name and phone are
    editable here; email and country changes need re-verification /
    have currency implications respectively and aren't in scope for
    this pass (see MeView's docstring)."""
    full_name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)

    def validate_full_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate_phone(self, value):
        """
        Kenyan-format validation only applies if the account is Kenyan
        (self.instance.country) — same conditional rule as signup. A
        non-Kenyan user can set/clear their phone freely.
        """
        value = (value or "").strip()
        country = getattr(self.instance, "country", KENYA_COUNTRY_CODE) if self.instance else KENYA_COUNTRY_CODE

        if country == KENYA_COUNTRY_CODE:
            if not value:
                raise serializers.ValidationError("Phone number is required for Kenyan accounts.")
            try:
                normalized = normalize_kenyan_phone(value)
            except InvalidKenyanPhone as exc:
                raise serializers.ValidationError(str(exc)) from exc
        else:
            normalized = value or None

        existing = UserSignup.objects.filter(phone=normalized).exclude(
            pk=self.instance.pk if self.instance else None
        ) if normalized else UserSignup.objects.none()
        if existing.exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return normalized


class DeleteAccountSerializer(serializers.Serializer):
    """POST body for /api/users/me/delete/ — password required so a
    hijacked/left-open session can't nuke the account with one click,
    same reasoning as ChangePasswordSerializer requiring current_password."""
    password = serializers.CharField(write_only=True, trim_whitespace=False)


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


class PasswordResetRequestSerializer(serializers.Serializer):
    """POST body for /api/users/password-reset/request/."""

    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """POST body for /api/users/password-reset/confirm/. uid/token come
    straight from the link the person clicked (see build_reset_url)."""

    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_new_password(self, value):
        django_validate_password(value)
        return value


class ChangePasswordSerializer(serializers.Serializer):
    """POST body for /api/users/change-password/."""
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_new_password(self, value):
        django_validate_password(value)
        return value
