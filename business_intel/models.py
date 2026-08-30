"""
business_intel/models.py

Production-grade data models for the Business Location Intelligence Engine.
Implements the 27-engine architecture with full traceability,
versioning, and auditability.

Design principles:
- Every computed number is a FeatureObservation row with metadata.
- Staleness is source-class dependent, not a constant.
- Atomic write operations prevent duplicate "latest" versions.
- All foreign keys use explicit on_delete policies.
- Database-level constraints enforce business rules.
"""

import uuid
import logging
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    MinValueValidator,
    MaxValueValidator,
    RegexValidator,
)
from django.db import models, transaction
from django.db.models import Q, UniqueConstraint, CheckConstraint, F
from django.utils import timezone
import geohash2 as geohash

logger = logging.getLogger("business_intel")


# =============================================================================
# Shared constants, validators, and mixins
# =============================================================================

LOCATION_CELL_GEOHASH_PRECISION = 7

# Staleness windows (in days) by source class – governs cache freshness.
# These are calibrated per spec's data source hierarchy.
STALENESS_DAYS_BY_SOURCE_CLASS = {
    "realtime_api": 30,           # Google Places, Routes – competitor landscape shifts fast
    "scraped_listing": 21,        # property/rental listings turn over quickly
    "government_survey": 730,     # KNBS census/survey aggregates – multi-year validity
    "gridded_estimate": 365,      # WorldPop annual releases
    "satellite": 90,              # change-detection imagery
    "internal_derived": 30,       # features computed from our own accumulating history
    "manual_entry": 180,
    "unknown": 30,                # conservative default
}

# Kenya bounding box (loose sanity check)
KENYA_LAT_BOUNDS = (-5.5, 5.5)
KENYA_LNG_BOUNDS = (33.0, 42.5)

latitude_validators = [MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))]
longitude_validators = [MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))]

geohash_validator = RegexValidator(
    regex=r"^[0123456789bcdefghjkmnpqrstuvwxyz]+$",
    message="Not a valid base32 geohash.",
)

score_validators = [MinValueValidator(0), MaxValueValidator(100)]


def compute_geohash(latitude, longitude, precision=LOCATION_CELL_GEOHASH_PRECISION):
    """Encode lat/lng to a geohash string of given precision."""
    return geohash.encode(float(latitude), float(longitude), precision=precision)


def validate_kenya_bounds(latitude, longitude):
    """Loose bounding-box sanity check – catches swapped lat/lng and stray (0,0)."""
    if not (KENYA_LAT_BOUNDS[0] <= float(latitude) <= KENYA_LAT_BOUNDS[1]):
        raise ValidationError(f"Latitude {latitude} is outside the supported region.")
    if not (KENYA_LNG_BOUNDS[0] <= float(longitude) <= KENYA_LNG_BOUNDS[1]):
        raise ValidationError(f"Longitude {longitude} is outside the supported region.")


class TimeStampedModel(models.Model):
    """Abstract base with created/updated timestamps and user audit."""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_created",
        help_text="User who created this record.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_updated",
        help_text="User who last updated this record.",
    )

    class Meta:
        abstract = True


class StaleAwareModel(models.Model):
    """
    Mixin for cached data from external sources. Staleness is derived
    from source_class + last_refreshed_at, not a hardcoded constant.
    """
    source_class = models.CharField(
        max_length=30,
        choices=[(k, k.replace("_", " ").title()) for k in STALENESS_DAYS_BY_SOURCE_CLASS],
        default="unknown",
        help_text="Governs staleness window. See STALENESS_DAYS_BY_SOURCE_CLASS.",
    )
    last_refreshed_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True

    @property
    def staleness_window_days(self):
        return STALENESS_DAYS_BY_SOURCE_CLASS.get(self.source_class, STALENESS_DAYS_BY_SOURCE_CLASS["unknown"])

    @property
    def is_stale(self):
        if self.last_refreshed_at is None:
            return True
        age = timezone.now() - self.last_refreshed_at
        return age.days > self.staleness_window_days

    @property
    def age_days(self):
        if self.last_refreshed_at is None:
            return None
        return (timezone.now() - self.last_refreshed_at).days


# =============================================================================
# 1. Spatial Context – LocationCell + Catchment
# =============================================================================

class BusinessLocationCell(StaleAwareModel, TimeStampedModel):
    """
    Cache unit: one row per ~150m x 150m geohash cell ever queried.
    All spatial entities FK to this cell to avoid duplicate geometry.
    """
    geohash = models.CharField(
        max_length=12,
        unique=True,
        db_index=True,
        validators=[geohash_validator],
        help_text=f"Geohash at {LOCATION_CELL_GEOHASH_PRECISION}-char precision (~150m cell).",
    )
    center_latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        validators=latitude_validators,
        help_text="Decimal latitude of cell center.",
    )
    center_longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        validators=longitude_validators,
        help_text="Decimal longitude of cell center.",
    )
    formatted_address = models.CharField(max_length=500, blank=True)
    county = models.CharField(max_length=100, blank=True, db_index=True)
    # Real KNBS census hierarchy: county -> subcounty -> sublocation.
    # Independent of `ward`, which is the parallel devolved-government
    # hierarchy (county -> subcounty -> ward) used by IEBC/county
    # dashboards -- KNBS does not publish population figures by ward, so
    # `ward` and `subcounty` are deliberately separate fields rather than
    # aliases of each other; see business_intel.engines.population's
    # RealAdminHierarchyProvider docstring for how subcounty is used to
    # resolve real sub-county-level population.
    subcounty = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Real KNBS sub-county name, e.g. 'Ruiru'. Distinct from `ward`.",
    )
    sublocation = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Real KNBS sub-location name -- the finest administrative unit KNBS "
            "publishes population figures for (an EA 'may be a village, group of "
            "villages or part of a village', per KNBS's own definition). Population "
            "data at this level is not yet available for most of the country; see "
            "business_intel.data.kenya_sublocations_2019_census (Mombasa only, as of "
            "this writing) and RealAdminHierarchyProvider's docstring."
        ),
    )
    ward = models.CharField(max_length=100, blank=True)
    first_queried_at = models.DateTimeField(auto_now_add=True)
    times_reused = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-last_refreshed_at"]
        indexes = [
            models.Index(fields=["geohash"]),
            models.Index(fields=["county", "ward"]),
            models.Index(fields=["county", "subcounty"]),
        ]

    def __str__(self):
        return f"{self.geohash} ({self.formatted_address or 'unresolved'})"

    @property
    def has_isochrones(self):
        """True if this cell has at least one walk/drive/transit isochrone computed."""
        return self.catchments.filter(
            catchment_type__in=("walk_isochrone", "drive_isochrone", "transit_isochrone")
        ).exists()

    def has_category_data(self, category):
        """True if this cell has a fresh (non-stale) CategoryEnrichmentLog
        row for `category` -- i.e. Places has actually been searched for
        this category near this cell recently, REGARDLESS of how many
        results came back. Zero competitors found is a valid, meaningful
        state (a genuinely open market) and must be distinguishable from
        'never searched'.

        services.needs_category_enrichment() has called this method since
        it was written; it simply didn't exist on the model, so every
        report generation crashed here before ever reaching Places.

        NOTE ON ANCHORS: tasks.py's anchor-enrichment step writes to
        cell.nearby_anchors / cell.anchors_fetched_at, neither of which
        exist on this model either -- same bug, not fixed in this pass.
        Unlike category search results, anchors already have a proper
        normalized table (AnchorFacility, with its own location_cell FK
        and place_id, mirroring BusinessEntity exactly) -- so the fix
        there should sync into AnchorFacility, NOT add a second
        JSON-on-cell design alongside it. Left for engines/anchor.py to
        be checked first, since I haven't seen what it currently queries
        and don't want to guess-wire it.
        """
        log = self.category_enrichments.filter(category=category).first()
        if log is None:
            return False
        return not log.is_stale

    def clean(self):
        validate_kenya_bounds(self.center_latitude, self.center_longitude)
        expected = compute_geohash(self.center_latitude, self.center_longitude)
        if self.geohash and self.geohash != expected:
            raise ValidationError(
                f"geohash '{self.geohash}' does not match center coordinates "
                f"(expected '{expected}'). Recompute via compute_geohash()."
            )

    def save(self, *args, **kwargs):
        if not self.geohash:
            self.geohash = compute_geohash(self.center_latitude, self.center_longitude)
        self.full_clean()
        super().save(*args, **kwargs)


CATCHMENT_TYPE_CHOICES = [
    ("radius", "Fixed-radius circular band"),
    ("network_radius", "Network-reachable area within a distance"),
    ("walk_isochrone", "Walking travel-time isochrone"),
    ("drive_isochrone", "Driving travel-time isochrone (traffic-aware)"),
    ("transit_isochrone", "Public-transport travel-time isochrone"),
]

DISTANCE_BAND_METERS = [250, 500, 1000, 2000, 3000, 5000, 10000]
WALK_TIME_MINUTES = [5, 10, 15, 20, 30]
DRIVE_TIME_MINUTES = [5, 10, 15, 20, 30]
TRANSIT_TIME_MINUTES = [15, 30, 45]


class Catchment(StaleAwareModel, TimeStampedModel):
    """
    One row = one (cell, catchment_type, catchment_size) combination.
    Replaces hardcoded fields on LocationCell with a flexible related table.
    """
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.CASCADE,
        related_name="catchments",
        help_text="The cell this catchment belongs to.",
    )
    catchment_type = models.CharField(max_length=20, choices=CATCHMENT_TYPE_CHOICES)
    catchment_size_value = models.PositiveIntegerField(help_text="e.g. 500, or 15")
    catchment_size_unit = models.CharField(
        max_length=10,
        choices=[("meters", "meters"), ("minutes", "minutes")],
        help_text="Unit of the size value.",
    )
    catchment_size_label = models.CharField(
        max_length=20,
        help_text="Display label, e.g. '500m', '15min'.",
    )
    is_network_based = models.BooleanField(
        default=False,
        help_text="False = naive circular buffer; True = network-reachable area.",
    )
    geometry_geojson = models.JSONField(
        null=True,
        blank=True,
        help_text="GeoJSON polygon of actual reachable area (if network-based).",
    )
    area_km2 = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Area in square kilometres.",
    )
    computation_method = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g. 'circular_buffer', 'osmnx_dijkstra', 'google_routes_traffic_aware'.",
    )
    traffic_profile = models.CharField(
        max_length=20,
        blank=True,
        choices=[("off_peak", "Off-peak"), ("peak", "Peak"), ("weekend", "Weekend")],
        help_text="Only meaningful for drive_isochrone.",
    )

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["location_cell", "catchment_type", "catchment_size_value", "traffic_profile"],
                name="uniq_catchment_per_cell_type_size_traffic",
            ),
            CheckConstraint(
                check=Q(catchment_size_value__gt=0),
                name="catchment_size_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["location_cell", "catchment_type"]),
        ]
        ordering = ["location_cell", "catchment_type", "catchment_size_value"]

    def __str__(self):
        return f"{self.location_cell.geohash} / {self.catchment_type} / {self.catchment_size_label}"

    def clean(self):
        if self.catchment_type in ("radius", "network_radius") and self.catchment_size_unit != "meters":
            raise ValidationError("Radius-type catchments must be sized in meters.")
        if self.catchment_type.endswith("_isochrone") and self.catchment_size_unit != "minutes":
            raise ValidationError("Isochrone catchments must be sized in minutes.")
        if self.catchment_type != "drive_isochrone" and self.traffic_profile:
            raise ValidationError("traffic_profile only applies to drive_isochrone.")


# =============================================================================
# 2. Feature Store – the spec's "What Must Be Stored Per Feature"
# =============================================================================

ENGINE_CHOICES = [
    (1, "spatial_context"), (2, "population"), (3, "purchasing_power"),
    (4, "mobility_traffic"), (5, "forced_traffic"), (6, "business_ecosystem"),
    (7, "business_survival"), (8, "competition"), (9, "market_gap"),
    (10, "business_adjacency"), (11, "anchor"), (12, "development_activity"),
    (13, "infrastructure_change"), (14, "commercial_occupancy"), (15, "property_cost"),
    (16, "visibility_access"), (17, "temporal"), (18, "economic_momentum"),
    (19, "opportunity_detection"), (20, "failure_detection"), (21, "anomaly_detection"),
    (22, "industry_translation"), (23, "evidence"), (24, "confidence"),
    (25, "scenario"), (26, "decision"), (27, "cross_engine_intelligence"),
]

VALUE_TYPE_CHOICES = [
    ("numeric", "Numeric"),
    ("categorical", "Categorical"),
    ("boolean", "Boolean"),
    ("json", "Structured (JSON)"),
]

DERIVATION_LEVEL_CHOICES = [
    ("observation", "Direct observation from a source"),
    ("calculation", "Deterministic formula over observations"),
    ("model_estimate", "Statistical/ML model output"),
    ("prediction", "Forecast of a future value"),
    ("business_conclusion", "Human-facing signal/decision output"),
]


class FeatureObservation(StaleAwareModel, TimeStampedModel):
    """
    One row = one computed or observed metric for one location context,
    at one point in time, at one version. This is the versioned feature store.
    """
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.CASCADE,
        related_name="features",
        help_text="The cell this feature applies to.",
    )
    catchment = models.ForeignKey(
        Catchment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="features",
        help_text="Optional catchment context (null for cell-level features).",
    )
    business_category = models.CharField(
        max_length=30,
        blank=True,
        db_index=True,
        help_text="Business category if feature is category-specific; blank otherwise.",
    )
    engine_number = models.PositiveSmallIntegerField(
        choices=[(n, f"{n}. {name}") for n, name in ENGINE_CHOICES],
        db_index=True,
        help_text="Which of the 27 engines produced this feature.",
    )
    feature_name = models.CharField(
        max_length=150,
        db_index=True,
        help_text="Short, machine-readable name of the feature, e.g. 'population_density'.",
    )
    value_type = models.CharField(max_length=15, choices=VALUE_TYPE_CHOICES)
    value_numeric = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    value_text = models.CharField(max_length=500, blank=True)
    value_json = models.JSONField(null=True, blank=True)
    unit = models.CharField(max_length=30, blank=True, help_text="e.g. 'people/km2', 'KES/m2', '%'.")
    observed_at = models.DateTimeField(help_text="When the underlying observation/model run occurred.")
    source = models.CharField(max_length=100, help_text="e.g. 'Google Places', 'WorldPop', 'internal_derived'.")
    source_timestamp = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the SOURCE collected/published this, if different from observed_at.",
    )
    method = models.CharField(
        max_length=100,
        blank=True,
        help_text="Calculation or model used, e.g. 'HHI', 'huff_model', 'kaplan_meier'.",
    )
    derivation_level = models.CharField(max_length=25, choices=DERIVATION_LEVEL_CHOICES)
    sample_size = models.PositiveIntegerField(null=True, blank=True)
    coverage_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Spatial/temporal coverage percentage of inputs.",
    )
    uncertainty = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Statistical/model uncertainty, same unit as value_numeric where applicable.",
    )
    confidence_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Calibrated reliability of this specific feature (0-100).",
    )
    version = models.PositiveIntegerField(default=1, help_text="Version number, incremented on recomputation.")
    is_latest = models.BooleanField(
        default=True,
        db_index=True,
        help_text="True for the most recent version of this feature identity.",
    )

    class Meta:
        ordering = ["-observed_at"]
        indexes = [
            models.Index(fields=["location_cell", "feature_name", "is_latest"]),
            models.Index(fields=["engine_number", "is_latest"]),
            models.Index(fields=["business_category", "feature_name"]),
            models.Index(fields=["location_cell", "engine_number", "is_latest"]),
        ]
        constraints = [
            # Only one latest version per identity tuple
            UniqueConstraint(
                fields=["location_cell", "catchment", "business_category", "engine_number", "feature_name"],
                condition=Q(is_latest=True),
                name="uniq_latest_feature_per_identity",
            ),
            # Ensure value_type matches the actual value field
            CheckConstraint(
                check=(
                    Q(value_type="numeric", value_numeric__isnull=False) |
                    Q(value_type="categorical", value_text__gt="") |
                    Q(value_type="boolean", value_json__isnull=False) |
                    Q(value_type="json", value_json__isnull=False)
                ),
                name="feature_has_value_in_matching_field",
            ),
        ]

    def __str__(self):
        return f"[{self.get_engine_number_display()}] {self.feature_name} = {self.display_value} (v{self.version})"

    @property
    def display_value(self):
        if self.value_type == "numeric":
            return self.value_numeric
        if self.value_type == "categorical":
            return self.value_text
        if self.value_type == "boolean":
            return self.value_json if self.value_json is not None else False
        return self.value_json

    def clean(self):
        # Validate that source_timestamp is not after observed_at
        if self.source_timestamp and self.observed_at and self.source_timestamp > self.observed_at:
            raise ValidationError("source_timestamp cannot be after observed_at.")
        # Ensure derivation_level is consistent with source/method (optional)
        if self.derivation_level == "observation" and self.method:
            # Observations should not have a method (they are direct)
            # but allow if method is "direct_fetch" or similar
            pass

    @classmethod
    def write_latest(cls, *, location_cell, engine_number, feature_name, defaults,
                     catchment=None, business_category=""):
        """
        Atomic write of a new latest version. Uses select_for_update to prevent
        race conditions where two workers try to write the same identity concurrently.
        """
        with transaction.atomic():
            # Lock the existing latest row (if any) to prevent race condition
            existing = cls.objects.select_for_update().filter(
                location_cell=location_cell,
                catchment=catchment,
                business_category=business_category,
                engine_number=engine_number,
                feature_name=feature_name,
                is_latest=True,
            ).first()

            next_version = (existing.version + 1) if existing else 1
            if existing:
                existing.is_latest = False
                existing.save(update_fields=["is_latest"])

            obj = cls.objects.create(
                location_cell=location_cell,
                catchment=catchment,
                business_category=business_category,
                engine_number=engine_number,
                feature_name=feature_name,
                version=next_version,
                is_latest=True,
                **defaults,
            )
            return obj


class EngineRun(TimeStampedModel):
    """
    One row per execution of one engine against one BusinessQuery.
    Tracks performance, status, and features written.
    """
    STATUS_CHOICES = [
        ("succeeded", "Succeeded"),
        ("degraded", "Succeeded with reduced inputs"),
        ("failed", "Failed"),
        ("skipped", "Skipped (inputs unavailable)"),
    ]

    query = models.ForeignKey("BusinessQuery", on_delete=models.CASCADE, related_name="engine_runs")
    engine_number = models.PositiveSmallIntegerField(choices=[(n, n) for n, _ in ENGINE_CHOICES])
    status = models.CharField(max_length=15, choices=STATUS_CHOICES)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    features_written = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    degraded_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="e.g. 'population_estimate unavailable, used competitor-count-only fallback'.",
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["query", "engine_number"]),
            models.Index(fields=["status", "started_at"]),
        ]

    def __str__(self):
        return f"Engine {self.engine_number} run for {self.query_id}: {self.status}"

    def clean(self):
        if self.finished_at and self.started_at and self.finished_at < self.started_at:
            raise ValidationError("finished_at cannot be before started_at.")
        if self.status == "failed" and not self.error_message:
            raise ValidationError("A failed EngineRun must record error_message.")
        if self.status == "degraded" and not self.degraded_reason:
            raise ValidationError("A degraded EngineRun must record degraded_reason.")


# =============================================================================
# 3. Business Entities – persistent identity across time
# =============================================================================

BUSINESS_CATEGORY_CHOICES = [
    ("pharmacy", "Pharmacy / Chemist"),
    ("supermarket", "Supermarket / Minimart"),
    ("restaurant", "Restaurant / Eatery"),
    ("salon_barbershop", "Salon / Barbershop"),
    ("hardware_store", "Hardware Store"),
    ("electronics_shop", "Electronics Shop"),
    ("gym", "Gym / Fitness Studio"),
    ("clinic", "Clinic / Medical Centre"),
    ("bank_agent", "Bank Agent / Mobile Money Shop"),
    ("boutique_clothing", "Boutique / Clothing Store"),
    ("bar", "Bar / Pub"),
    ("bakery", "Bakery"),
    ("other", "Other (see business_category_other)"),
]

CATEGORY_PLACES_TYPE_MAP = {
    "pharmacy": ["pharmacy"],
    "supermarket": ["supermarket", "grocery_store"],
    "restaurant": ["restaurant"],
    "salon_barbershop": ["hair_salon", "barber_shop"],
    "hardware_store": ["hardware_store"],
    "electronics_shop": ["electronics_store"],
    "gym": ["gym"],
    "clinic": ["doctor", "medical_clinic"],
    "bank_agent": ["bank", "atm"],
    "boutique_clothing": ["clothing_store"],
    "bar": ["bar"],
    "bakery": ["bakery"],
}

BUSINESS_STATUS_CHOICES = [
    ("operational", "Operational"),
    ("closed_temporarily", "Closed Temporarily"),
    ("closed_permanently", "Closed Permanently"),
    ("unknown", "Unknown (not re-observed recently)"),
]


class BusinessEntity(StaleAwareModel, TimeStampedModel):
    """
    A real-world business, tracked across time. Provides persistent identity
    for survival analysis, ecosystem, and competition engines.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    place_id = models.CharField(max_length=255, unique=True, null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=30, choices=BUSINESS_CATEGORY_CHOICES, db_index=True)
    category_other = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, validators=latitude_validators)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, validators=longitude_validators)
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="businesses",
        help_text="Geohash cell this business falls into.",
    )
    brand = models.CharField(max_length=255, blank=True, db_index=True, help_text="Chain/brand name.")
    current_status = models.CharField(max_length=20, choices=BUSINESS_STATUS_CHOICES, default="unknown")
    current_rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
    )
    current_review_count = models.PositiveIntegerField(default=0)
    first_observed_at = models.DateTimeField(help_text="First time we observed this business.")
    last_observed_at = models.DateTimeField(db_index=True, help_text="Most recent observation.")
    registration_date = models.DateField(
        null=True,
        blank=True,
        help_text="From BRS/Gazette, when available.",
    )

    class Meta:
        ordering = ["-last_observed_at"]
        indexes = [
            models.Index(fields=["category", "location_cell"]),
            models.Index(fields=["brand"]),
            models.Index(fields=["current_status"]),
            models.Index(fields=["location_cell", "current_status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"

    def clean(self):
        validate_kenya_bounds(self.latitude, self.longitude)
        if self.category == "other" and not self.category_other:
            raise ValidationError({"category_other": "Required when category is 'other'."})
        if self.last_observed_at and self.first_observed_at and self.last_observed_at < self.first_observed_at:
            raise ValidationError("last_observed_at cannot be before first_observed_at.")

    @property
    def observed_age_days(self):
        """Lower bound on business age based on our observation window."""
        if self.first_observed_at and self.last_observed_at:
            return (self.last_observed_at - self.first_observed_at).days
        return None


class CategoryEnrichmentLog(TimeStampedModel):
    """
    One row per (location_cell, category) ever searched via Places. This is
    what services.needs_category_enrichment() / BusinessLocationCell.
    has_category_data() actually check -- tracking THAT and WHEN a
    category was searched, independent of BusinessEntity, because "zero
    competitors found" and "never searched" must be distinguishable
    states. Without this table, there was no way to represent "we
    checked, there genuinely are none" -- which is exactly the state a
    wide-open market actually looks like.

    result_count is informational (quick "how many did the last search
    turn up" without joining BusinessEntity) -- BusinessEntity rows
    themselves are the source of truth for what to show/score; this
    table only answers "is it time to search again".
    """
    location_cell = models.ForeignKey(
        BusinessLocationCell, on_delete=models.CASCADE, related_name="category_enrichments"
    )
    category = models.CharField(max_length=30, help_text="A BUSINESS_CATEGORY_CHOICES value, or 'anchors'.")
    fetched_at = models.DateTimeField()
    result_count = models.PositiveIntegerField(default=0)
    search_radius_m = models.PositiveIntegerField(default=1500)

    # Same staleness window Places-derived BusinessEntity data uses --
    # see STALENESS_DAYS_BY_SOURCE_CLASS. Kept as a plain property rather
    # than inheriting StaleAwareModel so this table isn't tied to a
    # source_class field it doesn't otherwise need.
    STALE_AFTER_DAYS = 30

    class Meta:
        unique_together = [("location_cell", "category")]
        indexes = [models.Index(fields=["location_cell", "category"])]

    def __str__(self):
        return f"{self.location_cell.geohash}/{self.category} @ {self.fetched_at:%Y-%m-%d}"

    @property
    def is_stale(self):
        age_days = (timezone.now() - self.fetched_at).days
        return age_days > self.STALE_AFTER_DAYS


class BusinessSnapshot(TimeStampedModel):
    """
    One observation of a BusinessEntity at one point in time. Append-only.
    Enables trend analysis (rating, review velocity, status changes).
    """
    business = models.ForeignKey(BusinessEntity, on_delete=models.CASCADE, related_name="snapshots")
    observed_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=20, choices=BUSINESS_STATUS_CHOICES)
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(5)],
    )
    review_count = models.PositiveIntegerField(default=0)
    source = models.CharField(max_length=50, default="google_places")

    class Meta:
        ordering = ["-observed_at"]
        indexes = [
            models.Index(fields=["business", "observed_at"]),
        ]
        constraints = [
            UniqueConstraint(
                fields=["business", "observed_at", "source"],
                name="uniq_snapshot_per_business_time_source",
            ),
        ]

    def __str__(self):
        return f"{self.business.name} @ {self.observed_at:%Y-%m-%d}: {self.status}"


class BusinessLifecycleEvent(TimeStampedModel):
    """
    Birth/death events for survival analysis. Critical: censored vs confirmed deaths
    are distinguished to avoid overestimating failure rates.
    """
    EVENT_TYPE_CHOICES = [
        ("opened", "Opened"),
        ("closed_confirmed", "Closed (confirmed by source)"),
        ("censored_last_seen", "Last seen (not confirmed closed – right-censored)"),
    ]

    business = models.ForeignKey(BusinessEntity, on_delete=models.CASCADE, related_name="lifecycle_events")
    event_type = models.CharField(max_length=25, choices=EVENT_TYPE_CHOICES)
    event_date = models.DateField()
    is_censored = models.BooleanField(
        default=False,
        help_text="True for censored_last_seen events. Survival code must check this.",
    )
    source = models.CharField(max_length=100, help_text="e.g. 'google_places', 'kenya_gazette', 'brs'.")
    evidence_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["event_date"]
        indexes = [
            models.Index(fields=["business", "event_type"]),
            models.Index(fields=["event_type", "is_censored"]),
        ]
        constraints = [
            CheckConstraint(
                check=(
                    (Q(event_type="closed_confirmed") & Q(is_censored=False)) |
                    (Q(event_type="censored_last_seen") & Q(is_censored=True)) |
                    (Q(event_type="opened") & Q(is_censored=False))
                ),
                name="event_type_consistent_with_censored_flag",
            ),
        ]

    def __str__(self):
        return f"{self.business.name}: {self.get_event_type_display()} on {self.event_date}"


# Filtered manager for confirmed closures (use this in survival models)
class ConfirmedClosuresManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(event_type="closed_confirmed", is_censored=False)


# Attach the filtered manager without replacing the default manager.
BusinessLifecycleEvent.add_to_class("confirmed_closures", ConfirmedClosuresManager())


# =============================================================================
# 4. Anchors – demand-generating facilities
# =============================================================================

ANCHOR_TYPE_CHOICES = [
    ("school", "School (primary/secondary, unspecified)"),
    ("primary_school", "Primary School"),
    ("secondary_school", "Secondary School"),
    ("university", "University"),
    ("hospital", "Hospital"),
    ("clinic_anchor", "Clinic (as anchor)"),
    ("shopping_mall", "Shopping Mall"),
    ("market", "Open-Air Market"),
    ("industrial_park", "Industrial Park"),
    ("major_employer", "Major Employer"),
    ("government_facility", "Government Facility"),
    ("transit_hub", "Transit Hub"),
    ("residential_development", "Residential Development"),
    ("religious_community", "Religious/Community Facility"),
]

# google_client.py has imported this name since it was written
# ("from .models import ... ANCHOR_PLACES_TYPES") but it never existed
# here -- meaning `import business_intel.google_client` raised
# ImportError unconditionally, which means EVERYTHING downstream of it
# (tasks.py, and therefore report generation entirely) could not run.
# This fixes the import; it does NOT fully fix search_anchors()'s own
# logic gap, which its own code comment already flags: these are real
# Places API "type" strings, but search_anchors() currently only calls
# search_places_category() for an anchor_type if that same string is
# ALSO a key in CATEGORY_PLACES_TYPE_MAP (which is keyed by BUSINESS
# category, e.g. "pharmacy" -- none of these anchor types are in it), so
# search_anchors() still returns [] for everything until that's
# addressed too. Left as-is, not fixed further, since fixing it properly
# means deciding how anchors sync into AnchorFacility (see
# BusinessLocationCell.has_category_data's docstring) -- a design
# decision, not a one-line patch.
ANCHOR_PLACES_TYPES = [
    "school", "university", "hospital", "shopping_mall", "market",
    "government_facility", "transit_station", "religious_community",
]


class AnchorFacility(StaleAwareModel, TimeStampedModel):
    """
    A demand-generating facility with size/capacity for gravity models.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    place_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    name = models.CharField(max_length=255)
    anchor_type = models.CharField(max_length=30, choices=ANCHOR_TYPE_CHOICES, db_index=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, validators=latitude_validators)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, validators=longitude_validators)
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="anchors",
    )
    size_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Enrollment, beds, employees, GLA, etc.",
    )
    size_unit = models.CharField(
        max_length=30,
        blank=True,
        help_text="e.g. 'enrollment', 'beds', 'employees', 'gla_sqm'.",
    )
    size_source = models.CharField(max_length=100, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["anchor_type", "location_cell"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_anchor_type_display()})"

    def clean(self):
        validate_kenya_bounds(self.latitude, self.longitude)
        if self.size_value is not None and not self.size_unit:
            raise ValidationError({"size_unit": "Required whenever size_value is set."})


# =============================================================================
# 5. Infrastructure Projects (Development + Infrastructure Change)
# =============================================================================

class InfrastructureProject(StaleAwareModel, TimeStampedModel):
    """
    Covers both development activity and infrastructure change.
    """
    PROJECT_TYPE_CHOICES = [
        ("residential", "Residential Development"),
        ("commercial", "Commercial Development"),
        ("industrial", "Industrial Development"),
        ("road", "Road"),
        ("bridge", "Bridge"),
        ("interchange", "Interchange"),
        ("rail", "Rail"),
        ("brt", "Bus Rapid Transit"),
        ("water", "Water Infrastructure"),
        ("sewerage", "Sewerage"),
        ("electricity", "Electricity Infrastructure"),
        ("fibre", "Fibre / Digital Infrastructure"),
    ]
    STATUS_CHOICES = [
        ("proposed", "Proposed"),
        ("approved", "Approved"),
        ("under_construction", "Under Construction"),
        ("completed", "Completed"),
        ("stalled", "Stalled"),
        ("cancelled", "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    project_type = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        validators=latitude_validators,
    )
    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        validators=longitude_validators,
    )
    geometry_geojson = models.JSONField(
        null=True,
        blank=True,
        help_text="For linear/area projects (roads, rail).",
    )
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="infrastructure_projects",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    completion_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    budget_kes = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    funding_source = models.CharField(max_length=100, blank=True)
    source = models.CharField(
        max_length=100,
        help_text="e.g. 'KeNHA', 'KURA', 'World Bank', 'County Government'.",
    )
    source_url = models.URLField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["project_type", "status"]),
            models.Index(fields=["location_cell", "project_type"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"

    def clean(self):
        if self.latitude is not None and self.longitude is not None:
            validate_kenya_bounds(self.latitude, self.longitude)
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("end_date cannot be before start_date.")
        if self.status == "completed" and self.completion_pct is not None and self.completion_pct != 100:
            raise ValidationError("completion_pct must be 100 for completed projects.")


# =============================================================================
# 6. Property Listings (Engine 15)
# =============================================================================

class PropertyListing(StaleAwareModel, TimeStampedModel):
    """
    Scraped/observed listing from property platforms.
    Historical listings are kept for price-growth calculations.
    """
    LISTING_TYPE_CHOICES = [("sale", "For Sale"), ("rent", "For Rent")]
    PROPERTY_TYPE_CHOICES = [
        ("residential", "Residential"),
        ("commercial", "Commercial"),
        ("land", "Land"),
        ("mixed_use", "Mixed Use"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_listing_id = models.CharField(max_length=255, blank=True, db_index=True)
    source = models.CharField(max_length=100, help_text="e.g. 'BuyRentKenya', 'Property24 Kenya'.")
    source_url = models.URLField(blank=True)
    listing_type = models.CharField(max_length=10, choices=LISTING_TYPE_CHOICES)
    property_type = models.CharField(max_length=20, choices=PROPERTY_TYPE_CHOICES)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, validators=latitude_validators)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, validators=longitude_validators)
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="property_listings",
    )
    price_kes = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="For sale listings.",
    )
    monthly_rent_kes = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="For rent listings.",
    )
    floor_area_sqm = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    listed_at = models.DateField()
    delisted_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["location_cell", "listing_type", "property_type"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            CheckConstraint(
                check=(
                    Q(listing_type="sale", price_kes__isnull=False) |
                    Q(listing_type="rent", monthly_rent_kes__isnull=False)
                ),
                name="listing_has_relevant_price_field",
            ),
        ]

    def __str__(self):
        return f"{self.get_listing_type_display()} listing @ {self.location_cell_id or (self.latitude, self.longitude)}"

    def clean(self):
        validate_kenya_bounds(self.latitude, self.longitude)
        if self.listing_type == "sale" and self.price_kes is None:
            raise ValidationError({"price_kes": "Required for sale listings."})
        if self.listing_type == "rent" and self.monthly_rent_kes is None:
            raise ValidationError({"monthly_rent_kes": "Required for rent listings."})
        if self.delisted_at and self.listed_at and self.delisted_at < self.listed_at:
            raise ValidationError("delisted_at cannot be before listed_at.")
        if self.is_active and self.delisted_at:
            raise ValidationError("An active listing cannot have delisted_at set.")

    @property
    def price_per_sqm(self):
        if self.price_kes is not None and self.floor_area_sqm:
            return self.price_kes / self.floor_area_sqm
        return None

    @property
    def rent_per_sqm(self):
        if self.monthly_rent_kes is not None and self.floor_area_sqm:
            return self.monthly_rent_kes / self.floor_area_sqm
        return None


# =============================================================================
# 7. Requester, Query, Report – the user-facing pipeline
# =============================================================================

class Requester(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="business_intel_requester_profile",
    )
    last_active_at = models.DateTimeField(auto_now=True)

    free_reports_remaining = models.PositiveSmallIntegerField(default=5)
    free_reports_used_total = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-last_active_at"]

    def __str__(self):
        return self.email

    def clean(self):
        if self.email:
            self.email = self.email.strip().lower()

    def consume_free_report(self):
        updated = Requester.objects.filter(
            pk=self.pk, free_reports_remaining__gt=0
        ).update(
            free_reports_remaining=F("free_reports_remaining") - 1,
            free_reports_used_total=F("free_reports_used_total") + 1,
        )
        if updated:
            self.refresh_from_db(fields=["free_reports_remaining", "free_reports_used_total"])
            return True
        return False


class BusinessQuery(TimeStampedModel):
    """
    One exact 'should I open X here' question.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_location_input = models.TextField()
    latitude = models.DecimalField(max_digits=10, decimal_places=7, validators=latitude_validators)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, validators=longitude_validators)
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.PROTECT,
        related_name="queries",
        help_text="The cell this query is about.",
    )
    business_category = models.CharField(max_length=30, choices=BUSINESS_CATEGORY_CHOICES)
    business_category_other = models.CharField(max_length=100, blank=True)
    investment_amount_kes = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    branches_planned = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    target_segment = models.CharField(
        max_length=20,
        blank=True,
        choices=[("budget", "Budget"), ("mid_market", "Mid-market"), ("premium", "Premium")],
    )
    requester = models.ForeignKey(Requester, on_delete=models.PROTECT, related_name="queries")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
            models.Index(fields=["location_cell", "business_category"]),
        ]

    def __str__(self):
        return f"{self.get_business_category_display()} @ ({self.latitude}, {self.longitude})"

    def clean(self):
        validate_kenya_bounds(self.latitude, self.longitude)
        if self.business_category == "other" and not self.business_category_other:
            raise ValidationError({"business_category_other": "Required when business_category is 'other'."})


class BusinessReport(TimeStampedModel):
    """
    One generated report for one query. This is a frozen snapshot of the
    decision output, with references to the FeatureObservation rows used.
    """
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("awaiting_payment", "Awaiting payment"),
        ("generating", "Generating"),
        ("ready", "Ready"),
        ("failed", "Failed"),
    ]
    RECOMMENDATION_CHOICES = [
        ("proceed", "Proceed"),
        ("investigate_further", "Investigate Further"),
        ("avoid", "Avoid"),
    ]
    OPPORTUNITY_CHOICES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
    TIMING_CHOICES = [("early", "Early"), ("mature", "Mature"), ("late", "Late")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    query = models.ForeignKey(BusinessQuery, on_delete=models.CASCADE, related_name="reports")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    failure_reason = models.TextField(blank=True)
    pdf_storage_path = models.CharField(max_length=500, blank=True)
    pdf_generated_at = models.DateTimeField(null=True, blank=True)

    # Output contract (per spec section 6)
    opportunity_level = models.CharField(max_length=10, choices=OPPORTUNITY_CHOICES, blank=True)
    evidence = models.JSONField(default=list, blank=True, help_text="List of evidence strings.")
    risks = models.JSONField(default=list, blank=True)
    market_gap_summary = models.TextField(blank=True)
    competition_summary = models.TextField(blank=True)
    momentum_summary = models.TextField(blank=True)
    timing = models.CharField(max_length=10, choices=TIMING_CHOICES, blank=True)
    confidence_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Aggregate confidence across all features (0-100).",
    )
    recommendation = models.CharField(max_length=25, choices=RECOMMENDATION_CHOICES, blank=True)
    ai_summary_text = models.TextField(blank=True)

    # Freeze the exact feature versions used
    feature_snapshot_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="List of FeatureObservation primary keys used to produce this report.",
    )

    # --- ADDITION: Anomaly Detection (Engine 27) fields ---
    anomaly_risk_score = models.FloatField(
        default=0.0,
        help_text="Ensemble unsupervised ML score (0-100). Higher = more anomalous business pattern."
    )
    anomaly_confidence = models.FloatField(
        default=0.0,
        help_text="Confidence in the anomaly detection (0-100) based on ensemble consistency."
    )
    is_anomalous = models.BooleanField(
        default=False,
        help_text="Flag if location deviates statistically from normal business patterns (Engine 27)."
    )
    # --- END ADDITION ---

    # Payment
    is_paid = models.BooleanField(default=False)
    price_charged_kes = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    paystack_reference = models.CharField(max_length=100, blank=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["query", "status"]),
            models.Index(fields=["is_paid", "status"]),
        ]
        constraints = [
            CheckConstraint(
                check=(
                    Q(status="ready") & Q(recommendation__isnull=False) |
                    Q(~Q(status="ready"))
                ),
                name="ready_report_must_have_recommendation",
            ),
            CheckConstraint(
                check=(
                    Q(status="failed") & Q(failure_reason__gt="") |
                    Q(~Q(status="failed"))
                ),
                name="failed_report_must_have_reason",
            ),
            CheckConstraint(
                check=(
                    Q(is_paid=True) & Q(paid_at__isnull=False) |
                    Q(is_paid=False)
                ),
                name="paid_report_must_have_paid_at",
            ),
        ]

    def __str__(self):
        return f"Report {self.id} - {self.get_status_display()}"

    def clean(self):
        if self.status == "ready" and not self.recommendation:
            raise ValidationError("A 'ready' report must have a recommendation set.")
        if self.status == "failed" and not self.failure_reason:
            raise ValidationError("A 'failed' report must record failure_reason.")
        if self.is_paid and not self.paid_at:
            raise ValidationError({"paid_at": "Required when is_paid=True."})


# =============================================================================
# 8. API / Data Source Call Log (provenance & cost tracking)
# =============================================================================

class BusinessAPICallLog(TimeStampedModel):
    """
    One row per external request actually sent (not per cache hit).
    Covers APIs, scrapes, and manual entries.
    """
    API_CHOICES = [
        ("places_nearby", "Places API (New) - Nearby Search"),
        ("places_text", "Places API (New) - Text Search"),
        ("routes", "Routes API (traffic-aware duration)"),
        ("geocoding", "Geocoding API"),
        ("osm_overpass", "OSM Overpass"),
        ("worldpop_download", "WorldPop raster download"),
        ("viirs_download", "VIIRS night lights download"),
        ("knbs_portal", "KNBS Data Portal"),
        ("scrape_property", "Property listing scrape (BuyRentKenya/Property24)"),
        ("scrape_gazette", "Kenya Gazette scrape"),
        ("scrape_county", "County dashboard scrape"),
        ("brs_request", "BRS data request (manual/partnership)"),
        ("manual_entry", "Manual data entry"),
    ]

    api = models.CharField(max_length=30, choices=API_CHOICES, db_index=True)
    location_cell = models.ForeignKey(
        BusinessLocationCell,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_calls",
    )
    category = models.CharField(max_length=30, blank=True)
    request_params = models.JSONField(default=dict, blank=True)
    response_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)
    estimated_cost_usd = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        default=0,
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["api", "created_at"]),
            models.Index(fields=["location_cell", "api"]),
        ]

    def __str__(self):
        return f"{self.get_api_display()} - {'OK' if self.succeeded else 'FAILED'} ({self.created_at:%Y-%m-%d %H:%M})"

    def clean(self):
        if self.succeeded and self.response_status_code and not (200 <= self.response_status_code < 300):
            raise ValidationError("succeeded=True is inconsistent with a non-2xx response_status_code.")


"""
ADDITION to business_intel/models.py -- append this block (near
CategoryEnrichmentLog, which it deliberately mirrors).

WHY THIS IS NEEDED: compute_development_activity_score() currently issues
a live Overpass request on every call with no cache check at all -- every
repeat report for the same cell re-hits a shared public Overpass mirror
(overpass-api.de), which is rate-limited and not something this system
controls. CategoryEnrichmentLog already solved exactly this problem for
Places category search ("is it time to search again", not "did we ever
search"); this is the same table shape, one row per (cell, purpose,
radius), for Overpass instead of Places. Additive only -- nothing existing
is modified.
"""

class OSMOverpassQueryCache(TimeStampedModel):
    """
    One row per (location_cell, purpose, radius_m) Overpass query ever
    issued. Caches the raw `elements` list so repeat report runs for the
    same cell don't re-hit the shared overpass-api.de instance -- same
    "cache unit, staleness governs re-fetch" discipline
    CategoryEnrichmentLog already applies to Places.

    Storing the raw elements (not a derived metric) here deliberately --
    this is a transport-layer cache, not a FeatureObservation. Engines
    still compute their own derived numbers from `elements_json` fresh
    each time they read it, so a cache hit here is invisible to the
    statistics layer.
    """
    location_cell = models.ForeignKey(
        BusinessLocationCell, on_delete=models.CASCADE, related_name="osm_overpass_queries"
    )
    purpose = models.CharField(max_length=50, help_text="e.g. 'development_activity', 'amenity_verify'.")
    radius_m = models.PositiveIntegerField()
    fetched_at = models.DateTimeField()
    succeeded = models.BooleanField(default=False)
    elements_json = models.JSONField(
        default=list, blank=True, help_text="Raw Overpass `elements` array from the last successful fetch."
    )
    element_count = models.PositiveIntegerField(default=0)

    # Same window class as internal_derived data (STALENESS_DAYS_BY_SOURCE_CLASS) --
    # physical construction activity does not change hour to hour, and this
    # keeps cache re-fetch cadence consistent with every other cached-external-call
    # table in this codebase rather than inventing a bespoke number.
    STALE_AFTER_DAYS = STALENESS_DAYS_BY_SOURCE_CLASS["internal_derived"]

    class Meta:
        unique_together = [("location_cell", "purpose", "radius_m")]
        indexes = [models.Index(fields=["location_cell", "purpose", "radius_m"])]

    def __str__(self):
        return f"{self.location_cell.geohash}/{self.purpose}@{self.radius_m}m ({self.element_count} elements)"

    @property
    def is_stale(self):
        age_days = (timezone.now() - self.fetched_at).days
        return age_days > self.STALE_AFTER_DAYS


# =============================================================================
# ADDITION to business_intel/models.py -- ADDITIVE ONLY, same convention
# development_activity.py's OSMOverpassQueryCache addition already
# established. Insert this class anywhere after BusinessLocationCell is
# defined (it FKs to it) -- placing it directly after OSMOverpassQueryCache
# keeps all "transport-layer cache" tables grouped together.
# =============================================================================

class VIIRSAnnualRadianceCache(TimeStampedModel):
    """
    One row per (location_cell, year, radius_m) VIIRS annual-composite
    radiance fetch ever issued. Caches the derived (mean_radiance_masked,
    pixel_count, cloud_free_obs_mean) triple -- NOT a raw raster window --
    since a raw GEE raster crop is not something this system has any
    other use for once the buffer-mean is computed, unlike
    OSMOverpassQueryCache's raw `elements` (which the engine re-derives
    metrics from fresh on every read). Re-fetching and re-averaging an
    identical (cell, year, radius) window would waste a real
    Earth-Engine compute-pixels call for a number that cannot change
    (a past year's published annual composite is immutable once EOG
    releases it) -- so, unlike OSMOverpassQueryCache, a SUCCEEDED row
    here never expires. Only a FAILED row (succeeded=False) is retried on
    next access, since "we could not reach VIIRS this time" is a
    transient state, not a fact about the world.
    """
    location_cell = models.ForeignKey(
        BusinessLocationCell, on_delete=models.CASCADE, related_name="viirs_annual_radiance_queries"
    )
    year = models.PositiveSmallIntegerField()
    radius_m = models.PositiveIntegerField()
    fetched_at = models.DateTimeField()
    succeeded = models.BooleanField(default=False)
    mean_radiance_masked = models.FloatField(
        null=True, blank=True,
        help_text="Mean of the VIIRS annual composite's average_masked/median_masked band over the buffer, nW/cm2/sr.",
    )
    pixel_count = models.PositiveIntegerField(
        default=0, help_text="Number of ~463.83m VIIRS pixels intersecting the buffer -- 0 or 1 is common at this system's cell scale.",
    )
    cloud_free_obs_mean = models.FloatField(
        null=True, blank=True,
        help_text="Mean of the composite's cf_cvg band over the buffer -- how many cloud-free nightly observations backed this year's value here.",
    )
    error_message = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = [("location_cell", "year", "radius_m")]
        indexes = [models.Index(fields=["location_cell", "year", "radius_m"])]

    def __str__(self):
        return f"{self.location_cell.geohash}/{self.year}@{self.radius_m}m ({'ok' if self.succeeded else 'failed'})"

    @property
    def is_retryable(self):
        """A failed fetch is always worth retrying on next access -- past
        VIIRS composites are static once published, so there is no
        staleness WINDOW the way OSMOverpassQueryCache.is_stale has one;
        the only question is whether we ever successfully got the value.
        """
        return not self.succeeded

# =============================================================================
# 9. Administrative Units and Census Population
# =============================================================================

class AdminUnit(TimeStampedModel):
    """
    One row per administrative unit (county or sub-county) with KNBS codes.
    Used by the population engine and the census data loader.
    """
    ADMIN_TYPE_CHOICES = [
        ('county', 'County'),
        ('sub_county', 'Sub-county'),
    ]

    code = models.CharField(
        max_length=10,
        unique=True,
        db_index=True,
        help_text="KNBS county code (e.g. '01') or sub-county code (e.g. '101').",
    )
    name = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Official KNBS name.",
    )
    admin_type = models.CharField(
        max_length=15,
        choices=ADMIN_TYPE_CHOICES,
        db_index=True,
    )
    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='children',
        help_text="Parent county for sub-counties; null for counties.",
    )
    centroid_lat = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        validators=latitude_validators,
        help_text="From CountyGPS.csv.",
    )
    centroid_lng = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        validators=longitude_validators,
    )

    class Meta:
        ordering = ['admin_type', 'name']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['name', 'admin_type']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_admin_type_display()})"


class PopulationEstimate(TimeStampedModel):
    """
    One row per (admin_unit, year) with census or intercensal estimates.
    """
    admin_unit = models.ForeignKey(
        AdminUnit,
        on_delete=models.CASCADE,
        related_name='population_estimates',
    )
    year = models.PositiveSmallIntegerField()
    total = models.PositiveIntegerField()
    male = models.PositiveIntegerField()
    female = models.PositiveIntegerField()
    intersex = models.PositiveIntegerField(default=0)
    households = models.PositiveIntegerField(null=True, blank=True)
    avg_household_size = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        null=True,
        blank=True,
    )
    land_area_sqkm = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    density_per_sqkm = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        unique_together = [['admin_unit', 'year']]
        ordering = ['-year', 'admin_unit__name']
        indexes = [
            models.Index(fields=['admin_unit', 'year']),
            models.Index(fields=['year']),
        ]

    def __str__(self):
        return f"{self.admin_unit.name} ({self.year}): {self.total}"