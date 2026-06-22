from rest_framework import serializers

from .models import PageView


class PageViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageView
        fields = ["url", "referrer", "title"]
