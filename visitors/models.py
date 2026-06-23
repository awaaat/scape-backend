from django.db import models


class Visitor(models.Model):
    """One row per browser session. Updated on every API hit from that session."""

    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    device_type = models.CharField(max_length=50, blank=True)
    browser = models.CharField(max_length=50, blank=True)
    operating_system = models.CharField(max_length=50, blank=True)

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    request_count = models.PositiveIntegerField(default=0)

    is_lead = models.BooleanField(default=False)
    lead_created_at = models.DateTimeField(null=True, blank=True)

    # ── Enrichment fields ────────────────────────────────────────────
    is_enriched = models.BooleanField(default=False, db_index=True)

    # Geo
    country = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=4, blank=True)
    region = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)

    # Company / ISP
    company_name = models.CharField(max_length=255, blank=True, db_index=True)
    company_domain = models.CharField(max_length=255, blank=True)
    company_industry = models.CharField(max_length=255, blank=True)
    company_size = models.CharField(max_length=50, blank=True)
    isp = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        label = self.company_name or self.ip_address or "unknown"
        return f"Visitor {self.session_id[:8]} ({label})"


class PageView(models.Model):
    """One row per page navigation reported by the frontend."""

    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name="page_views")
    url = models.URLField(max_length=500)
    referrer = models.URLField(max_length=500, blank=True)
    title = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.url} @ {self.created_at:%Y-%m-%d %H:%M}"