from pathlib import Path
import os
import sys
import socket

# -------------------------------------------------
# Detect PyInstaller EXE mode
# -------------------------------------------------
IS_FROZEN = getattr(sys, "frozen", False)


def _exe_base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path.cwd()))


def _exe_data_dir() -> Path:
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent / "app_data"
    return Path.cwd() / "app_data"


BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BASE_DIR / "onlineretailpos"

EXE_BASE_DIR = _exe_base_dir()
EXE_DATA_DIR = _exe_data_dir()
EXE_DATA_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------------------------------
# Core Django settings
# -------------------------------------------------
SECRET_KEY = "django_live_secret_key_adamsmini_change_later_2026"

DEBUG = True

ROOT_URLCONF = "onlineretailpos.urls"
WSGI_APPLICATION = "onlineretailpos.wsgi.application"
ASGI_APPLICATION = "onlineretailpos.asgi.application"

# -------------------------------------------------
# Hosts & CSRF
# -------------------------------------------------
ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "adamsmini.shop",
    "www.adamsmini.shop",
    "31.97.52.238",
]

CSRF_TRUSTED_ORIGINS = [
    "https://adamsmini.shop",
    "https://www.adamsmini.shop",
    "http://127.0.0.1",
    "http://localhost",
]

try:
    local_ip = socket.gethostbyname(socket.gethostname())
    if local_ip and local_ip not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(local_ip)
except Exception:
    pass

# -------------------------------------------------
# Whitenoise optional
# -------------------------------------------------
try:
    import whitenoise  # noqa
    HAS_WHITENOISE = True
except Exception:
    HAS_WHITENOISE = False

# -------------------------------------------------
# Installed apps
# -------------------------------------------------
INSTALLED_APPS = [
    "colorfield",

    # custom admin config
    "onlineretailpos.admin.MyAdminConfig",

    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",

    # third party
    "jquery",
    "mathfilters",
    "import_export",
    "rangefilter",
    "django_admin_logs",

    # project apps
    "inventory",
    "transaction",
    "cart",
]

if HAS_WHITENOISE:
    INSTALLED_APPS.append("whitenoise.runserver_nostatic")

# -------------------------------------------------
# Middleware
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
# Templates
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
# Database
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
# Static & media
# -------------------------------------------------
STATIC_URL = "/static/"
MEDIA_URL = "/media/"

if IS_FROZEN:
    STATICFILES_DIRS = []
    STATIC_ROOT = EXE_BASE_DIR / "staticfiles"

    MEDIA_ROOT = EXE_DATA_DIR / "media"
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
else:
    STATICFILES_DIRS = [BASE_DIR / "onlineretailpos" / "static"]
    STATIC_ROOT = BASE_DIR / "staticfiles"
    MEDIA_ROOT = PROJECT_DIR / "media"

if HAS_WHITENOISE:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# -------------------------------------------------
# Basic config
# -------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Dar_es_Salaam"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -------------------------------------------------
# QZ Tray certificate signing paths
# -------------------------------------------------
# Put files like this:
# OnlineRetailPOS/
#   qz_keys/
#       digital-certificate.txt
#       private-key.pem
#
# If running as EXE, keep qz_keys beside the EXE.
if IS_FROZEN:
    QZ_KEYS_DIR = Path(sys.executable).resolve().parent / "qz_keys"
else:
    QZ_KEYS_DIR = BASE_DIR / "qz_keys"

QZ_CERT_PATH = QZ_KEYS_DIR / "digital-certificate.txt"
QZ_PRIVATE_KEY_PATH = QZ_KEYS_DIR / "private-key.pem"

# -------------------------------------------------
# Receipt/store settings
# -------------------------------------------------
RECEIPT_CHAR_COUNT = 42

STORE_NAME = "ADAMS MINI SUPERMARKET"
STORE_ADDRESS = "PO BOX 542 MOSHI\nJ.K. Nyerere Street"
STORE_PHONE = "+255744844699"
STORE_EMAIL = "adamssupermarket@gmail.com"

STORE_TIN = "102-188-357"
STORE_VRN = ""

RECEIPT_ADDITIONAL_HEADING = ""

INCLUDE_PHONE_IN_HEADING = True
INCLUDE_EMAIL_IN_HEADING = True

RECEIPT_FOOTER = "You are Welcomed !"
RECEIPT_SALES_TITLE = "*** NON FISCAL RECEIPT ***"
RECEIPT_NONFISCAL_TEXT = "40-318362-M"
RECEIPT_TILL_NO = "Till003"

PRINTER_VENDOR_ID = ""
PRINTER_PRODUCT_ID = ""
PRINT_RECEIPT = True
CASH_DRAWER = False

# IMPORTANT:
# Keep RECEIPT_HEADER as store/header only.
# Do not include TIN, VRN, NON-FISCAL, Receipt No, or item headings here.
# Those are arranged in transaction/views.py build_receipt_text().
_receipt_header_lines = [STORE_NAME]

if STORE_ADDRESS:
    _receipt_header_lines.extend(
        [line for line in STORE_ADDRESS.splitlines() if line.strip()]
    )

if INCLUDE_PHONE_IN_HEADING and STORE_PHONE:
    _receipt_header_lines.append(STORE_PHONE)

if INCLUDE_EMAIL_IN_HEADING and STORE_EMAIL:
    _receipt_header_lines.append(STORE_EMAIL)

if RECEIPT_ADDITIONAL_HEADING:
    _receipt_header_lines.append(RECEIPT_ADDITIONAL_HEADING)

RECEIPT_HEADER = "\n".join(_receipt_header_lines)
RECEIPT_SEPARATOR = "-" * RECEIPT_CHAR_COUNT
RECEIPT_COLUMN_HEADER = "DESCRIPTION\nQTY   PRICE       AMOUNT"

# -------------------------------------------------
# Login/session settings
# -------------------------------------------------
LOGIN_URL = "/user/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/user/login/"

SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_SAVE_EVERY_REQUEST = True

# -------------------------------------------------
# Security production settings
# -------------------------------------------------
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_PRELOAD = False
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
# SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# -------------------------------------------------
# Console debug prints for server logs
# -------------------------------------------------
print(
    f"[settings] DEBUG={DEBUG} | DB=django.db.backends.sqlite3 | "
    f"ALLOWED_HOSTS={ALLOWED_HOSTS}"
)
print(f"[settings] CSRF_TRUSTED_ORIGINS={CSRF_TRUSTED_ORIGINS}")
print(
    f"[settings] RECEIPT: char_count={RECEIPT_CHAR_COUNT} | "
    f"store='{STORE_NAME}' | print={PRINT_RECEIPT}"
)
print(f"[settings] QZ_CERT_PATH={QZ_CERT_PATH}")
print(f"[settings] QZ_PRIVATE_KEY_PATH={QZ_PRIVATE_KEY_PATH}")