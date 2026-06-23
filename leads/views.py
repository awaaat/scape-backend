import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .email import send_admin_notification, send_user_welcome, sync_lead_to_brevo
from .models import Lead
from .serializers import LeadSerializer, ExitIntentSerializer

logger = logging.getLogger("leads")


class ContactView(APIView):
    """
    POST /api/contact/
    Used by every contact form across the site.
    """

    throttle_scope = "contact"

    def post(self, request):
        serializer = LeadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        lead = serializer.save()

        visitor = getattr(request, "visitor", None)
        if visitor is not None:
            lead.visitor = visitor
            lead.save(update_fields=["visitor"])
            if not visitor.is_lead:
                from django.utils import timezone
                visitor.is_lead = True
                visitor.lead_created_at = timezone.now()
                visitor.save(update_fields=["is_lead", "lead_created_at"])

        try:
            send_user_welcome(lead)
            lead.welcome_email_sent = True
        except Exception as exc:
            logger.error("Welcome email failed for lead #%s: %s", lead.id, exc)

        try:
            send_admin_notification(lead)
            lead.admin_notified = True
        except Exception as exc:
            logger.error("Admin notification failed for lead #%s: %s", lead.id, exc)

        try:
            if sync_lead_to_brevo(lead):
                lead.synced_to_brevo = True
        except Exception as exc:
            logger.error("Brevo sync failed for lead #%s: %s", lead.id, exc)

        lead.save(update_fields=["welcome_email_sent", "admin_notified", "synced_to_brevo"])

        return Response(
            {"message": "Thanks — we've received your message and will be in touch within 24 hours."},
            status=status.HTTP_201_CREATED,
        )


class ExitIntentView(APIView):
    """
    POST /api/exit-intent/
    Lightweight email capture from the exit-intent popup.
    """

    throttle_scope = "contact"

    def post(self, request):
        serializer = ExitIntentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        lead = Lead.objects.create(
            name=data["name"],
            email=data["email"],
            service="Exit Intent",
            message=data.get("message") or "Captured via exit-intent popup.",
            page_url=data.get("page_url", ""),
        )

        visitor = getattr(request, "visitor", None)
        if visitor is not None:
            lead.visitor = visitor
            lead.save(update_fields=["visitor"])
            if not visitor.is_lead:
                from django.utils import timezone
                visitor.is_lead = True
                visitor.lead_created_at = timezone.now()
                visitor.save(update_fields=["is_lead", "lead_created_at"])

        try:
            send_user_welcome(lead)
            lead.welcome_email_sent = True
        except Exception as exc:
            logger.error("Exit-intent welcome email failed for lead #%s: %s", lead.id, exc)

        try:
            send_admin_notification(lead)
            lead.admin_notified = True
        except Exception as exc:
            logger.error("Exit-intent admin notification failed for lead #%s: %s", lead.id, exc)

        try:
            if sync_lead_to_brevo(lead):
                lead.synced_to_brevo = True
        except Exception as exc:
            logger.error("Exit-intent Brevo sync failed for lead #%s: %s", lead.id, exc)

        lead.save(update_fields=["welcome_email_sent", "admin_notified", "synced_to_brevo"])

        return Response({"message": "Thanks! We'll be in touch."}, status=status.HTTP_201_CREATED)