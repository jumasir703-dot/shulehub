"""
licence.py - licence key and open check-in for ShuleHub.

What it does
  The school enters the licence key you gave them (Menu > Licence & Check-in). From then on, when ShuleHub starts
  (and every 12 hours while it stays open) it sends ONE small message to your ShuleHub Control server.

What is sent (and the school can read this on the same page)
  licence key, school name, ShuleHub version, computer type (e.g. Windows 10), and a random install id
  created on this computer. Nothing else: no students, guardians, fees, marks, timetables or files.

If there is no internet, nothing happens and ShuleHub works normally. Check-in never slows a page down
(it runs in the background) and never raises an error.

Access: the pages here are not listed in permissions.py, so only administrators can open them.
"""
import json
import platform
import threading
import urllib.request
import uuid
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

import checkin_config
import database as db

licence_bp = Blueprint("licence", __name__, url_prefix="/licence")

_no_id = getattr(db, "TABLES_WITHOUT_ID", None)
if _no_id is not None:
    _no_id.add("licence_settings")

INTERVAL_SECONDS = 12 * 3600
TIMEOUT_SECONDS = 6
_ready = False
_started = False
_wake = threading.Event()


def ensure_schema():
    global _ready
    if _ready:
        return
    conn = db.get_connection()
    try:
        ddl = "CREATE TABLE IF NOT EXISTS licence_settings (key TEXT PRIMARY KEY, value TEXT)"
        if db.USE_POSTGRES:
            conn.cursor().execute(ddl)
        else:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()
    _ready = True


def _get(key, default=""):
    row = db.fetch_one("SELECT value FROM licence_settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] is not None else default


def _put(key, value):
    db.execute("INSERT INTO licence_settings (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


def server_url():
    return (getattr(checkin_config, "SERVER_URL", "") or "").strip().rstrip("/")


def install_id():
    iid = _get("install_id")
    if not iid:
        iid = uuid.uuid4().hex
        _put("install_id", iid)
    return iid


def payload():
    """Exactly what a check-in contains."""
    try:
        school = db.get_school_settings()["name"]
    except Exception:
        school = ""
    return {"key": _get("licence_key"), "school": school, "version": checkin_config.APP_VERSION,
            "os": f"{platform.system()} {platform.release()}".strip(), "install_id": install_id()}


def ping():
    """Send one check-in. Returns (ok, message). Never raises."""
    try:
        ensure_schema()
        if not server_url():
            return False, "This copy of ShuleHub is not set up to check in."
        data = payload()
        if not data["key"]:
            return False, "Enter your licence key first."
        req = urllib.request.Request(server_url() + "/api/checkin", data=json.dumps(data).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": "ShuleHub/" + data["version"]})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
        stamp = datetime.now().strftime("%d %b %Y, %H:%M")
        _put("last_attempt", stamp)
        _put("notice", answer.get("notice") or "")
        if answer.get("known"):
            _put("last_ok", stamp)
            _put("last_result", "Checked in" + ("" if answer.get("ok") else " (this licence is switched off, please contact your supplier)"))
            return True, "Checked in."
        _put("last_result", "The server did not recognise this licence key. Check it and try again.")
        return False, "The server did not recognise this licence key."
    except Exception:
        try:
            _put("last_attempt", datetime.now().strftime("%d %b %Y, %H:%M"))
            _put("last_result", "Could not reach the server (no internet?). ShuleHub works normally; it will try again later.")
        except Exception:
            pass
        return False, "Could not reach the server (no internet?). ShuleHub works normally; it will try again later."


def _loop():
    while True:
        if _get("licence_key") and server_url():
            ping()
        _wake.wait(INTERVAL_SECONDS)
        _wake.clear()


@licence_bp.before_app_request
def _start_background_checkin():
    """On the first page request after ShuleHub starts, begin checking in in the background."""
    global _started
    if _started:
        return
    _started = True
    try:
        ensure_schema()
        if server_url():
            threading.Thread(target=_loop, daemon=True, name="shulehub-checkin").start()
    except Exception:
        pass


@licence_bp.before_request
def _before():
    ensure_schema()


@licence_bp.route("/")
def index():
    return render_template("licence/index.html", configured=bool(server_url()), key=_get("licence_key"),
                           sent=payload() if _get("licence_key") else None, last_ok=_get("last_ok"),
                           last_attempt=_get("last_attempt"), last_result=_get("last_result"), notice=_get("notice"),
                           version=checkin_config.APP_VERSION, server=server_url(), example=dict(payload(), key=_get("licence_key") or "SHUB-XXXX-XXXX-XXXX"))


@licence_bp.route("/save", methods=["POST"])
def save():
    key = request.form.get("key", "").strip().upper()[:40]
    _put("licence_key", key)
    if key:
        ok, msg = ping()
        flash(("Licence key saved. " + msg), "success" if ok else "error")
    else:
        flash("Licence key removed.", "success")
    return redirect(url_for("licence.index"))


@licence_bp.route("/now", methods=["POST"])
def now():
    ok, msg = ping()
    flash(msg, "success" if ok else "error")
    return redirect(url_for("licence.index"))
