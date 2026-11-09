import re
import secrets

from django.db import models
from django.utils import timezone

from visitors.models import Visitor

from . import catalog

_REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I — readable over the phone


def generate_reference():
    return "SCP-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def generate_token():
    return secrets.token_urlsafe(24)


class Order(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft (not submitted)"
        SUBMITTED = "submitted", "New order"
        CONTACTED = "contacted", "Contacted"
        QUOTED = "quoted", "Quote sent"
        WON = "won", "Won / in progress"
        LOST = "lost", "Lost"
        SPAM = "spam", "Spam"

    reference = models.CharField(max_length=20, unique=True, default=generate_reference, editable=False)
    # Unguessable capability token. Holding it = being allowed to edit this draft.
    public_token = models.CharField(max_length=48, unique=True, default=generate_token, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    # Which storefront took this order — a key of catalog.SITES.
    site = models.CharField(max_length=40, blank=True, default="", db_index=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")

    # contact
    name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField(max_length=30, blank=True, help_text="Phone / WhatsApp, with country code")
    country = models.CharField(max_length=80, blank=True)

    # the order itself
    service_slug = models.CharField(max_length=80, blank=True)
    service = models.CharField(max_length=150, blank=True)
    academic_level = models.CharField(max_length=60, blank=True)
    data_status = models.CharField(max_length=80, blank=True)
    software = models.CharField(max_length=60, blank=True)
    deadline_window = models.CharField(max_length=60, blank=True)
    deadline = models.DateField(null=True, blank=True)
    topic = models.CharField(max_length=200, blank=True)
    details = models.TextField(blank=True)
    extra_answers = models.JSONField(default=dict, blank=True)

    # funnel + attribution
    current_step = models.PositiveSmallIntegerField(default=0)
    source_url = models.CharField(max_length=500, blank=True)
    referrer = models.CharField(max_length=500, blank=True)
    utm = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    # notification bookkeeping
    admin_notified = models.BooleanField(default=False)
    confirmation_sent = models.BooleanField(default=False)
    abandoned_notified = models.BooleanField(default=False)
    recovery_sent = models.BooleanField(default=False)

    admin_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-last_activity_at"]
        indexes = [models.Index(fields=["status", "last_activity_at"])]

    def __str__(self):
        return f"{self.reference} · {self.display_name} · {self.service or 'no service yet'}"

    # ── derived ────────────────────────────────────────────────────
    @property
    def site_info(self):
        return catalog.get_site(self.site)

    @property
    def display_name(self):
        return self.name.strip() or self.email or "Anonymous visitor"

    @property
    def days_left(self):
        if not self.deadline:
            return None
        return (self.deadline - timezone.localdate()).days

    @property
    def is_urgent(self):
        d = self.days_left
        return d is not None and d <= 3

    @property
    def whatsapp_digits(self):
        return re.sub(r"\D", "", self.phone or "")

    @property
    def whatsapp_link(self):
        d = self.whatsapp_digits
        return f"https://wa.me/{d}" if d else ""

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    def deadline_display(self):
        parts = []
        if self.deadline_window:
            parts.append(self.deadline_window)
        if self.deadline:
            dl = self.days_left
            when = "today" if dl == 0 else (f"in {dl} days" if dl and dl > 0 else f"{abs(dl)} days ago")
            parts.append(f"{self.deadline:%d %b %Y} ({when})")
        return " · ".join(parts)

    def summary_rows(self):
        """Ordered (label, value) pairs — drives the admin email, the client
        confirmation, the admin change page and the plain-text package."""
        rows = [
            ("Name", self.name),
            ("Email", self.email),
            ("WhatsApp / phone", self.phone),
            ("Country", self.country),
            ("Service", self.service),
            ("Academic level", self.academic_level),
            ("Data status", self.data_status),
            ("Software", self.software),
            ("Deadline", self.deadline_display()),
            ("Topic", self.topic),
            ("Details", self.details),
        ]
        for key, val in (self.extra_answers or {}).items():
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            rows.append((catalog.field_label(self.site, key), "" if val is None else str(val)))
        return [(label, str(value)) for label, value in rows if value not in ("", None)]

    def attribution_rows(self):
        rows = [("Storefront", f"{self.site_info['name']} ({self.site_info['key']})"),
                ("Submitted from", self.source_url), ("Referrer", self.referrer)]
        if self.utm:
            rows.append(("Campaign", ", ".join(f"{k}={v}" for k, v in self.utm.items())))
        v = self.visitor
        if v is not None:
            place = ", ".join(p for p in (v.city, v.country) if p)
            if place:
                rows.append(("Location (from IP)", place))
            rows.append(("Device", " / ".join(p for p in (v.device_type, v.browser, v.operating_system) if p)))
        return [(k, val) for k, val in rows if val]

    def as_text(self):
        lines = [f"Order {self.reference}  [{self.get_status_display()}]", ""]
        lines += [f"{label}: {value}" for label, value in self.summary_rows()]
        files = list(self.files.all())
        if files:
            lines += ["", "Files:"] + [f"  - {f.original_name}: {f.url}" for f in files]
        extra = self.attribution_rows()
        if extra:
            lines += [""] + [f"{k}: {v}" for k, v in extra]
        return "\n".join(lines)


class OrderFile(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="files")
    original_name = models.CharField(max_length=255)
    url = models.CharField(max_length=700)
    storage_path = models.CharField(max_length=500, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.original_name
