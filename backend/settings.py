import os
from pathlib import Path
import environ

# ─── Base directory ───────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Load environment variables ──────────────────────────────────────
# Locally: reads from a .env file in the project root (gitignored).
# On Render (and any platform that injects env vars directly): no .env
# file exists on disk — that's expected, not an error. environ.Env()
# still reads from the real process environment either way.
env = environ.Env()
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

# ─── Django core ──────────────────────────────────────────────────────
SECRET_KEY = env("SECRET_KEY")  # no default — must be set, locally or on Render
DEBUG = env.bool("DEBUG", default=False)

# ✅ FIX: Render sets RENDER=true automatically on every deployed service
# (https://render.com/docs/environment-variables#all-runtimes). Use THIS,
# not DEBUG, to gate anything that should only ever apply on the real
# deployed server (HTTPS redirect, secure cookies, HSTS). DEBUG is easy
# to get wrong locally (e.g. a .env copied from Render for testing some
# other var) — if that happens, ALLOWED_HOSTS/CORS still degrade safely,
# but SECURE_SSL_REDIRECT does not: it forces every request, including
# the browser's CORS preflight OPTIONS request, from http:// to https://.
# runserver doesn't speak https, and browsers refuse to follow a redirect
# on a preflight at all — so EVERY API call fails with a CORS error that
# has nothing to do with CORS. Keying off RENDER instead means this can
# only ever fire on the actual deployed service, never locally by accident.
IS_RENDER = env.bool("RENDER", default=False)

# ✅ FIX: Always include localhost and 127.0.0.1, even if the env var overrides.
# We read the env list, then merge with a base list so we never lose local dev access.
_BASE_ALLOWED_HOSTS = ["localhost", "127.0.0.1", ".scapedatasolutions.com"]
_ENV_ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
ALLOWED_HOSTS = list(set(_BASE_ALLOWED_HOSTS + _ENV_ALLOWED_HOSTS))

# If DEBUG is True, allow any host (convenience for dev).
if DEBUG:
    ALLOWED_HOSTS = ["*"]

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[
        "http://localhost:3000",
        "http://localhost:5173",
        "https://scapedatasolutions.com",
        "https://www.scapedatasolutions.com",
    ],
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "daphne",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "anymail",
    "visitors",
    "leads",
    "enrichment",
    "jobs",
    "property_intel",
    "payments",
    "users",
    "agreements",
    "contracts",
    "projects",  # ← NEW: client project submission + real-time messaging
    "business_intel",
    "channels",
    "rest_framework_simplejwt.token_blacklist",
]

# ─── Middleware ──────────────────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",   # for static files in production
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "visitors.middleware.VisitorTrackingMiddleware",
]

ROOT_URLCONF = "backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "backend.wsgi.application"
ASGI_APPLICATION = "backend.asgi.application"

# ─── Channels (websockets — contracts + projects live messaging) ────
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [env("CHANNEL_LAYERS_REDIS_URL", default="redis://localhost:6379/1")],
        },
    },
}

# ─── Database – local PostgreSQL (dev) ─────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST"),
        "PORT": env("DB_PORT", default="5432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"sslmode": "require", "prepare_threshold": None},
    }
}

# ─── Password validation ─────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── Internationalisation ────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ─── Static files ────────────────────────────────────────────────────
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# ─── Media files (resume uploads, contract docs, project attachments) ─
# Local disk for now. NOTE: Render's filesystem is ephemeral — anything
# written here is wiped on every deploy/restart unless a persistent
# Disk add-on is attached to the service. Fine for local dev; revisit
# before relying on this in production. (property_intel already moved
# its own uploads to Supabase Storage for exactly this reason — see
# property_intel/storage.py. contracts/signed docs and projects/
# attachments have NOT been moved yet; same ephemeral-disk risk applies
# to both until they are.)
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── CORS – only your real frontend origins ─────────────────────────
# Local dev: accept any frontend origin when DEBUG. Production keeps
# the explicit allowlist below since DEBUG defaults to False there.
#
# ✅ FIX: Same class of bug as ALLOWED_HOSTS above. FRONTEND_ORIGINS is
# set in your .env (copied from Render, pointed at the prod domain),
# and env.list(..., default=[...]) only uses the default when the var
# is completely UNSET — if it's set to anything, it REPLACES the list
# entirely, so localhost:5173/3000 silently fell out of the allowlist
# whenever FRONTEND_ORIGINS was present. Now we always merge the env
# value into a base local-dev list so local origins can never be
# dropped by a prod-flavored .env.
CORS_ALLOW_ALL_ORIGINS = DEBUG
_BASE_CORS_ORIGINS = ["http://localhost:5173", "http://localhost:3000"]
_ENV_CORS_ORIGINS = env.list("FRONTEND_ORIGINS", default=[])
CORS_ALLOWED_ORIGINS = list(set(_BASE_CORS_ORIGINS + _ENV_CORS_ORIGINS))
CORS_ALLOW_CREDENTIALS = True

# ─── Email (Brevo via Anymail HTTP API — no SMTP, works reliably on Render) ──
EMAIL_BACKEND = "anymail.backends.brevo.EmailBackend"
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default="Scape Data Solutions <info@scapedatasolutions.com>"
)
WELCOME_FROM_EMAIL = env(
    "WELCOME_FROM_EMAIL", default="Scape Data Solutions <noreply@scapedatasolutions.com>"
)
REPLY_TO_EMAIL = env("REPLY_TO_EMAIL", default="info@scapedatasolutions.com")

ANYMAIL = {
    "BREVO_API_KEY": env("BREVO_API_KEY", default=""),
}

# ─── Brevo API (contact sync) ──────────────────────────────────────
BREVO_API_KEY = env("BREVO_API_KEY", default="")
BREVO_CONTACT_LIST_ID = env.int("BREVO_CONTACT_LIST_ID", default=2)

# ─── Notification recipients ────────────────────────────────────────
ADMIN_NOTIFICATION_EMAILS = env.list(
    "ADMIN_NOTIFICATION_EMAILS", default=["info@scapedatasolutions.com"]
)

SITE_DOMAIN = env("SITE_DOMAIN", default="https://scapedatasolutions.com")

# ─── DRF ────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": (
        ["rest_framework.renderers.JSONRenderer"]
        if not DEBUG
        else [
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        ]
    ),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/minute",
        "contact": "5/minute",
        "track": "60/minute",
        "property_pin": "20/minute",
        "property_otp": "5/minute",
        "payments_initialize": "10/minute",
        "project_submit": "10/minute",    # ← NEW: throttles POST /api/projects/
        "project_message": "60/minute",   # ← NEW: throttles project message-send endpoints
        "business_intel_submit": "10/minute",
    },
}

# ─── Security ────────────────────────────────────────────────────────
# ✅ FIX: was `if not DEBUG:` — now gated on IS_RENDER (see comment near
# ALLOWED_HOSTS above). Only the actual deployed Render service ever
# gets SECURE_SSL_REDIRECT etc.; a mis-set DEBUG locally can't trigger
# it anymore.
if IS_RENDER:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_BROWSER_XSS_FILTER = True

# ─── Logging ────────────────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "visitors": {"level": "INFO", "propagate": True},
        "leads": {"level": "INFO", "propagate": True},
        "enrichment": {"level": "INFO", "propagate": True},
        "jobs": {"level": "INFO", "propagate": True},
        "contracts": {"level": "INFO", "propagate": True},
        "projects": {"level": "INFO", "propagate": True},  # ← NEW
    },
}
# ─── Gmail SMTP (resume attachments ONLY — separate from Brevo) ─────
# Brevo remains EMAIL_BACKEND / DEFAULT for everything else (leads,
# applicant confirmations). This is a standalone connection used only
# in jobs/email.py to get resume files into a Gmail inbox directly.
GMAIL_SMTP_USER = env("GMAIL_SMTP_USER", default="")
GMAIL_SMTP_APP_PASSWORD = env("GMAIL_SMTP_APP_PASSWORD", default="")
GMAIL_RESUME_RECIPIENT = env("GMAIL_RESUME_RECIPIENT", default="scapedatasolutions@gmail.com")


# ─── Celery ─────────────────────────────────────────────────────────
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BEAT_SCHEDULE = {
    "sweep-stuck-property-reports": {
        "task": "property_intel.tasks.sweep_stuck_reports",
        "schedule": 300.0,
    },
    "purge-expired-property-reports": {
        "task": "property_intel.tasks.purge_expired_reports",
        "schedule": 86400.0,  # once a day
    },
    "sweep-stuck-business-reports": {
        "task": "business_intel.tasks.sweep_stuck_reports",
        "schedule": 300.0,  # every 5 minutes
    },
}

# ─── Paystack ───────────────────────────────────────────────────────
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_CALLBACK_URL = env("PAYSTACK_CALLBACK_URL", default="")
PROPERTY_REPORT_PRICE_KES = env.int("PROPERTY_REPORT_PRICE_KES", default=300)

# ─── Google Maps Platform ─────────────────────────────────────────────
GOOGLE_MAPS_API_KEY = env("GOOGLE_MAPS_API_KEY", default="")

# Dev-only bypass for slow/unreachable OSM Overpass calls -- see
# google_client.query_osm_overpass()'s own comment. Defaults to False
# so production is unaffected unless explicitly set in that env's .env.
SKIP_OSM_OVERPASS_LOCALLY = env.bool("SKIP_OSM_OVERPASS_LOCALLY", default=False)

# ─── Africa's Talking (SMS OTP) ────────────────────────────────────
AFRICASTALKING_USERNAME = env("AFRICASTALKING_USERNAME", default="")
AFRICASTALKING_API_KEY = env("AFRICASTALKING_API_KEY", default="")
AFRICASTALKING_SENDER_ID = env("AFRICASTALKING_SENDER_ID", default="")

# ─── Supabase Storage ───────────────────────────────────────────────
SUPABASE_URL = env("SUPABASE_URL", default="")
SUPABASE_SERVICE_KEY = env("SUPABASE_SERVICE_KEY", default="")
SUPABASE_STORAGE_BUCKET = env("SUPABASE_STORAGE_BUCKET", default="property-intel")

# ─── Users app ──────────────────────────────────────────────────────
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", default="http://localhost:5173")
BREVO_VERIFICATION_TEMPLATE_ID = env.int("BREVO_VERIFICATION_TEMPLATE_ID", default=1)

# ─── Resume parsing (jobs app) ──────────────────────────────────────
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")

# ─── Enrichment (Clearbit, optional) ─────────────────────────────────
CLEARBIT_API_KEY = env("CLEARBIT_API_KEY", default="")

# ─── Privacy policy version (jobs app) ────────────────────────────────
PRIVACY_POLICY_VERSION = env("PRIVACY_POLICY_VERSION", default="1.0")

# ─── JWT auth (users app login) ──────────────────────────────────────
from datetime import timedelta  # noqa: E402

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
}

# ─── Projects app (client-submitted work requests) ──────────────────
# Max size for a single project attachment upload, enforced in
# projects/serializers.py before it's ever sent to storage.
PROJECT_ATTACHMENT_MAX_SIZE_MB = env.int("PROJECT_ATTACHMENT_MAX_SIZE_MB", default=15)

# Supabase bucket project attachments are uploaded to (see
# projects/storage.py). Separate from SUPABASE_STORAGE_BUCKET
# (property_intel's bucket) so the two apps' files don't mix. Create
# this bucket in your Supabase project (Storage → New bucket → public)
# before shipping — uploads will fail with a clear 502 until it exists.
PROJECTS_STORAGE_BUCKET = env("PROJECTS_STORAGE_BUCKET", default="project-attachments")

# Emails that skip fingerprinting/fraud scoring/payment entirely and get
# unlimited free reports. Comma-separated in env; falls back to a default
# so this doesn't silently disappear if the env var is unset.
ADMIN_BYPASS_EMAILS = {
    e.strip().lower()
    for e in os.environ.get(
        "ADMIN_BYPASS_EMAILS", "writers.smith112@gmail.com"
    ).split(",")
    if e.strip()
}
# ─── Invoicing — company + payment method details for generated PDFs ───
# All PLACEHOLDER values below — replace with real numbers before any
# invoice goes to a real client. No KRA/tax field per product decision;
# clients are Kenyan and pan-African, so payment methods deliberately
# span local (bank, M-Pesa Paybill) and cross-border rails rather than
# assuming one country's banking system.
INVOICE_COMPANY_NAME = env("INVOICE_COMPANY_NAME", default="Scape Data Solutions")
INVOICE_COMPANY_ADDRESS = env("INVOICE_COMPANY_ADDRESS", default="Nairobi, Kenya")
INVOICE_COMPANY_EMAIL = env("INVOICE_COMPANY_EMAIL", default="billing@scapedata.example")
INVOICE_COMPANY_PHONE = env("INVOICE_COMPANY_PHONE", default="+254 700 000 000")

INVOICE_PAYMENT_METHODS = [
    {"label": "Bank transfer (KES)", "lines": ["Bank: PLACEHOLDER BANK", "Account name: Scape Data Solutions", "Account number: 0000000000", "Branch/SWIFT: PLACEHOLDER"]},
    {"label": "M-Pesa Paybill", "lines": ["Paybill: 000000", "Account number: use the Invoice Number below"]},
    {"label": "Payoneer", "lines": ["Payoneer customer ID: PLACEHOLDER", "Or send to: billing@scapedata.example"]},
    {"label": "PayPal", "lines": ["paypal.me/PLACEHOLDER", "Or: billing@scapedata.example"]},
    {"label": "Wise (TransferWise)", "lines": ["Wise account: billing@scapedata.example", "Reference: use the Invoice Number below"]},
    {"label": "Remitly", "lines": ["Recipient: Scape Data Solutions", "Contact billing@scapedata.example for send details"]},
    {"label": "Sendwave", "lines": ["Recipient: Scape Data Solutions", "Contact billing@scapedata.example for send details"]},
    {"label": "Crypto (USDT)", "lines": ["Network: TRC20", "Wallet address: PLACEHOLDER_WALLET_ADDRESS"]},
]

# ─── Invoicing — sequential numbering ───────────────────────────────────
# "SCP-2026-000123"-style. See contracts/services._next_invoice_number()
# for the race-safe InvoiceCounter it drives.
INVOICE_NUMBER_PREFIX = env("INVOICE_NUMBER_PREFIX", default="SCP")
INVOICE_DEFAULT_DUE_DAYS = env.int("INVOICE_DEFAULT_DUE_DAYS", default=14)


# ── Multi-app signup routing (users/app_sources.py) ─────────────────────────
# One account system, several front-ends. Each app gets its own base URL so
# verification / welcome / reset emails send people back to THE APP THEY USED.
# Set these in your .env — they fall back to FRONTEND_BASE_URL until you do.
import os as _os
_default_front = globals().get("FRONTEND_BASE_URL", "")
APP_BASE_URLS = {
    "property": _os.environ.get("PROPERTY_APP_BASE_URL", _default_front),
    "portal": _os.environ.get("PORTAL_BASE_URL", _default_front),
    "thesis": _os.environ.get("THESIS_BASE_URL", _default_front),
}
# Per-app front-end routes. Adjust to match each app's router.
APP_PATHS = {
    "property": {"verify": "/verify-email", "reset": "/reset-password", "dashboard": "/dashboard"},
    "portal":   {"verify": "/verify-email", "reset": "/reset-password", "dashboard": "/dashboard"},
    "thesis":   {"verify": "/verify-email", "reset": "/reset-password", "dashboard": "/dashboard"},
}


# ── Scape multi-app registry (users/app_registry.py) ────────────────────────
# One account system, many front-ends. Each app: its own URL, routes and brand,
# so verification / welcome / reset emails return people to THE APP THEY USED.
# ADD A NEW APP = add one entry below. Set the env vars in .env; until you do,
# they fall back to FRONTEND_BASE_URL and `manage.py check` warns you.
import os as _os


def _scape_origins(env_name):
    return [o.strip() for o in _os.environ.get(env_name, "").split(",") if o.strip()]


_scape_front = globals().get("FRONTEND_BASE_URL", "")
SCAPE_APPS = {
    "property": {
        "name": "Scape Property Intelligence",
        "base_url": _os.environ.get("PROPERTY_APP_BASE_URL", _scape_front),
        "extra_origins": _scape_origins("PROPERTY_APP_EXTRA_ORIGINS"),   # e.g. www., staging, http://localhost:5173
        "paths": {"verify": "/verify-email", "reset": "/reset-password", "dashboard": "/dashboard", "login": "/login"},
        "show_free_reports": True,
    },
    "portal": {
        "name": "Scape Client Portal",
        "base_url": _os.environ.get("PORTAL_BASE_URL", _scape_front),
        "extra_origins": _scape_origins("PORTAL_EXTRA_ORIGINS"),
        "paths": {"verify": "/verify-email", "reset": "/reset-password", "dashboard": "/dashboard", "login": "/login"},
    },
    "thesis": {
        "name": "Scape Thesis",
        "base_url": _os.environ.get("THESIS_BASE_URL", _scape_front),
        "extra_origins": _scape_origins("THESIS_EXTRA_ORIGINS"),
        "paths": {"verify": "/verify-email", "reset": "/reset-password", "dashboard": "/dashboard", "login": "/login"},
    },
}
SCAPE_DEFAULT_APP = "property"            # used only when nothing identifies the calling app
SCAPE_RESEND_COOLDOWN_SECONDS = 60        # per-account throttle on resend-verification / password-reset emails


# Client Portal serves all its pages under /portal/
SCAPE_APPS["portal"]["paths"] = {
    "verify": "/portal/verify-email",
    "reset": "/portal/reset-password",
    "dashboard": "/portal/dashboard",
    "login": "/portal/login",
}
