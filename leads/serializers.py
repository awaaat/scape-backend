from rest_framework import serializers

from .models import Lead


class LeadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = ["name", "email", "company", "phone", "service", "message", "page_url"]

    def validate_message(self, value):
        if len(value.strip()) < 10:
            raise serializers.ValidationError("Message must be at least 10 characters.")
        return value

    def validate_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Name looks too short.")
        return value


class ExitIntentSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    message = serializers.CharField(required=False, allow_blank=True, default="")
    page_url = serializers.URLField(required=False, allow_blank=True, default="")

    def validate_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Name looks too short.")
        return value