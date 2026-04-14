import os
import sys
import time
import threading
import webbrowser
import traceback
from pathlib import Path
import importlib

# -------------------------------------------------
# 1) Detect PyInstaller EXE mode + paths
# -------------------------------------------------
IS_FROZEN = getattr(sys, "frozen", False)


def get_exe_data_dir() -> Path:
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent / "app_data"
    return Path(__file__).resolve().parent / "app_data"


def get_exe_base_dir() -> Path:
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


EXE_DATA_DIR = get_exe_data_dir()
EXE_BASE_DIR = get_exe_base_dir()
EXE_DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = EXE_DATA_DIR / "app.log"


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def crash(e: Exception):
    log("❌ CRASH: " + repr(e))
    log(traceback.format_exc())


# -------------------------------------------------
# 2) Ensure project import path
# -------------------------------------------------
try:
    sys.path.insert(0, str(EXE_BASE_DIR))

    log(f"IS_FROZEN={IS_FROZEN}")
    log(f"EXE_BASE_DIR={EXE_BASE_DIR}")
    log(f"EXE_DATA_DIR={EXE_DATA_DIR}")
    log(f"sys.path[0]={sys.path[0]}")

except Exception as e:
    crash(e)

# -------------------------------------------------
# 3) FIX escpos capabilities.json (IMPORTANT)
# -------------------------------------------------
try:
    escpos_caps = EXE_BASE_DIR / "escpos" / "capabilities.json"
    if escpos_caps.exists():
        os.environ["ESCPOS_CAPABILITIES_FILE"] = str(escpos_caps)
        log("✅ escpos capabilities.json configured")
except Exception as e:
    crash(e)

# -------------------------------------------------
# 4) Settings module
# -------------------------------------------------
SETTINGS_MODULE = "onlineretailpos.settings.settings"

try:
    importlib.import_module(SETTINGS_MODULE)
except Exception as e:
    crash(e)
    raise SystemExit(1)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
log(f"✅ DJANGO_SETTINGS_MODULE={SETTINGS_MODULE}")

# -------------------------------------------------
# SAFE ADMIN IMPORT ONLY
# -------------------------------------------------
try:
    import onlineretailpos.admin
    log("✅ Forced import OK: onlineretailpos.admin")
except Exception as e:
    crash(e)

# -------------------------------------------------
# 5) Run Django + Waitress
# -------------------------------------------------
def run_server():
    try:
        import django

        django.setup()
        log("✅ django.setup() OK")

        # -----------------------------------------
        # AUTO DATABASE MIGRATIONS (FIRST RUN FIX)
        # -----------------------------------------
        try:
            from django.core.management import call_command

            db_file = EXE_DATA_DIR / "db.sqlite3"

            if not db_file.exists():
                log("⚙ First launch detected — running migrations...")
                call_command("migrate", interactive=False, run_syncdb=True)
                log("✅ Migrations completed")
            else:
                log("✅ Database already exists — skipping migrations")

        except Exception as e:
            crash(e)

        # -----------------------------------------
        # LOAD WSGI
        # -----------------------------------------
        from django.core.wsgi import get_wsgi_application
        from waitress import serve

        application = get_wsgi_application()
        log("✅ WSGI application loaded OK")

        # -----------------------------------------
        # OPEN BROWSER
        # -----------------------------------------
        def open_browser():
            time.sleep(2)
            url = "http://127.0.0.1:8000/"
            log(f"🌐 Opening browser: {url}")
            try:
                webbrowser.open(url, new=0)
            except Exception as e:
                crash(e)

        threading.Thread(target=open_browser, daemon=True).start()

        # -----------------------------------------
        # START SERVER
        # -----------------------------------------
        log("🚀 Starting waitress on 127.0.0.1:8000 ...")

        serve(
            application,
            host="127.0.0.1",
            port=8000,
            threads=8,
        )

    except Exception as e:
        crash(e)


# -------------------------------------------------
# MAIN ENTRY
# -------------------------------------------------
if __name__ == "__main__":
    run_server()
