import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("CRM_DEBUG") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY") or ("dev-key-for-local-testing" if DEBUG else os.environ["SECRET_KEY"])
ALLOWED_HOSTS = ["localhost", "127.0.0.1"] + [h for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h]
if os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
ROOT_URLCONF = "crm.urls"
INSTALLED_APPS = ["django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "crm"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware", "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware"]
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
if not DEBUG and not os.getenv("DATABASE_URL"):
    raise RuntimeError("DATABASE_URL is required in production.")
DATABASES = {"default": dj_database_url.config(default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60, conn_health_checks=True)}
if not DEBUG and DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise RuntimeError("Production requires a PostgreSQL database.")
AUTH_PASSWORD_VALIDATORS = [{"NAME": f"django.contrib.auth.password_validation.{name}"} for name in ("UserAttributeSimilarityValidator", "MinimumLengthValidator", "CommonPasswordValidator", "NumericPasswordValidator")]
LOGIN_URL = "/login/"
TIME_ZONE = "Asia/Dhaka"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^health/?$"]
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
SESSION_COOKIE_AGE = 43200
X_FRAME_OPTIONS = "SAMEORIGIN"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
