from django.db import models

from visitors.models import Visitor


class Lead(models.Model):
    visitor = models.ForeignKey(
        Visitor, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads"
    )
    name = models.CharField(max_length=150)
    email = models.EmailField()
    company = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    service = models.CharField(max_length=150)
    message = models.TextField()
    page_url = models.URLField(max_length=500, blank=True, help_text="Which page the form was submitted from")

    created_at = models.DateTimeField(auto_now_add=True)
    is_processed = models.BooleanField(default=False)

    welcome_email_sent = models.BooleanField(default=False)
    admin_notified = models.BooleanField(default=False)
    synced_to_brevo = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}> - {self.service}"
