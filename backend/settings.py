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
ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", ".scapedatasolutions.com"],
)
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

# ─── Database – local PostgreSQL (dev) ─────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST"),
        "PORT": env("DB_PORT", default="5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"sslmode": "require"},
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

# ─── Media files (resume uploads) ────────────────────────────────────
# Local disk for now. NOTE: Render's filesystem is ephemeral — anything
# written here is wiped on every deploy/restart unless a persistent
# Disk add-on is attached to the service. Fine for local dev; revisit
# before relying on this in production.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── CORS – only your real frontend origins ─────────────────────────
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env.list(
    "FRONTEND_ORIGINS",
    default=["http://localhost:5173", "http://localhost:3000"],
)
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
    },
}

# ─── Security (only matters once DEBUG=False) ──────────────────────
if not DEBUG:
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
}

# ─── Paystack ───────────────────────────────────────────────────────
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_CALLBACK_URL = env("PAYSTACK_CALLBACK_URL", default="")
PROPERTY_REPORT_PRICE_KES = env.int("PROPERTY_REPORT_PRICE_KES", default=300)

# ─── Google Maps Platform ─────────────────────────────────────────────
GOOGLE_MAPS_API_KEY = env("GOOGLE_MAPS_API_KEY", default="")

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
