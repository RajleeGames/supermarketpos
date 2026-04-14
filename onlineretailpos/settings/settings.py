from pathlib import Path
import os
import sys
import socket
from dotenv import load_dotenv

load_dotenv()

# -------------------------------------------------
# ✅ Detect PyInstaller (EXE) mode
# -------------------------------------------------
IS_FROZEN = getattr(sys, "frozen", False)

def _exe_base_dir() -> Path:
    # In onefile EXE, this is the extracted temp dir
    return Path(getattr(sys, "_MEIPASS", Path.cwd()))

def _exe_data_dir() -> Path:
    # Persistent writable folder next to exe
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent / "app_data"
    return Path.cwd() / "app_data"

# Project root (your repo folder)
BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BASE_DIR / "onlineretailpos"

EXE_BASE_DIR = _exe_base_dir()
EXE_DATA_DIR = _exe_data_dir()
EXE_DATA_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def str_to_bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")

def csv_to_list(s: str):
    return [x.strip() for x in (s or "").split(",") if x.strip()]

# -------------------------------------------------
# ✅ Core Django settings (must always exist!)
# -------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "django_dev_secret_key_online-retail-pos-1234")

# In EXE mode you can keep True while testing
DEBUG = str_to_bool(os.getenv("DEBUG", "True"))

ROOT_URLCONF = "onlineretailpos.urls"
WSGI_APPLICATION = "onlineretailpos.wsgi.application"
ASGI_APPLICATION = "onlineretailpos.asgi.application"

# -------------------------------------------------
# ✅ Hosts & CSRF
# -------------------------------------------------
_default_hosts = ["127.0.0.1", "localhost", "adamsmini.shop", "www.adamsmini.shop"]

env_allowed = os.getenv("ALLOWED_HOSTS", "")
if env_allowed:
    ALLOWED_HOSTS = csv_to_list(env_allowed)
    for h in _default_hosts:
        if h not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(h)
else:
    ALLOWED_HOSTS = _default_hosts.copy()

env_csrf = os.getenv("CSRF_TRUSTED_ORIGINS", "")
default_csrf = ["https://adamsmini.shop", "https://www.adamsmini.shop"]
CSRF_TRUSTED_ORIGINS = csv_to_list(env_csrf) if env_csrf else default_csrf.copy()

# local dev origins
for origin in ["http://127.0.0.1", "http://localhost"]:
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)

# add LAN IP for testing in network
try:
    local_ip = socket.gethostbyname(socket.gethostname())
    if local_ip and local_ip not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(local_ip)
except Exception:
    pass

# -------------------------------------------------
# ✅ Whitenoise optional import
# -------------------------------------------------
try:
    import whitenoise  # noqa
    HAS_WHITENOISE = True
except Exception:
    HAS_WHITENOISE = False

# -------------------------------------------------
# ✅ Installed apps
# -------------------------------------------------
INSTALLED_APPS = [
    "colorfield",

    # ✅ custom admin config (replaces django.contrib.admin)
    "onlineretailpos.admin.MyAdminConfig",

    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",

    # 3rd party
    "jquery",
    "mathfilters",
    "import_export",
    "rangefilter",
    "django_admin_logs",

    # your apps
    "inventory",
    "transaction",
    "cart",
]

# only add whitenoise app if installed
if HAS_WHITENOISE:
    INSTALLED_APPS.append("whitenoise.runserver_nostatic")

# -------------------------------------------------
# ✅ Middleware
# -------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
]

if HAS_WHITENOISE:
    MIDDLEWARE.append("whitenoise.middleware.WhiteNoiseMiddleware")

MIDDLEWARE += [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# -------------------------------------------------
# ✅ Templates
# -------------------------------------------------
if IS_FROZEN:
    TEMPLATE_DIR = str(EXE_BASE_DIR / "onlineretailpos" / "templates")
else:
    TEMPLATE_DIR = str(PROJECT_DIR / "templates")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [TEMPLATE_DIR],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

# -------------------------------------------------
# ✅ Database (force sqlite for EXE)
# -------------------------------------------------
if IS_FROZEN:
    DB_PATH = EXE_DATA_DIR / "db.sqlite3"
else:
    DB_PATH = BASE_DIR / "db.sqlite3"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DB_PATH),
    }
}

# -------------------------------------------------
# ✅ Static & media
# -------------------------------------------------
STATIC_URL = "/static/"
MEDIA_URL = "/media/"

if IS_FROZEN:
    # bundled via --add-data "staticfiles;staticfiles"
    STATICFILES_DIRS = []
    STATIC_ROOT = EXE_BASE_DIR / "staticfiles"

    MEDIA_ROOT = EXE_DATA_DIR / "media"
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
else:
    STATICFILES_DIRS = [BASE_DIR / "onlineretailpos" / "static"]
    STATIC_ROOT = BASE_DIR / "staticfiles"
    MEDIA_ROOT = PROJECT_DIR / "media"

# whitenoise storage only if installed
if HAS_WHITENOISE:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# -------------------------------------------------
# ✅ Basic config
# -------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Dar_es_Salaam"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -------------------------------------------------
# ✅ Your store/receipt settings (THIS FIXES STORE_NAME)
# -------------------------------------------------
RECEIPT_CHAR_COUNT = int(os.getenv("RECEIPT_CHAR_COUNT", "32"))

STORE_NAME = os.getenv("STORE_NAME", "ADAMS MINI SUPERMARKET")
STORE_ADDRESS = os.getenv("STORE_ADDRESS", "PO BOX 942 MOSHI\nJ.K. Nyerere Street")
STORE_PHONE = os.getenv("STORE_PHONE", "+255744844699")
STORE_EMAIL = os.getenv("STORE_EMAIL", "adamssupermarket@gmail.com")

STORE_TIN = os.getenv("STORE_TIN", "102-188-357")
STORE_VRN = os.getenv("STORE_VRN", "")

RECEIPT_ADDITIONAL_HEADING = os.getenv("RECEIPT_ADDITIONAL_HEADING", "")

INCLUDE_PHONE_IN_HEADING = str_to_bool(os.getenv("INCLUDE_PHONE_IN_HEADING", "True"))
INCLUDE_EMAIL_IN_HEADING = str_to_bool(os.getenv("INCLUDE_EMAIL_IN_HEADING", "True"))

RECEIPT_FOOTER = os.getenv("RECEIPT_FOOTER", "You are Welcomed !")
RECEIPT_SALES_TITLE = os.getenv("RECEIPT_SALES_TITLE", "*** Sales Receipt ***")
RECEIPT_NONFISCAL_TEXT = os.getenv("RECEIPT_NONFISCAL_TEXT", "*** NON-FISCAL RECEIPT ***")

PRINTER_VENDOR_ID = os.getenv("PRINTER_VENDOR_ID", "")
PRINTER_PRODUCT_ID = os.getenv("PRINTER_PRODUCT_ID", "")
PRINT_RECEIPT = str_to_bool(os.getenv("PRINT_RECEIPT", "True"))
CASH_DRAWER = str_to_bool(os.getenv("CASH_DRAWER", "False"))

_receipt_header_lines = [STORE_NAME]

if STORE_ADDRESS:
    _receipt_header_lines.extend([line for line in STORE_ADDRESS.splitlines() if line.strip()])

if INCLUDE_PHONE_IN_HEADING and STORE_PHONE:
    _receipt_header_lines.append(STORE_PHONE)

if INCLUDE_EMAIL_IN_HEADING and STORE_EMAIL:
    _receipt_header_lines.append(STORE_EMAIL)

if RECEIPT_ADDITIONAL_HEADING:
    _receipt_header_lines.append(RECEIPT_ADDITIONAL_HEADING)

_receipt_header_lines += ["", RECEIPT_SALES_TITLE]

if STORE_TIN:
    _receipt_header_lines.append(f"TIN: {STORE_TIN}")
if STORE_VRN:
    _receipt_header_lines.append(f"VRN: {STORE_VRN}")

_receipt_header_lines += [RECEIPT_NONFISCAL_TEXT, ""]

RECEIPT_HEADER = "\n".join(_receipt_header_lines)
RECEIPT_SEPARATOR = "-" * RECEIPT_CHAR_COUNT
RECEIPT_COLUMN_HEADER = "DESCRIPTION\nQTY   PRICE     AMOUNT"

# -------------------------------------------------
# ✅ Security production toggles
# -------------------------------------------------
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "120"))
    SECURE_HSTS_PRELOAD = True
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
else:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
