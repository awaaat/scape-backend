"""
property_intel/models.py

Core data model for Scape Property Intelligence.

Design principle: separate WHAT WE KNOW ABOUT A LOCATION (LocationCell) from
WHAT A BROKER ASKED FOR (PropertyPin) from WHAT WE DELIVERED (PropertyReport).

This split is what makes caching possible. Two brokers pasting two different
pins 80m apart in the same estate should hit Google's APIs exactly once
between them, not twice — LocationCell is the shared cache; PropertyPin is
just "this exact spot, at this exact moment, for this broker."

Caching strategy: geohash the coordinates to ~7 characters (~150m x 150m
cell). Nearby plots in the same estate share a LocationCell and therefore
share cached amenities, images, air quality, and travel times. This is the
single biggest lever on API cost at scale — see APICallLog for the
before/after cost visibility.

--------------------------------------------------------------------------
CHANGELOG (production hardening pass):
  - Payments (Paystack, idempotent webhooks) moved OUT of this app entirely
    into a standalone `payments` app — see payments/models.py,
    payments/paystack.py. property_intel only reacts to a `payment_succeeded`
    signal; it holds no payment-provider logic or webhook models itself.
  - FraudReviewLog: append-only audit trail. suspicion_score/is_blocked on
    DeviceFingerprint only ever show current state; this is what lets you
    answer "why was this device blocked, and when" after the fact.
  - OTPVerification: backs the SMS-OTP escalation path referenced in
    fraud.py — a suspicion score crossing the OTP threshold needs somewhere
    to actually record the code, expiry, and attempt count.
  - PropertyPin/DeviceFingerprint: no field changes, only new related models.
--------------------------------------------------------------------------
"""
import uuid

from django.conf import settings
from django.db import models

import geohash2 as geohash


# ---------------------------------------------------------------------------
# Geohash precision reference (approx cell size at the equator):
#   5 chars ≈ 4.9km x 4.9km   — too coarse, would blend distinct estates
#   6 chars ≈ 1.2km x 0.6km   — still too coarse for "which school is nearest"
#   7 chars ≈ 153m x 153m     — sweet spot: same estate, same road frontage
#   8 chars ≈ 38m x 19m       — too fine, defeats the point of caching
# ---------------------------------------------------------------------------
LOCATION_CELL_GEOHASH_PRECISION = 7

# How long cached location data is considered fresh before a report
# generation run is allowed to trigger a re-fetch from Google. Amenities and
# roads don't change week to week, but a bypass opening or a new school does
# eventually shift the picture — 90 days balances freshness against cost.
LOCATION_CELL_STALE_AFTER_DAYS = 90


def compute_geohash(latitude, longitude, precision=LOCATION_CELL_GEOHASH_PRECISION):
    return geohash.encode(latitude, longitude, precision=precision)


class LocationCell(models.Model):
    """
    The cache unit. One row per ~150m x 150m geohash cell that's ever been
    queried. All Google API data for that cell lives here — this is the
    thing that saves you money as usage grows, because a second pin in the
    same estate reads this row instead of calling Google again.

    Raw API responses are kept in JSONFields rather than being fully
    normalized into their own tables. Reasoning: Google's response shape for
    Places/Air Quality/Routes evolves, report templates evolve, and we don't
    yet know which fields end up mattering for the "investment score" model
    down the line. JSONField keeps this flexible now; if a specific field
    (e.g. nearest_school_distance_m) turns out to be queried/filtered on
    constantly, promote it to a real column later — don't guess upfront.
    """

    geohash = models.CharField(
        max_length=12,
        unique=True,
        db_index=True,
        help_text=f"Geohash at {LOCATION_CELL_GEOHASH_PRECISION}-char precision (~150m cell).",
    )

    # Cell center — NOT the same as any one broker's exact pin. Used for
    # display purposes and as the actual coordinate sent to Google APIs.
    center_latitude = models.DecimalField(max_digits=10, decimal_places=7)
    center_longitude = models.DecimalField(max_digits=10, decimal_places=7)

    # ── Geocoding ──────────────────────────────────────────────────────
    formatted_address = models.CharField(max_length=500, blank=True)
    geocode_raw_response = models.JSONField(
        default=dict, blank=True, help_text="Full Geocoding API response, kept for reprocessing without a re-call."
    )

    # ── Imagery ────────────────────────────────────────────────────────
    satellite_image_url = models.URLField(
        blank=True, help_text="Maps Static API — top-down satellite/map view."
    )
    satellite_image_fetched_at = models.DateTimeField(null=True, blank=True)

    street_view_available = models.BooleanField(
        default=False, help_text="Whether Google has street-level imagery for this cell at all."
    )
    street_view_image_url = models.URLField(blank=True)
    street_view_pano_id = models.CharField(
        max_length=100, blank=True, help_text="Google's panorama ID — lets us re-derive the image URL without re-querying metadata."
    )
    street_view_heading = models.FloatField(
        null=True, blank=True, help_text="Camera heading used, for reproducing the same framing later."
    )
    street_view_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Nearby amenities (Places API New — one list per category) ────────
    # Each entry: {"name": str, "lat": float, "lng": float, "distance_m": int,
    #              "place_id": str, "rating": float|null}
    nearby_schools = models.JSONField(default=list, blank=True)
    nearby_hospitals = models.JSONField(default=list, blank=True)
    nearby_banks = models.JSONField(default=list, blank=True)
    nearby_petrol_stations = models.JSONField(default=list, blank=True)
    nearby_shopping = models.JSONField(default=list, blank=True)
    nearby_universities = models.JSONField(default=list, blank=True)
    nearby_supermarkets = models.JSONField(default=list, blank=True)
    nearby_restaurants = models.JSONField(default=list, blank=True)
    nearby_student_housing = models.JSONField(default=list, blank=True)
    nearby_gated_communities = models.JSONField(default=list, blank=True)
    nearby_police_stations = models.JSONField(default=list, blank=True)
    nearby_fire_stations = models.JSONField(default=list, blank=True)
    nearby_pharmacies = models.JSONField(default=list, blank=True)
    nearby_transit_stops = models.JSONField(default=list, blank=True)
    nearby_parks = models.JSONField(default=list, blank=True)
    nearby_ev_charging = models.JSONField(default=list, blank=True)
    amenities_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Air quality ────────────────────────────────────────────────────
    air_quality_raw_response = models.JSONField(default=dict, blank=True)
    air_quality_index = models.PositiveSmallIntegerField(null=True, blank=True)
    air_quality_category = models.CharField(max_length=100, blank=True)
    air_quality_fetched_at = models.DateTimeField(null=True, blank=True)
    air_quality_history_raw = models.JSONField(default=dict, blank=True)
    air_quality_good_days_streak = models.PositiveSmallIntegerField(null=True, blank=True)
    air_quality_history_fetched_at = models.DateTimeField(null=True, blank=True)
    air_quality_forecast_raw = models.JSONField(default=dict, blank=True)
    air_quality_forecast_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Elevation ──────────────────────────────────────────────────────
    elevation_meters = models.FloatField(null=True, blank=True)
    elevation_grid = models.JSONField(default=list, blank=True, help_text="5-point sample (center + N/E/S/W ~150m) used to derive slope.")
    elevation_slope_range_m = models.FloatField(null=True, blank=True, help_text="max-min elevation across the sample grid -- a real slope signal, not a single-point guess.")
    elevation_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Road context (Roads API) ──────────────────────────────────────
    on_paved_road = models.BooleanField(null=True, blank=True)
    nearest_road_distance_m = models.PositiveIntegerField(null=True, blank=True)
    nearest_road_name = models.CharField(max_length=255, null=True, blank=True)
    road_context_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Major road context (Places Text Search, curated highway list) ──
    # Deprecated in favor of nearby_roads below -- left in place (unused
    # going forward) rather than dropped, to avoid a destructive migration.
    nearest_major_road_name = models.CharField(max_length=255, null=True, blank=True)
    nearest_major_road_distance_m = models.PositiveIntegerField(null=True, blank=True)
    major_road_context_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Nearby roads (OSM Overpass, falls back to Google Roads API) ────
    # Up to 3 nearest real roads by actual proximity, nearest first --
    # NOT filtered to any "major"/"minor" classification. Each entry:
    # {"name": str, "distance_m": int}.
    nearby_roads = models.JSONField(
        default=list, blank=True,
        help_text="Up to 3 nearest named roads, nearest first: [{'name':.., 'distance_m':..}, ...]. Not filtered by road classification.",
    )

    # ── Travel times (Routes API) ─────────────────────────────────────
    # {"nairobi_cbd": {"duration_s": int, "distance_m": int}, "local_cbd": {...}}
    travel_times = models.JSONField(default=dict, blank=True)
    travel_times_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Nearest towns (dynamic, Kenya-wide -- see kenya_towns.py) ──────
    # Up to 5 entries, nearest-first: {"name", "county", "rank",
    # "distance_m" (haversine), "drive_duration_s", "drive_distance_m"}.
    # Replaces the old fixed TRAVEL_DESTINATIONS satellite-town list, which
    # only worked correctly near Nairobi.
    nearest_towns = models.JSONField(default=list, blank=True)
    nearest_towns_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Soil (ISRIC SoilGrids v2.0, topsoil 0-5cm, 250m global grid) ───
    # Free, no-key REST API -- see google_client.fetch_soil_data(). Raw
    # values converted from SoilGrids' integer "mapped" units to the
    # conventional units noted per field. This is a global model, NOT
    # ground-truthed local soil survey data, so it's presented in the
    # report as informational context, never as a definitive fertility
    # verdict.
    soil_ph = models.FloatField(null=True, blank=True, help_text="Topsoil (0-5cm) pH in water (phh2o), mapped value / 10.")
    soil_organic_carbon_g_per_kg = models.FloatField(null=True, blank=True, help_text="Topsoil organic carbon (soc), mapped value / 10, in g/kg.")
    soil_nitrogen_g_per_kg = models.FloatField(null=True, blank=True, help_text="Topsoil total nitrogen, mapped value / 100, in g/kg.")
    soil_clay_pct = models.FloatField(null=True, blank=True, help_text="Topsoil clay content, mapped value / 10, in percent.")
    soil_sand_pct = models.FloatField(null=True, blank=True, help_text="Topsoil sand content, mapped value / 10, in percent.")
    soil_silt_pct = models.FloatField(null=True, blank=True, help_text="Topsoil silt content, mapped value / 10, in percent.")
    soil_raw_response = models.JSONField(default=dict, blank=True, help_text="Full ISRIC SoilGrids properties/query response, kept for reprocessing without a re-call.")
    soil_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Climate (NASA POWER, 30-year point climatology, free/no-key) ───
    # avg_annual_rainfall_mm is derived (ANN daily-average mm/day * 365),
    # not a direct API field -- it's a 30-year normal, not this year's
    # actual rainfall, and is labelled as such wherever it's printed.
    avg_annual_rainfall_mm = models.FloatField(null=True, blank=True, help_text="Approximate average annual rainfall in mm, derived from NASA POWER's 30-year PRECTOTCORR climatology (ANN daily average x 365).")
    avg_annual_temp_c = models.FloatField(null=True, blank=True, help_text="Average annual air temperature (deg C), NASA POWER T2M climatology, ANN value.")
    climate_raw_response = models.JSONField(default=dict, blank=True, help_text="Full NASA POWER climatology/point response, kept for reprocessing without a re-call.")
    climate_fetched_at = models.DateTimeField(null=True, blank=True)

    # ── Cache bookkeeping ──────────────────────────────────────────────
    first_queried_at = models.DateTimeField(auto_now_add=True)
    last_refreshed_at = models.DateTimeField(auto_now=True)
    times_reused = models.PositiveIntegerField(
        default=0, help_text="Incremented every time a NEW pin lands in this cell without needing fresh API calls. Your caching ROI, made visible."
    )

    class Meta:
        ordering = ["-last_refreshed_at"]
        indexes = [
            models.Index(fields=["geohash"]),
        ]

    def __str__(self):
        return f"{self.geohash} ({self.formatted_address or 'unresolved'})"

    @property
    def is_stale(self):
        from django.utils import timezone
        age_days = (timezone.now() - self.last_refreshed_at).days
        return age_days > LOCATION_CELL_STALE_AFTER_DAYS

    @property
    def has_complete_data(self):
        """True once every enrichment step has run at least once. Used to
        decide whether a cache hit is actually usable or needs backfilling
        (e.g. cell was created by an earlier version of the pipeline that
        didn't fetch air quality yet)."""
        return bool(
            self.formatted_address
            and self.satellite_image_url
            and self.amenities_fetched_at
            and self.air_quality_fetched_at
            and self.travel_times_fetched_at
        )


class DeviceFingerprint(models.Model):
    """
    THE actual free-tier gate. One row per unique browser/device fingerprint
    (generated client-side by FingerprintJS or equivalent, sent with every
    request). Free report allowance lives HERE, not on Broker/email —
    because email is trivial to regenerate, but a stable fingerprint hash
    survives clearing cookies, incognito, and changing email or IP.

    This is deliberately the source of truth for "has this device already
    used its free reports", independent of which email is attached today.
    """

    fingerprint_hash = models.CharField(max_length=100, unique=True, db_index=True)

    free_reports_remaining = models.PositiveSmallIntegerField(default=5)
    free_reports_used_total = models.PositiveIntegerField(default=0)

    # Supporting signals, not enforcement — useful for manual fraud review,
    # never used alone to block (too many false positives: shared office
    # WiFi, mobile carrier NAT, legitimate multi-device users).
    first_seen_ip = models.GenericIPAddressField(null=True, blank=True)
    known_ips = models.JSONField(default=list, blank=True, help_text="All IPs ever seen with this fingerprint, most recent last.")
    linked_emails = models.JSONField(default=list, blank=True, help_text="All emails ever associated with this fingerprint — a growing list here is itself a fraud signal.")

    is_blocked = models.BooleanField(default=False, help_text="Manually blocked after fraud review — hard stop regardless of remaining count.")
    block_reason = models.TextField(blank=True)

    # ── Fraud-scoring signals (see property_intel/fraud.py) ─────────────
    is_datacenter_ip = models.BooleanField(
        default=False, help_text="True if first_seen_ip resolved to a known VPN/proxy/datacenter ASN rather than a residential/mobile ISP."
    )
    ip_asn_name = models.CharField(max_length=255, blank=True, help_text="ISP/ASN name for first_seen_ip, e.g. 'Safaricom PLC' vs 'DigitalOcean'.")

    suspicion_score = models.PositiveSmallIntegerField(
        default=0, help_text="Latest computed fraud score (see fraud.py). Not cumulative — recalculated each time it matters."
    )
    requires_otp_verification = models.BooleanField(
        default=False, help_text="Set True when suspicion_score crosses the OTP threshold — free report withheld until phone OTP confirms a real number."
    )
    otp_verified_phone = models.CharField(max_length=20, blank=True)
    otp_verified_at = models.DateTimeField(null=True, blank=True)

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        return f"Device {self.fingerprint_hash[:12]}… ({self.free_reports_remaining} free left)"

    @property
    def suspicious(self):
        """Flag (not block) devices linked to an unusual number of emails/IPs —
        surfaced in admin for manual review, not auto-enforced."""
        return len(self.linked_emails) > 3 or len(self.known_ips) > 5

    def record_sighting(self, ip=None, email=None):
        """
        Updates known_ips/linked_emails/first_seen_ip. Called once per
        request that resolves this fingerprint (see views.py). Kept as a
        single method so "what counts as a new sighting" has one definition
        instead of being duplicated across call sites.
        """
        update_fields = []
        if ip:
            if not self.first_seen_ip:
                self.first_seen_ip = ip
                update_fields.append("first_seen_ip")
            known = list(self.known_ips or [])
            if ip not in known:
                known.append(ip)
                self.known_ips = known[-25:]  # cap — this is a fraud signal list, not an unbounded log
                update_fields.append("known_ips")
        if email:
            linked = list(self.linked_emails or [])
            if email not in linked:
                linked.append(email)
                self.linked_emails = linked[-25:]
                update_fields.append("linked_emails")
        if update_fields:
            self.save(update_fields=update_fields)

    def consume_free_report(self):
        """Atomically decrements the free counter. Returns True if a free
        report was available and consumed, False if the device is out of
        free reports (caller must require payment) or blocked."""
        if self.is_blocked or self.free_reports_remaining <= 0:
            return False
        updated = DeviceFingerprint.objects.filter(
            pk=self.pk, free_reports_remaining__gt=0
        ).update(
            free_reports_remaining=models.F("free_reports_remaining") - 1,
            free_reports_used_total=models.F("free_reports_used_total") + 1,
        )
        if updated:
            self.refresh_from_db(fields=["free_reports_remaining", "free_reports_used_total"])
            return True
        return False


class Broker(models.Model):
    """
    Lightweight identity — email only, no password, no Django auth. This is
    NOT django.contrib.auth.User on purpose: brokers never log in, there's
    no session to maintain, and adding a password field would reintroduce
    the signup friction we're specifically trying to avoid.

    Exists mainly as (a) a receipt/contact record for payments and emailed
    PDFs, and (b) a link back to the DeviceFingerprint that actually
    enforces the free tier.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    email_is_disposable = models.BooleanField(
        default=False, help_text="Flagged at signup if the email domain matches a known disposable/temp-mail provider list."
    )

    device_fingerprint = models.ForeignKey(
        DeviceFingerprint, on_delete=models.SET_NULL, null=True, blank=True, related_name="brokers"
    )
    # Links this Broker record to a logged-in dashboard account, when one
    # exists. Nullable because Broker predates any login system — a pin
    # can still be submitted by someone who never signed up. Set the
    # moment a submission comes in with a valid JWT (see views.py).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="broker_profile"
    )
    signup_ip = models.GenericIPAddressField(null=True, blank=True)

    # Set after the broker's FIRST successful Paystack payment. This is the
    # strongest identity signal available: a Safaricom number requires ID
    # registration, so the same number reappearing on a "new" Broker record
    # is a much harder thing to fake than a new email or even a new device.
    # Store a hash, not the raw number/card — this field is for matching,
    # not for display; the real value lives in Paystack's own records.
    payment_method_hash = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="SHA-256 of the M-Pesa number or card fingerprint from the first confirmed Paystack payment.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_active_at"]

    def __str__(self):
        return self.email


class PropertyPin(models.Model):
    """
    One exact location a broker pasted in. Always belongs to a LocationCell
    (created if it doesn't exist yet). Kept separate from LocationCell so we
    never lose the broker's literal input, exact coordinates, or the fact
    that they looked this specific plot up — even though the enrichment data
    itself is shared/cached at the cell level.
    """

    INPUT_TYPE_CHOICES = [
        ("coordinates", "Raw coordinates"),
        ("google_maps_link", "Google Maps link"),
        ("google_maps_short_link", "Google Maps short link"),
        ("whatsapp_location", "WhatsApp shared location"),
        ("plus_code", "Google Plus Code"),
        ("unknown", "Unrecognized format"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    raw_input = models.TextField(help_text="Exactly what the broker pasted, before any parsing.")
    input_type = models.CharField(max_length=30, choices=INPUT_TYPE_CHOICES, default="unknown")

    # The exact pin — deliberately NOT rounded, unlike LocationCell's center.
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)

    location_cell = models.ForeignKey(
        LocationCell, on_delete=models.PROTECT, related_name="pins"
    )
    was_cache_hit = models.BooleanField(
        default=False, help_text="True if location_cell already existed and had complete data when this pin was submitted."
    )

    broker = models.ForeignKey(Broker, on_delete=models.PROTECT, related_name="pins")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["location_cell", "created_at"], name="pin_cell_velocity_idx"),
        ]

    def __str__(self):
        return f"Pin ({self.latitude}, {self.longitude}) — {self.get_input_type_display()}"


class PropertyReport(models.Model):
    """One generated report for one pin. A pin can have multiple reports
    over time (e.g. re-generated after cell data refreshes, or re-downloaded)."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("awaiting_payment", "Awaiting payment"),
        ("awaiting_review", "Awaiting manual fraud review"),
        ("generating", "Generating"),
        ("ready", "Ready"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pin = models.ForeignKey(PropertyPin, on_delete=models.CASCADE, related_name="reports")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    failure_reason = models.TextField(blank=True)

    # PDF is generated then uploaded to object storage (Supabase Storage /
    # S3) — see property_intel/storage.py, following the same pattern jobs
    # app uses for resumes: never trust local disk, Render wipes it.
    pdf_storage_path = models.CharField(max_length=500, blank=True)
    pdf_generated_at = models.DateTimeField(null=True, blank=True)

    # Snapshot of computed scores at generation time — kept even if the
    # underlying LocationCell data changes later, so a broker's downloaded
    # PDF and the numbers you can look up in admin always agree.
    investment_score = models.PositiveSmallIntegerField(null=True, blank=True)
    accessibility_score = models.PositiveSmallIntegerField(null=True, blank=True)
    ai_summary_text = models.TextField(blank=True)

    # ── Payment ────────────────────────────────────────────────────────
    is_free_tier = models.BooleanField(default=True, help_text="Consumed from the device's free report allowance.")
    price_charged_kes = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    wallet_applied_kes = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text=(
            "Partial wallet balance earmarked toward this report's price at "
            "checkout time (e.g. KES 36 of a KES 199 report). Only actually "
            "debited from the wallet once the Paystack remainder is "
            "confirmed paid -- see property_intel/signals.py."
        ),
    )
    is_paid = models.BooleanField(default=False)
    paystack_reference = models.CharField(
        max_length=100, blank=True, db_index=True,
        help_text="Paystack transaction reference — set when payment initializes, confirmed via webhook.",
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report {self.id} — {self.get_status_display()}"


class APICallLog(models.Model):
    """
    One row per external API request actually sent (not per cache hit — the
    whole point is this table stays small if caching is working). This is
    what lets you answer "how much did Google actually cost us this month"
    and "is our cache hit rate improving" with a query instead of a guess.

    Billing note: Google charges per request received, regardless of
    response code — a 404/400 still costs money. So failed calls are logged
    here too, with their status, not silently dropped.
    """

    API_CHOICES = [
        ("geocoding", "Geocoding API"),
        ("maps_static", "Maps Static API"),
        ("street_view_static", "Street View Static API"),
        ("street_view_metadata", "Street View Metadata (free — checks availability before billing for the image)"),
        ("places_nearby", "Places API (New) — Nearby Search"),
        ("routes", "Routes API"),
        ("air_quality", "Air Quality API"),
        ("elevation", "Elevation API"),
        ("roads", "Roads API"),
        ("places_text", "Places API (New) — Text Search"),
    ]

    api = models.CharField(max_length=30, choices=API_CHOICES, db_index=True)
    location_cell = models.ForeignKey(
        LocationCell, on_delete=models.SET_NULL, null=True, blank=True, related_name="api_calls"
    )

    request_params = models.JSONField(default=dict, blank=True)
    response_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)

    estimated_cost_usd = models.DecimalField(
        max_digits=8, decimal_places=5, default=0,
        help_text="Estimated cost of THIS call based on the SKU's published price — not billed truth, Google's own billing dashboard is the source of truth for actual spend.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["api", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_api_display()} — {'OK' if self.succeeded else 'FAILED'} ({self.created_at:%Y-%m-%d %H:%M})"


# ---------------------------------------------------------------------------
# NEW — production-hardening models
# ---------------------------------------------------------------------------

class FraudReviewLog(models.Model):
    """
    Append-only audit trail for fraud decisions. DeviceFingerprint's
    suspicion_score/is_blocked fields only ever reflect the CURRENT state —
    this table is what lets you reconstruct "why was this device blocked,
    when, and on what score" after the fact. That matters for two reasons:
    tuning POINTS_*/thresholds in fraud.py against real data, and handling
    a legitimate broker's "why am I blocked" complaint fairly rather than
    guessing.
    """

    ACTION_CHOICES = [
        ("score_computed", "Suspicion score computed"),
        ("otp_required", "OTP verification required"),
        ("otp_verified", "OTP verification completed"),
        ("held_for_review", "Held for manual review"),
        ("manually_blocked", "Manually blocked"),
        ("manually_unblocked", "Manually unblocked"),
    ]

    device_fingerprint = models.ForeignKey(
        DeviceFingerprint, on_delete=models.CASCADE, related_name="fraud_logs"
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    reasons = models.JSONField(default=list, blank=True)
    actor = models.CharField(
        max_length=150, blank=True,
        help_text="Admin username for manual actions (block/unblock); blank for automated scoring events.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["device_fingerprint", "created_at"])]

    def __str__(self):
        return f"{self.get_action_display()} — device {self.device_fingerprint.fingerprint_hash[:12]}…"


class OTPVerification(models.Model):
    """
    Backs the SMS-OTP escalation path (fraud.py OTP_THRESHOLD). A device
    whose suspicion score crosses the threshold has its free report
    withheld until this succeeds — the underlying assumption being that a
    second registered Safaricom line is a real cost to acquire, unlike a
    second email address.

    code_hash, never the raw code: this table is queried by admins
    debugging failed OTP flows, and there's no reason the raw code needs to
    be readable in the database once it's been sent.
    """

    device_fingerprint = models.ForeignKey(
        DeviceFingerprint, on_delete=models.CASCADE, related_name="otp_attempts"
    )
    phone_number = models.CharField(max_length=20)
    code_hash = models.CharField(max_length=64)

    attempts = models.PositiveSmallIntegerField(default=0)
    MAX_ATTEMPTS = 5

    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["device_fingerprint", "created_at"])]

    def __str__(self):
        status = "verified" if self.verified_at else "pending"
        return f"OTP for {self.phone_number} ({status})"

    @property
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() >= self.expires_at

    @property
    def is_exhausted(self):
        return self.attempts >= self.MAX_ATTEMPTS