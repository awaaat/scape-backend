from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .app_registry import normalize_source, resolve_source
from .memberships import record_app_membership
from .models import UserSignup, KENYA_COUNTRY_CODE
from .phone_utils import normalize_kenyan_phone, InvalidKenyanPhone


class _OptionalSourceMixin(serializers.Serializer):
    """
    Which Scape app is calling (property / portal / thesis...). Always optional
    and lenient: an unknown value is ignored (not an error) and the backend
    falls back to Origin/Referer detection, so old/buggy clients never break.
    Clients send a KEY only, never a URL (see users/app_registry.py).
    """
    source = serializers.CharField(write_only=True, required=False, allow_blank=True, allow_null=True)

    def validate_source(self, value):
        return normalize_source(value)


class UserSignupCreateSerializer(_OptionalSourceMixin, serializers.ModelSerializer):
    """
    Inbound signup form. consent_given must be explicitly true.
    `country` drives whether `phone` is required/validated as a Kenyan number.
    """

    consent_given = serializers.BooleanField(write_only=True)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True, default="")
    country = serializers.CharField(max_length=2)

    class Meta:
        model = UserSignup
        fields = [
            "full_name", "email", "phone", "country", "consent_given",
            "privacy_policy_version", "password", "source",
        ]
        # Drop DRF's auto UniqueValidator on email: it runs BEFORE validate_email and would
        # hide the machine-readable account_exists code. validate_email does a case-insensitive
        # check instead, and the DB unique constraint is still the final guard (see SignupView).
        extra_kwargs = {"email": {"validators": []}}

    def validate_consent_given(self, value):
        if not value:
            raise serializers.ValidationError("Consent is required to sign up.")
        return value

    def validate_password(self, value):
        django_validate_password(value)
        return value

    def validate_email(self, value):
        value = value.strip()
        User = get_user_model()
        if (
            User.objects.filter(username__iexact=value).exists()
            or UserSignup.objects.filter(email__iexact=value).exists()
        ):
            # code="account_exists" lets SignupView add a machine-readable flag so any
            # front-end can say "you already have a Scape account - sign in instead".
            raise serializers.ValidationError(
                "An account with this email already exists.", code="account_exists",
            )
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
                raise serializers.ValidationError({"phone": ["An account with this phone number already exists."]})
            attrs["phone"] = normalized
        else:
            if phone:
                if UserSignup.objects.filter(phone=phone).exists():
                    raise serializers.ValidationError({"phone": ["An account with this phone number already exists."]})
                attrs["phone"] = phone
            else:
                attrs["phone"] = None  # NULL, not "" - see models.py on the unique index

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        password = validated_data.pop("password")
        explicit_source = validated_data.pop("source", None)
        validated_data["signup_source"] = resolve_source(request, explicit_source)
        validated_data["consent_given_at"] = timezone.now()

        if request is not None:
            validated_data["ip_address"] = request.META.get("REMOTE_ADDR")
            validated_data["user_agent"] = request.META.get("HTTP_USER_AGENT", "")
            visitor = getattr(request, "visitor", None)  # set by visitors/middleware.py
            if visitor is not None:
                validated_data["visitor"] = visitor

        User = get_user_model()
        # Atomic: if the profile insert fails (e.g. a phone/email uniqueness race), the
        # auth user is rolled back too. Without this an orphan auth user is left behind
        # and that email can never sign up again ("already exists" forever).
        with transaction.atomic():
            auth_user = User.objects.create_user(
                username=validated_data["email"],
                email=validated_data["email"],
                password=password,
                first_name=validated_data["full_name"].split(" ")[0],
            )
            validated_data["user"] = auth_user
            signup = super().create(validated_data)
            record_app_membership(auth_user, signup.signup_source)
        return signup


class UserSignupReadSerializer(serializers.ModelSerializer):
    apps = serializers.SerializerMethodField()

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
            "signup_source",
            "apps",
            "created_at",
        ]
        read_only_fields = fields

    def get_apps(self, obj):
        """Every Scape app this person has used (same account across all of them)."""
        if obj.user_id:
            apps = list(obj.user.app_memberships.values_list("source", flat=True))
            if apps:
                return apps
        return [obj.signup_source]


class ProfileUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150, required=False)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)

    def validate_full_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter your full name.")
        return value

    def validate_phone(self, value):
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
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class EmailVerificationRequestSerializer(_OptionalSourceMixin, serializers.Serializer):
    email = serializers.EmailField()


class EmailVerificationConfirmSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    token = serializers.CharField(max_length=64)


class LoginSerializer(_OptionalSourceMixin, serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class PasswordResetRequestSerializer(_OptionalSourceMixin, serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_new_password(self, value):
        django_validate_password(value)
        return value


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_new_password(self, value):
        django_validate_password(value)
        return value
