# desktop_app.py
import os
import sys
import time
import socket
import subprocess
import logging
import ctypes
import platform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "desktop_app.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

LOCK_HOST = "127.0.0.1"
LOCK_PORT = 54321         # single-instance lock port
DJANGO_HOST = "127.0.0.1"
DJANGO_PORT = 8000
DJANGO_ADDR = f"{DJANGO_HOST}:{DJANGO_PORT}"

def msgbox(title, text):
    """Simple message box (Windows) fallback to print."""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0)
    except Exception:
        print(title + ": " + text)

def acquire_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((LOCK_HOST, LOCK_PORT))
        s.listen(1)
        logging.info("Lock acquired on %s:%d", LOCK_HOST, LOCK_PORT)
        return s
    except OSError as e:
        logging.warning("Could not acquire lock on %s:%d - %s", LOCK_HOST, LOCK_PORT, e)
        return None

def is_port_open(host, port, timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def start_django():
    """Start django runserver only if port is free. Return subprocess or None."""
    if is_port_open(DJANGO_HOST, DJANGO_PORT):
        logging.info("Django already listening on %s:%d", DJANGO_HOST, DJANGO_PORT)
        return None

    python = sys.executable or "python"
    cmd = [python, "manage.py", "runserver", DJANGO_ADDR]
    logging.info("Starting Django: %s (cwd=%s)", " ".join(cmd), BASE_DIR)
    creationflags = 0
    # hide console window on Windows
    try:
        creationflags = subprocess.CREATE_NO_WINDOW
    except Exception:
        creationflags = 0

    proc = subprocess.Popen(
        cmd,
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    logging.info("Django started pid=%s", proc.pid)
    return proc

def run_webview_force_mshtml():
    """
    Force MSHTML backend on Windows (uses IE engine). This avoids bundling CEF/Edge.
    If not Windows or MSHTML fails, fallback to default webview.start().
    """
    import webview

    url = f"http://{DJANGO_ADDR}"
    logging.info("Creating webview window for %s (forcing mshtml backend on Windows if available)", url)

    try:
        # Create window first (works across backends)
        webview.create_window("Adams Mini POS", url, width=1200, height=800, resizable=True)
        if platform.system().lower().startswith("windows"):
            # Prefer mshtml to avoid CEF/Edge-related subprocesses
            try:
                logging.info("Attempting webview.start(gui='mshtml')")
                webview.start(gui='mshtml')
                return
            except Exception as e_ms:
                logging.warning("mshtml gui failed: %s. Falling back to default start()", e_ms)
        # default fallback
        webview.start()
    except Exception as e:
        logging.exception("webview failed: %s", e)
        raise

def main():
    logging.info("Launcher starting (base_dir=%s)", BASE_DIR)

    lock = acquire_lock()
    if not lock:
        msg = "Adams Mini POS is already running. Close the existing instance first."
        logging.error(msg)
        msgbox("Adams Mini POS", msg)
        return

    django_proc = None
    try:
        django_proc = start_django()
        # wait for django to respond
        timeout = 20
        start_ts = time.time()
        while time.time() - start_ts < timeout:
            if is_port_open(DJANGO_HOST, DJANGO_PORT):
                logging.info("Django is up and listening.")
                break
            time.sleep(0.5)
        else:
            logging.error("Django did not start within %ds.", timeout)
            msgbox("Adams Mini POS", "Django server did not start quickly. The app will show an error page.")

        # Force mshtml on Windows to avoid cef/edge child processes
        try:
            run_webview_force_mshtml()
        except Exception as e:
            logging.exception("run_webview_force_mshtml failed: %s", e)
            # final fallback: try default start
            try:
                import webview
                webview.start()
            except Exception as e2:
                logging.exception("Final fallback webview.start() also failed: %s", e2)
                msgbox("Adams Mini POS", "Failed to start embedded browser. See log.")
    except Exception as e:
        logging.exception("Unhandled exception in launcher: %s", e)
        msgbox("Launcher Error", "An unexpected error occurred. Check desktop_app.log for details.")
    finally:
        # terminate django if started
        try:
            if django_proc and django_proc.poll() is None:
                logging.info("Terminating django pid=%s", django_proc.pid)
                django_proc.terminate()
                try:
                    django_proc.wait(timeout=5)
                except Exception:
                    django_proc.kill()
        except Exception as e:
            logging.exception("Error terminating django process: %s", e)
        # close lock socket
        try:
            if lock:
                lock.close()
                logging.info("Lock socket closed.")
        except Exception:
            pass
        logging.info("Launcher finished.")

if __name__ == "__main__":
    main()
