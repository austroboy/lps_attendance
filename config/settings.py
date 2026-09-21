"""
Django settings for the LPS Attendance System.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    """
    Read .env into the environment.

    Every setting in this file is read from the environment, and .env.example
    tells you to copy it to .env — but until now nothing actually read the file,
    so a token or a broker URL you carefully put there was silently ignored and
    the default was used instead.

    Deliberately no python-dotenv dependency: the format we need is KEY=value,
    optional quotes, # comments, blank lines. Real environment variables always
    win, so `EBKN_LOG_LEVEL=DEBUG python manage.py runserver` still overrides
    the file.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def env(key, default=""):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return env(key, str(default)).lower() in ("1", "true", "yes", "on")


_DEV_KEY = "dev-insecure-change-me-before-deploy"
SECRET_KEY = env("DJANGO_SECRET_KEY") or env("SECRET_KEY") or _DEV_KEY
DEBUG = env_bool("DJANGO_DEBUG", True)

if not DEBUG and SECRET_KEY == _DEV_KEY:
    # Better to refuse to start than to sign every session with a key that is
    # sitting in a public repository.
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is False.")

# Devices talk to the server by raw IP, so keep this permissive on LAN.
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "accounts",
    "academics",
    "devices",
    "attendance",
    "smsapp",
    "reports",
]

MIDDLEWARE = [
    # Must sit above CSRF / session middleware: the attendance terminals POST
    # raw octet-stream bodies with no cookies and no CSRF token.
    "devices.middleware.EbknDeviceMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Static files are served by the app itself. It keeps nginx and SELinux out
    # of the picture — nginx never has to read inside /home — and costs nothing
    # measurable for an admin tool.
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "accounts.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

def _database_from_url(url):
    """
    postgresql://user:pass@host:port/name?sslmode=disable  ->  Django DATABASES entry.

    No dj-database-url dependency; this is the only shape we need. Query
    parameters become connection OPTIONS, which is how ?sslmode=disable reaches
    psycopg — required on a server whose local PostgreSQL has no TLS.
    """
    from urllib.parse import parse_qsl, unquote, urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql", "pgsql"):
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "127.0.0.1",
        "PORT": str(parsed.port or 5432),
        "OPTIONS": dict(parse_qsl(parsed.query)),
        # Reuse connections for a minute; health checks catch a PostgreSQL
        # restart instead of failing the first request after it.
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }


if env("DATABASE_URL"):
    DATABASES = {"default": _database_from_url(env("DATABASE_URL"))}
elif env("DB_ENGINE", "sqlite") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "lps_attendance"),
            "USER": env("DB_USER", "postgres"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 6}},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Dhaka")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed but not manifest-hashed. A manifest store raises at import time
    # if collectstatic has not run, which would take the whole site down on a
    # botched deploy. Unhashed files are cached for 60 seconds in production, so
    # a CSS change reaches every browser within a minute.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Running behind nginx
# ---------------------------------------------------------------------------
CSRF_TRUSTED_ORIGINS = [o for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

if not DEBUG:
    # nginx terminates TLS and says so in X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = False
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    # nginx already redirects port 80 to 443 for people. It must never do that
    # for the terminals, which talk plain HTTP on their own port — so Django
    # does not redirect either, rather than risk bouncing a punch.
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "0"))

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# ---------------------------------------------------------------------------
# EBKN device protocol
# ---------------------------------------------------------------------------
# Devices POST to whatever path is configured in their menu. The middleware
# recognises them by the `request_code` header instead of the path, so any
# path works. This is only the canonical one shown in the UI.
EBKN_ENDPOINT_PATH = env("EBKN_ENDPOINT_PATH", "/ebkn/")

# What to type into the terminal menu. In production the devices do NOT use the
# website's domain: they talk plain HTTP to a dedicated port where this app is
# the only thing listening. Shown on the Devices screen.
EBKN_PUBLIC_ENDPOINT = env("EBKN_PUBLIC_ENDPOINT", "")

# Django's dev server, nginx and Apache 2.4+ all delete headers containing an
# underscore by default — which is every header this protocol uses. Leave this
# on or no terminal will ever be identified. See devices/apps.py.
EBKN_ALLOW_UNDERSCORE_HEADERS = env_bool("EBKN_ALLOW_UNDERSCORE_HEADERS", True)

# Accept punches from a device serial that is not registered yet. They land in
# the "Unknown devices" screen so you can see the real dev_id and register it.
EBKN_AUTO_DISCOVER = env_bool("EBKN_AUTO_DISCOVER", True)

# Keep a full copy of every request/response for debugging. Turn off once the
# fleet is stable; it grows fast.
EBKN_TRAFFIC_LOG = env_bool("EBKN_TRAFFIC_LOG", True)
EBKN_TRAFFIC_KEEP = int(env("EBKN_TRAFFIC_KEEP", "2000"))

# ---------------------------------------------------------------------------
# SSL Wireless SMS (ISMS Plus v3)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Celery — background work (bulk student imports)
# ---------------------------------------------------------------------------
# Sized for a small server. Two workers, one task at a time, late ack so a
# killed worker re-queues its job instead of dropping it.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 20
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_RESULT_EXPIRES = 60 * 60 * 24

# With no broker installed, run the import inline instead. Slower and it blocks
# the request, but the feature still works on a machine where Redis was never
# set up. Turn it on deliberately; do not leave it on with 5,000-row files.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True

# How long the upload form waits to hand a job to the broker before giving up
# and leaving it PENDING for `manage.py run_import`.
CELERY_DISPATCH_TIMEOUT = float(env("CELERY_DISPATCH_TIMEOUT", "3"))

SSLWIRELESS = {
    "BASE_URL": env("SSLWIRELESS_BASE_URL", "https://smsplus.sslwireless.com"),
    "API_TOKEN": env("SSLWIRELESS_API_TOKEN", ""),
    "SID": env("SSLWIRELESS_SID", ""),
    "TIMEOUT": int(env("SSLWIRELESS_TIMEOUT", "20")),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # INFO keeps the useful lines (an unregistered terminal called in, a
        # backfill landed) and drops the setup chatter. Switch to DEBUG while
        # you are chasing a device that will not talk.
        "ebkn": {"handlers": ["console"],
                 "level": env("EBKN_LOG_LEVEL", "INFO"), "propagate": False},
    },
}
