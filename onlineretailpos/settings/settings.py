"""
Merged settings.py for onlineretailpos

Environment variables (recommended):
  - DEBUG = "True" or "False"
  - SECRET_KEY = "<your secret key>"
  - NAME_OF_DATABASE = "sqlite" | "postgres" | "mysql"   (default: sqlite)
  - DB_NAME, DB_USERNAME, DB_PASSWORD, DB_HOST, DB_PORT  (for postgres/mysql)
  - ALLOWED_HOSTS = "adamsmini.shop,localhost,127.0.0.1"  (comma separated)   <- optional
  - CSRF_TRUSTED_ORIGINS = "https://adamsmini.shop,https://www.adamsmini.shop" (comma separated) <- optional
  - STORE_NAME, STORE_ADDRESS, STORE_PHONE, RECEIPT_CHAR_COUNT, RECEIPT_ADDITIONAL_HEADING, RECEIPT_FOOTER
  - PRINTER_VENDOR_ID, PRINTER_PRODUCT_ID, PRINT_RECEIPT, CASH_DRAWER

Notes:
 - This file ensures adamsmini.shop and www.adamsmini.shop are present by default so your VPS won't raise host/origin errors.
 - When deploying behind Nginx (proxy) set DEBUG=False and ensure Nginx forwards X-Forwarded-Proto header.
"""
from pathlib import Path
import os
import socket
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

# ---------- Basic paths ----------
BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BASE_DIR / "onlineretailpos"

# ---------- small helpers ----------
def str_to_bool(s: str) -> bool:
    return str(s).lower() in ("1", "true", "yes", "on")

def csv_to_list(s: str):
    return [item.strip() for item in s.split(",") if item.strip()]

# ---------- Debug / Secret ----------
DEBUG = str_to_bool(os.getenv("DEBUG", "True"))
SECRET_KEY = os.getenv("SECRET_KEY", "django_dev_secret_key_online-retail-pos-1234")

# ---------- Hosts & CSRF ----------
# Default hosts to include local and your domain (so VPS won't error)
_default_hosts = ["127.0.0.1", "localhost", "adamsmini.shop", "www.adamsmini.shop"]

env_allowed = os.getenv("ALLOWED_HOSTS", "")
if env_allowed:
    ALLOWED_HOSTS = csv_to_list(env_allowed)
    # ensure the essential hosts are present
    for h in _default_hosts:
        if h not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(h)
else:
    ALLOWED_HOSTS = _default_hosts.copy()

# CSRF trusted origins: prefer explicit https origins for production.
env_csrf = os.getenv("CSRF_TRUSTED_ORIGINS", "")
default_csrf = ["https://adamsmini.shop", "https://www.adamsmini.shop"]
if env_csrf:
    CSRF_TRUSTED_ORIGINS = csv_to_list(env_csrf)
    for origin in default_csrf:
        if origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin)
else:
    CSRF_TRUSTED_ORIGINS = default_csrf.copy()

# Add local http origins for local testing (optional)
if "http://127.0.0.1" not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append("http://127.0.0.1")

# If no explicit hosts, try to add local machine IP for local testing
try:
    local_ip = socket.gethostbyname(socket.gethostname())
    if local_ip and local_ip not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(local_ip)
except Exception:
    local_ip = None

# ---------- Installed apps & middleware ----------
INSTALLED_APPS = [
    "colorfield",
    'onlineretailpos.admin.MyAdminConfig',  # custom admin config
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'jquery',
    'mathfilters',
    'inventory',
    'transaction',
    'cart',
    'import_export',
    'rangefilter',
    'django_admin_logs',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

X_FRAME_OPTIONS = "SAMEORIGIN"
SILENCED_SYSTEM_CHECKS = ["security.W019"]

ROOT_URLCONF = 'onlineretailpos.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [str(PROJECT_DIR / "templates")],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'onlineretailpos.wsgi.application'

# Channels (default in-memory for now)
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer"
    }
}

# ---------- Password validators ----------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# ---------- Internationalization ----------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Dar_es_Salaam'
USE_I18N = True
USE_TZ = True
CURRENCY_SYMBOL = "TZS"

# ---------- Static & Media ----------
STATIC_URL = '/static/'
STATICFILES_DIRS = [ BASE_DIR / 'onlineretailpos' / 'static' ]
STATIC_ROOT = BASE_DIR / 'staticfiles'   # for collectstatic in production

MEDIA_URL = "/media/"
MEDIA_ROOT = PROJECT_DIR / "media"

# ---------- Cart session ----------
CART_SESSION_ID = 'cart'

# ---------- Databases ----------
# You can set NAME_OF_DATABASE to "sqlite", "postgres", or "mysql".
database_dict = {
    'sqlite' :  {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    },
    'postgres' : {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', "OnlineRetailPOS"),
        'USER': os.getenv('DB_USERNAME', ""),
        'PASSWORD': os.getenv('DB_PASSWORD', ""),
        'HOST': os.getenv('DB_HOST', "localhost"),
        'PORT': os.getenv('DB_PORT', ""),
    },
    'mysql': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', "OnlineRetailPOS"),
        'USER': os.getenv('DB_USERNAME', ""),
        'PASSWORD': os.getenv('DB_PASSWORD', ""),
        'HOST': os.getenv('DB_HOST', "localhost"),
        'PORT': os.getenv('DB_PORT', ""),
        'OPTIONS': {'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"},
    }
}

chosen_db = os.getenv('NAME_OF_DATABASE', 'sqlite').lower()
if chosen_db not in database_dict:
    chosen_db = 'sqlite'

DATABASES = {'default': database_dict[chosen_db]}

# Informational print (helpful while testing locally)
print(f"[settings] DEBUG={DEBUG} | DB={DATABASES['default']['ENGINE']} | ALLOWED_HOSTS={ALLOWED_HOSTS}")
if CSRF_TRUSTED_ORIGINS:
    print(f"[settings] CSRF_TRUSTED_ORIGINS={CSRF_TRUSTED_ORIGINS}")

# ---------- Default primary key ----------
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------- Receipt / Store Settings ----------
# Controls receipt appearance and printer options.
# You can override any of these via environment variables.

# Number of characters per line on your thermal printer (commonly 32 for 58mm, 48+ for 80mm)
RECEIPT_CHAR_COUNT = int(os.getenv("RECEIPT_CHAR_COUNT", "32"))

# Store identity (defaults chosen to match your photo)
STORE_NAME = os.getenv("STORE_NAME", "ADAMS MINI SUPERMARKET")
STORE_ADDRESS = os.getenv("STORE_ADDRESS", "PO BOX 942 MOSHI\nJ.K. Nyerere Street")
STORE_PHONE = os.getenv("STORE_PHONE", "+255744844699")
STORE_EMAIL = os.getenv("STORE_EMAIL", "adamssupermarket@gmail.com")

# Tax / registration numbers (optional)
STORE_TIN = os.getenv("STORE_TIN", "102-188-357")
STORE_VRN = os.getenv("STORE_VRN", "")

# Additional heading lines you may want to include (e.g. branch name, tagline)
RECEIPT_ADDITIONAL_HEADING = os.getenv("RECEIPT_ADDITIONAL_HEADING", "")

# Whether to include phone or email line in header (env true/false)
INCLUDE_PHONE_IN_HEADING = str_to_bool(os.getenv("INCLUDE_PHONE_IN_HEADING", "True"))
INCLUDE_EMAIL_IN_HEADING = str_to_bool(os.getenv("INCLUDE_EMAIL_IN_HEADING", "True"))

# Footer line(s)
RECEIPT_FOOTER = os.getenv("RECEIPT_FOOTER", "You are Welcomed !")

# Receipt markers (e.g., fiscal/non-fiscal text)
RECEIPT_SALES_TITLE = os.getenv("RECEIPT_SALES_TITLE", "*** Sales Receipt ***")
RECEIPT_NONFISCAL_TEXT = os.getenv("RECEIPT_NONFISCAL_TEXT", "*** NON-FISCAL RECEIPT ***")

# Printer hardware settings (optional)
PRINTER_VENDOR_ID = os.getenv("PRINTER_VENDOR_ID", "")
PRINTER_PRODUCT_ID = os.getenv("PRINTER_PRODUCT_ID", "")
PRINT_RECEIPT = str_to_bool(os.getenv("PRINT_RECEIPT", "True"))
CASH_DRAWER = str_to_bool(os.getenv("CASH_DRAWER", "False"))

# Helper: build a standard receipt header string (multi-line)
_receipt_header_lines = []
_receipt_header_lines.append(STORE_NAME)
# If address contains newlines keep them; otherwise add one line
if STORE_ADDRESS:
    _receipt_header_lines.extend([line for line in STORE_ADDRESS.splitlines() if line.strip()])
if INCLUDE_PHONE_IN_HEADING and STORE_PHONE:
    _receipt_header_lines.append(STORE_PHONE)
if INCLUDE_EMAIL_IN_HEADING and STORE_EMAIL:
    _receipt_header_lines.append(STORE_EMAIL)

# Add optional additional heading
if RECEIPT_ADDITIONAL_HEADING:
    _receipt_header_lines.append(RECEIPT_ADDITIONAL_HEADING)

# Add title, TIN/VRN and non-fiscal marker
_receipt_header_lines.append("")  # blank line
_receipt_header_lines.append(RECEIPT_SALES_TITLE)
if STORE_TIN:
    _receipt_header_lines.append(f"TIN: {STORE_TIN}")
if STORE_VRN:
    _receipt_header_lines.append(f"VRN: {STORE_VRN}")
_receipt_header_lines.append(RECEIPT_NONFISCAL_TEXT)
_receipt_header_lines.append("")  # blank line after header block

# Join and store final header constant
RECEIPT_HEADER = "\n".join(_receipt_header_lines)

# Default layout strings you can reuse in code (for clarity)
RECEIPT_SEPARATOR = "-" * RECEIPT_CHAR_COUNT
RECEIPT_COLUMN_HEADER = "DESCRIPTION\nQTY   PRICE     AMOUNT"

# Print debug info at startup (helpful during local dev)
print(f"[settings] RECEIPT: char_count={RECEIPT_CHAR_COUNT} | store='{STORE_NAME}' | print={PRINT_RECEIPT}")

# ---------- Security for production ----------
if not DEBUG:
    # Basic production security settings
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', 120))
    SECURE_HSTS_PRELOAD = True
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

    # When using Nginx as a reverse proxy you should set this so Django knows HTTPS is handled by the proxy.
    # Make sure Nginx sends: proxy_set_header X-Forwarded-Proto $scheme;
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
else:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# ---------- Other helpful settings ----------
# Add any other settings you need below, e.g. logging, email, cache, etc.
