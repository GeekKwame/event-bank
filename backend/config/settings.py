import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG")
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSECURE_SECRET_KEYS = {"", "change-me", "dev-insecure-secret-key"}
MIN_PRODUCTION_SECRET_KEY_LENGTH = 50

if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set")
if not DEBUG:
    if SECRET_KEY in INSECURE_SECRET_KEYS or len(SECRET_KEY) < MIN_PRODUCTION_SECRET_KEY_LENGTH:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is a placeholder or shorter than "
            f"{MIN_PRODUCTION_SECRET_KEY_LENGTH} characters; refusing to start with DEBUG off"
        )
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set when DEBUG is off")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.common",
    "apps.sources",
    "apps.events",
    "apps.ingestion",
    "apps.subscriptions",
    "apps.notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files (the admin's CSS/JS) when DEBUG is off.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "event_bank"),
        "USER": os.environ.get("POSTGRES_USER", "event_bank"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Africa/Accra")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "3600"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    if env_bool("DJANGO_BEHIND_TLS_PROXY"):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Every API view must declare its own permission_classes; anything that forgets is denied.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["apps.common.permissions.DenyAll"],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("API_THROTTLE_ANON", "60/min"),
        "user": os.environ.get("API_THROTTLE_USER", "600/min"),
        "health": os.environ.get("API_THROTTLE_HEALTH", "120/min"),
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    # Number of trusted reverse proxies in front of the app; throttling then uses the client IP
    # from X-Forwarded-For instead of the proxy's address. Leave unset without a proxy.
    "NUM_PROXIES": int(os.environ["API_NUM_PROXIES"])
    if os.environ.get("API_NUM_PROXIES")
    else None,
}

# Shared by all web and worker processes, so throttling limits are global rather than
# per-process. Uses PostgreSQL (created by `createcachetable`); no Redis needed.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "event_bank_cache",
    }
}

LOG_LEVEL = os.environ.get("DJANGO_LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "redacting": {
            "()": "apps.common.redaction.RedactingFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "redacting"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # httpx logs full request URLs, and mNotify puts its API key in the query string.
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}

CONNECTOR_USER_AGENT = os.environ.get("CONNECTOR_USER_AGENT", "EventBankBot/0.1")
CONNECTOR_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CONNECTOR_CONNECT_TIMEOUT_SECONDS", "5"))
CONNECTOR_READ_TIMEOUT_SECONDS = float(os.environ.get("CONNECTOR_READ_TIMEOUT_SECONDS", "10"))
CONNECTOR_TOTAL_TIMEOUT_SECONDS = float(os.environ.get("CONNECTOR_TOTAL_TIMEOUT_SECONDS", "20"))
CONNECTOR_MAX_RESPONSE_BYTES = int(os.environ.get("CONNECTOR_MAX_RESPONSE_BYTES", str(2 * 1024**2)))
CONNECTOR_MAX_REDIRECTS = int(os.environ.get("CONNECTOR_MAX_REDIRECTS", "3"))

# Real SMS can only be sent when this is explicitly enabled.
SMS_SENDING_ENABLED = env_bool("SMS_SENDING_ENABLED")
MNOTIFY_API_KEY = os.environ.get("MNOTIFY_API_KEY", "")
MNOTIFY_SENDER_ID = os.environ.get("MNOTIFY_SENDER_ID", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SENDING_ENABLED = env_bool("TELEGRAM_SENDING_ENABLED")
# Opt in to automatic ingestion explicitly. Manual ingest_source always remains available.
INGESTION_SCHEDULING_ENABLED = env_bool("INGESTION_SCHEDULING_ENABLED")
INGESTION_INTERVAL_SECONDS = max(3600, int(os.environ.get("INGESTION_INTERVAL_SECONDS", "3600")))
# Few sources per worker cycle keeps Telegram polling and delivery responsive.
INGESTION_SOURCES_PER_CYCLE = max(1, int(os.environ.get("INGESTION_SOURCES_PER_CYCLE", "2")))

# Finished deliveries, /add attempts and dormant subscribers are deleted after this many days.
DATA_RETENTION_DAYS = max(1, int(os.environ.get("DATA_RETENTION_DAYS", "30")))
