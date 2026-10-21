"""
run_desktop.py — offline desktop launcher for ShuleHub.

This is the PyInstaller entry point. It does three things app.py itself
doesn't need to worry about when run normally with `flask run` / gunicorn:

1. Points the SQLite database at a writable, persistent folder in the
   user's home directory (~/ShuleHubData) instead of the folder the
   app happens to be unpacked into. PyInstaller's --onefile mode
   extracts the app to a temporary folder that's wiped after every run,
   so if we didn't do this, all data would vanish each time the program
   closed.
2. Forces DATABASE_URL to stay unset, so database.py always falls back
   to its local SQLite path — no Postgres, no network, fully offline.
3. Starts the Flask dev server on localhost only (not 0.0.0.0) with
   debug/reloader off (those don't play well with a frozen executable),
   running in a background thread, and opens it in a native desktop
   window via pywebview instead of a browser tab -- on Windows this uses
   the Edge WebView2 engine that ships with Windows 10/11, so there's no
   extra runtime to install.
"""

import os
import sys
import threading
import time
import traceback
import webbrowser

from flask import redirect, render_template_string, request

from license_manager import verify_key

# --- 1 & 2: decide where the database lives, before app/database import it ---
def _frozen_data_dir():
    """~/ShuleHubData. Installs from before the rename kept their data in
    ~/GTimetableData: move that folder across so nothing is lost, or, if it
    can't be moved (locked, permissions), keep using it where it is."""
    home = os.path.expanduser("~")
    new_dir = os.path.join(home, "ShuleHubData")
    old_dir = os.path.join(home, "GTimetableData")
    if not os.path.exists(new_dir) and os.path.isdir(old_dir):
        try:
            os.rename(old_dir, new_dir)
        except OSError:
            return old_dir
    return new_dir


if getattr(sys, "frozen", False):
    # Running as a PyInstaller-built executable.
    DATA_DIR = _frozen_data_dir()
else:
    # Running as a normal script (e.g. `python run_desktop.py` for testing).
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("DB_PATH", os.path.join(DATA_DIR, "timetable.db"))
os.environ.pop("DATABASE_URL", None)  # never talk to Postgres offline


def _log(message):
    """print() can crash a windowed (no-console) build on Windows, since
    sys.stdout is None in that mode. This writes to both the console when
    one exists, and always to a log file, so problems are never invisible."""
    if sys.stdout is not None:
        try:
            print(message)
        except Exception:
            pass
    try:
        with open(os.path.join(DATA_DIR, "app.log"), "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass


# Now it's safe to import the app (this is what actually creates the DB).
from app import app  # noqa: E402

# The windowed build has no console, so Flask's error details used to vanish.
# Send every server error (with its full traceback) to app.log in the data folder.
import logging  # noqa: E402

_error_log = logging.FileHandler(os.path.join(DATA_DIR, "app.log"), encoding="utf-8")
_error_log.setLevel(logging.ERROR)
_error_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
app.logger.addHandler(_error_log)


@app.context_processor
def _inject_desktop_mode():
    # Tells base.html not to register the PWA service worker (and to
    # unregister any copy a previous run already installed). That service
    # worker is meant for the web-hosted version; in the desktop build its
    # network fetch() gets blocked by Windows/Chrome whenever wifi is off
    # -- even though it's only talking to our own local server -- which
    # was making the app incorrectly show its "offline" page.
    return {"desktop_mode": True}


# ---------------------------------------------------------------------------
# Passkey / subscription gating
# ---------------------------------------------------------------------------
LICENSE_FILE = os.path.join(DATA_DIR, "license.key")
LAST_SEEN_FILE = os.path.join(DATA_DIR, "license_last_seen")

_license_state = {"valid": False, "tier": None, "expiry": None, "error": "No passkey activated yet."}


def _clock_looks_rolled_back():
    """Cheap check against setting the system clock backwards to dodge an
    expiry date: we remember the latest date we've ever seen the app run,
    and refuse to trust "today" if it's earlier than that. Not bulletproof
    (see license_manager.py docstring) but stops casual tampering."""
    import datetime as _dt

    today = _dt.date.today().isoformat()
    try:
        with open(LAST_SEEN_FILE, "r", encoding="utf-8") as f:
            last_seen = f.read().strip()
    except Exception:
        last_seen = None

    rolled_back = bool(last_seen) and today < last_seen

    newest = max(today, last_seen) if last_seen else today
    try:
        with open(LAST_SEEN_FILE, "w", encoding="utf-8") as f:
            f.write(newest)
    except Exception:
        pass

    return rolled_back


def _refresh_license_state():
    if not os.path.exists(LICENSE_FILE):
        _license_state.update({"valid": False, "tier": None, "expiry": None,
                                "error": "No passkey activated yet."})
        return
    try:
        with open(LICENSE_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
    except Exception:
        _license_state.update({"valid": False, "error": "Could not read the saved passkey."})
        return

    result = verify_key(key)
    if result.get("valid") and not result.get("expired") and _clock_looks_rolled_back():
        result = {"valid": False, "error": "Your system clock looks like it was set backwards. "
                                            "Please correct the date and time and try again."}
    _license_state.update(result)


_LICENSE_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<title>Activate ShuleHub</title>
<style>
  body { font-family: system-ui, sans-serif; background:#103524; color:#fbf8f0;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }
  .card { background:#0b2419; padding:2.5rem; border-radius:12px; width:400px;
          box-shadow:0 10px 40px rgba(0,0,0,.4); }
  h1 { font-size:1.25rem; margin-top:0; }
  input[type=text] { width:100%; padding:.65rem; border-radius:6px; border:1px solid #3a5c48;
                      background:#0e2c1f; color:#fbf8f0; box-sizing:border-box;
                      font-size:1rem; letter-spacing:.5px; font-family: monospace; }
  button { margin-top:1rem; width:100%; padding:.75rem; border:none; border-radius:6px;
           background:#e3dcc9; color:#103524; font-weight:bold; cursor:pointer; font-size:1rem; }
  .error { color:#ff9a8b; margin-top:.85rem; font-size:.9rem; }
  .hint { color:#9fbfae; font-size:.8rem; margin-top:1.25rem; line-height:1.4; }
</style></head>
<body>
  <div class="card">
    <h1>{{ heading }}</h1>
    <form method="post">
      <input type="text" name="passkey" placeholder="GT-STANDARD-20261231-AB12CD34" autofocus required>
      <button type="submit">Activate</button>
    </form>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <div class="hint">Contact your administrator for a passkey.</div>
  </div>
</body></html>
"""


@app.route("/license", methods=["GET", "POST"])
def _license_page():
    error = None
    if request.method == "POST":
        key = request.form.get("passkey", "")
        result = verify_key(key)
        if result.get("valid") and not result.get("expired"):
            with open(LICENSE_FILE, "w", encoding="utf-8") as f:
                f.write(key.strip())
            _refresh_license_state()
            return redirect("/")
        error = result.get("error") or "That passkey isn't valid."

    heading = "Your subscription has expired" if _license_state.get("expired") \
        else "Activate ShuleHub"
    return render_template_string(_LICENSE_PAGE, error=error, heading=heading)


@app.before_request
def _enforce_license():
    if request.path.startswith("/static"):
        return
    if request.path == "/license":
        # Handle this ourselves and stop here -- otherwise, since we only
        # stepped aside, the request would fall through to app.py's own
        # before_request (e.g. its onboarding-wizard redirect), which
        # doesn't know about /license and would redirect it elsewhere.
        return _license_page()
    if not _license_state.get("valid") or _license_state.get("expired"):
        return redirect("/license")


# app.py registers its own before_request (e.g. redirecting to the
# onboarding wizard) when it's imported above, and Flask runs
# before_request hooks in registration order, stopping at the first one
# that returns a response. That means without this, app.py's onboarding
# check would run first and could redirect away from /license before our
# check ever gets a say. Moving ours to the front guarantees the license
# gate always wins.
app.before_request_funcs.setdefault(None, []).remove(_enforce_license)
app.before_request_funcs.setdefault(None, []).insert(0, _enforce_license)

_refresh_license_state()

# The rest of app.py's routes can read app.config["LICENSE_TIER"] to gate
# specific features by package if you want different tiers to unlock
# different things later on.
app.config["LICENSE_TIER"] = _license_state.get("tier")
app.config["LICENSE_EXPIRY"] = _license_state.get("expiry")


HOST = "127.0.0.1"
PORT = 5000


def _run_server():
    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    except Exception:
        _log("CRASHED:\n" + traceback.format_exc())


if __name__ == "__main__":
    _log(f"ShuleHub starting at http://{HOST}:{PORT}/")
    _log(f"Your data is stored at: {os.environ['DB_PATH']}")

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()
    time.sleep(1.2)  # give the server a moment to bind the port

    try:
        import webview

        # Recent pywebview versions block file downloads unless the app allows
        # them, which silently breaks every "Download PDF" / "Export to CSV"
        # button inside the app window. Turn them on (Windows then shows the
        # normal "Save as" box). Wrapped so an older pywebview without this
        # setting can't stop the app from opening.
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass

        webview.create_window(
            "ShuleHub",
            f"http://{HOST}:{PORT}/",
            width=1200,
            height=800,
            min_size=(800, 600),
        )
        webview.start()  # blocks until the window is closed
    except Exception:
        # If pywebview can't load for any reason (missing WebView2
        # runtime, unsupported platform, etc.) fall back to opening the
        # user's normal browser instead, so the app is still usable.
        _log("pywebview failed, falling back to browser:\n" + traceback.format_exc())
        webbrowser.open(f"http://{HOST}:{PORT}/")
        server_thread.join()
