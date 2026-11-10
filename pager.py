"""
pager.py — Staff alerts ("pager"): tell teachers and staff about meetings and
school events, in the app and by SMS, and see who has confirmed.

Who can do what
    Everyone with a login   reads the alerts sent to them (bell + pop-up + My Alerts),
                            taps "Got it", and sets their own phone number.
    People with the "Send staff alerts" service (Roles & Access)
                            send alerts, see who has confirmed, resend failed SMS,
                            withdraw an alert, and keep everyone's phone numbers.

Pages
    /pager/                 My Alerts                          (everyone)
    /pager/send   (GET)     compose an alert                   (senders)
    /pager/sent             alerts sent so far                 (senders)
    /pager/sent/<id>        who has seen / confirmed, SMS status (senders)
    /pager/contacts         staff phone numbers + SMS test     (senders)
    /api/pager/summary      unread count + new pop-ups, polled by every page
    /api/pager/ack/<id>     "Got it" from the pop-up

SMS (Africa's Talking) is switched on with environment variables, never stored
in the database:
    AT_USERNAME   your Africa's Talking username ("sandbox" uses their test service)
    AT_API_KEY    the API key
    AT_SENDER_ID  optional approved sender name
    PAGER_SMS_MODE=test   records SMS as sent without sending anything (for trying it out)
Without these, alerts still work in the app and the SMS option is switched off.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

import auth
import database as db

pager_bp = Blueprint("pager", __name__)

# Everyone who is logged in may read their own alerts. (Every other pager page
# belongs to the "pager" service — see permissions.py.)
auth.COMMON.update({"pager.inbox", "pager.ack", "pager.my_phone", "pager.api_summary", "pager.api_ack"})

# These tables have no 'id' column. On Postgres, database.execute() adds
# "RETURNING id" to every INSERT unless the table is listed here.
db.TABLES_WITHOUT_ID.update({"pager_recipients", "pager_contacts"})

KINDS = {"meeting": "Meeting", "event": "School event", "notice": "Notice", "urgent": "Urgent"}
SMS_MAX_CHARS = 459                 # three SMS parts
SMS_SUCCESS = {100, 101, 102}       # Processed, Sent, Queued
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_DDL = """
CREATE TABLE IF NOT EXISTS pager_alerts (
    id {PK},
    kind TEXT NOT NULL DEFAULT 'notice',
    title TEXT NOT NULL,
    body TEXT,
    event_date TEXT,
    event_time TEXT,
    venue TEXT,
    audience TEXT,
    sent_by INTEGER,
    sent_by_name TEXT,
    created_at TEXT NOT NULL,
    sms_requested INTEGER NOT NULL DEFAULT 0,
    withdrawn INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pager_recipients (
    alert_id INTEGER NOT NULL REFERENCES pager_alerts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone TEXT,
    sms_status TEXT,
    sms_detail TEXT,
    seen_at TEXT,
    ack_at TEXT,
    PRIMARY KEY (alert_id, user_id)
);

CREATE TABLE IF NOT EXISTS pager_contacts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    phone TEXT
);

CREATE INDEX IF NOT EXISTS idx_pager_recipients_user ON pager_recipients (user_id, ack_at);
"""

_ready = set()   # schools whose tables are known to exist


def _school_key():
    try:
        import tenancy
        return tenancy.current()
    except ImportError:
        return 1


def ensure_schema():
    """Create the pager tables if they're missing. Safe to run any number of
    times, so existing databases (SQLite or Postgres) upgrade in place."""
    key = _school_key()
    if key in _ready:
        return
    conn = db.get_connection()
    try:
        ddl = _DDL.replace("{PK}", "SERIAL PRIMARY KEY" if db.USE_POSTGRES
                           else "INTEGER PRIMARY KEY AUTOINCREMENT")
        if not db.USE_POSTGRES and key != 1:
            # SQLite sister schools keep logins in another file, so a foreign key to users can't exist here
            ddl = re.sub(r"\s+REFERENCES users\(id\)( ON DELETE CASCADE)?", "", ddl)
        if db.USE_POSTGRES:
            conn.cursor().execute(ddl)
        else:
            conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    _ready.add(key)


@pager_bp.before_request
def _before():
    ensure_schema()


@pager_bp.app_context_processor
def _flag():
    # base.html only draws the bell and menu links when this is true, so the
    # page layout keeps working even if the pager is not installed.
    return {"pager_on": True}


# ---------------------------------------------------------------- small helpers
def _now():
    """Now, in the school's time zone (default Nairobi) as 'YYYY-MM-DD HH:MM:SS'."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("SCHOOL_TZ", "Africa/Nairobi"))
    except Exception:
        tz = timezone(timedelta(hours=3))   # Kenya has no daylight saving
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def normalize_phone(raw):
    """Kenyan-style numbers -> +2547XXXXXXXX; anything else is kept as digits."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return ""
    if s.startswith("+"):
        return s
    if s.startswith("254") and len(s) == 12:
        return "+" + s
    if s.startswith("0") and len(s) == 10:
        return "+254" + s[1:]
    if len(s) == 9 and s[0] in "17":
        return "+254" + s
    return s


def valid_phone(p):
    return bool(re.fullmatch(r"\+\d{9,15}", p or ""))


def _clean_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _clean_time(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def nice_date(iso):
    """'2026-11-14' -> 'Fri 14 Nov 2026' (no locale needed)."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def nice_stamp(stamp):
    """'2026-11-08 10:24:00' -> '8 Nov, 10:24'."""
    try:
        d = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {MONTHS[d.month - 1]}, {d:%H:%M}"


def when_text(a):
    return " ".join(x for x in (nice_date(a["event_date"]), a["event_time"] or "") if x)


def sms_text(school, kind, title, event_date, event_time, venue, body):
    """The text staff get by SMS. The compose page builds the identical text in
    JavaScript for its live preview, so keep the two in step."""
    head = f"{school} - {KINDS.get(kind, 'Notice')}: {title}"
    bits = []
    when = " ".join(x for x in (nice_date(event_date), event_time or "") if x)
    if when:
        bits.append(when)
    if venue:
        bits.append(f"Venue: {venue}")
    if body:
        bits.append(" ".join(body.split()))
    text = head + (". " + ". ".join(bits) if bits else "")
    return text[:SMS_MAX_CHARS]


def _school_name():
    return db.get_school_settings()["name"]


def _staff():
    """Everyone with an active login, with their SMS number if they have one."""
    rows = db.fetch_all(
        "SELECT u.id, u.full_name, u.username, u.role, c.phone FROM users u "
        "LEFT JOIN pager_contacts c ON c.user_id = u.id WHERE u.active = 1 ORDER BY u.full_name")
    roles = auth.role_names()
    try:   # a school group: only the people who work in this school
        import tenancy
        here = tenancy.users_for_school(tenancy.current())
        rows = [r for r in rows if r["id"] in here]
    except ImportError:
        pass
    out = []
    for r in rows:
        phone = normalize_phone(r["phone"])
        out.append({"id": r["id"], "name": r["full_name"], "username": r["username"], "role": r["role"],
                    "role_name": roles.get(r["role"], r["role"]), "phone": phone,
                    "phone_ok": valid_phone(phone)})
    return out


# ---------------------------------------------------------------- SMS (Africa's Talking)
def sms_config():
    mode = os.environ.get("PAGER_SMS_MODE", "").strip().lower()
    user = os.environ.get("AT_USERNAME", "").strip()
    key = os.environ.get("AT_API_KEY", "").strip()
    return {"test_mode": mode == "test", "username": user, "api_key": key,
            "sender": os.environ.get("AT_SENDER_ID", "").strip(),
            "sandbox": user.lower() == "sandbox",
            "configured": mode == "test" or bool(user and key)}


def sms_status_info():
    """What the pages may know about SMS — never the API key."""
    c = sms_config()
    return {"configured": c["configured"], "test_mode": c["test_mode"], "sandbox": c["sandbox"]}


def _http_post(url, headers, data, timeout=15):
    """POST a form; returns (status, body text). Kept separate so tests can replace it."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _send_chunk(cfg, phones, message):
    """One request to Africa's Talking. Returns {phone: (ok, detail)} for every phone given."""
    host = "api.sandbox.africastalking.com" if cfg["sandbox"] else "api.africastalking.com"
    data = {"username": cfg["username"], "to": ",".join(phones), "message": message}
    if cfg["sender"]:
        data["from"] = cfg["sender"]
    headers = {"apiKey": cfg["api_key"], "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    try:
        status, text = _http_post(f"https://{host}/version1/messaging", headers, data)
    except Exception:
        return {p: (False, "Could not reach the SMS service") for p in phones}
    if status in (401, 403):
        return {p: (False, "SMS login rejected: check the username and API key") for p in phones}
    try:
        payload = json.loads(text)["SMSMessageData"]
        recipients = payload.get("Recipients") or []
        note = (payload.get("Message") or "").strip()
    except Exception:
        return {p: (False, f"The SMS service sent an unexpected reply (HTTP {status})") for p in phones}
    result = {}
    for r in recipients:
        try:
            ok = int(r.get("statusCode")) in SMS_SUCCESS
        except (TypeError, ValueError):
            ok = False
        result[normalize_phone(str(r.get("number", "")))] = (
            ok, "Sent" if ok else (str(r.get("status") or "Failed")[:80]))
    missing = note[:80] if note else "No reply for this number"
    return {p: result.get(p, (False, missing)) for p in phones}


def send_sms(targets, message):
    """targets: [(key, '+2547...')]. Returns {key: (ok, detail)}. Never raises."""
    cfg = sms_config()
    if not targets:
        return {}
    if cfg["test_mode"]:
        return {k: (True, "Test mode: not really sent") for k, _ in targets}
    if not (cfg["username"] and cfg["api_key"]):
        return {k: (False, "SMS is not connected yet") for k, _ in targets}
    by_phone = {}
    for key, phone in targets:
        by_phone.setdefault(phone, []).append(key)
    phones = list(by_phone)
    results = {}
    for i in range(0, len(phones), 100):
        results.update(_send_chunk(cfg, phones[i:i + 100], message))
    out = {}
    for phone, keys in by_phone.items():
        for k in keys:
            out[k] = results.get(phone, (False, "No reply for this number"))
    return out


def _sms_summary(counts):
    bits = []
    if counts.get("sent"):
        bits.append(f"{counts['sent']} sent")
    if counts.get("failed"):
        bits.append(f"{counts['failed']} failed")
    if counts.get("no_number"):
        bits.append(f"{counts['no_number']} with no valid phone number")
    return "SMS: " + ", ".join(bits) + "." if bits else ""


def _apply_sms(alert_id, rows, text):
    """Send `text` to rows [{user_id, phone}] and record each person's result.
    Returns {'sent':n,'failed':n,'no_number':n}."""
    counts = {"sent": 0, "failed": 0, "no_number": 0}
    targets = [(r["user_id"], r["phone"]) for r in rows if valid_phone(r["phone"])]
    results = send_sms(targets, text)
    updates = []
    for r in rows:
        uid = r["user_id"]
        if uid in results:
            ok, detail = results[uid]
            status = "sent" if ok else "failed"
        else:
            status, detail = "no_number", "No valid phone number"
        counts[status] += 1
        updates.append(("UPDATE pager_recipients SET sms_status=?, sms_detail=? WHERE alert_id=? AND user_id=?",
                        (status, detail, alert_id, uid)))
    if updates:
        db.execute_many(updates)
    return counts


# ---------------------------------------------------------------- views for alerts
def _alert_view(a):
    return {
        "id": a["id"], "kind": a["kind"], "kind_label": KINDS.get(a["kind"], "Notice"),
        "urgent": a["kind"] == "urgent", "title": a["title"], "body": a["body"] or "",
        "when": when_text(a), "venue": a["venue"] or "", "sent_by": a["sent_by_name"] or "",
        "sent_at": nice_stamp(a["created_at"]),
    }


def _my_unread():
    return db.fetch_one(
        "SELECT COUNT(*) AS c FROM pager_recipients r JOIN pager_alerts a ON a.id = r.alert_id "
        "WHERE r.user_id=? AND r.ack_at IS NULL AND a.withdrawn=0", (g.user["id"],))["c"]


# ---------------------------------------------------------------- everyone: My Alerts
@pager_bp.route("/pager/")
def inbox():
    uid = g.user["id"]
    rows = db.fetch_all(
        "SELECT a.*, r.seen_at, r.ack_at FROM pager_recipients r JOIN pager_alerts a ON a.id = r.alert_id "
        "WHERE r.user_id=? AND a.withdrawn=0 ORDER BY a.id DESC LIMIT 100", (uid,))
    alerts = []
    for r in rows:
        v = _alert_view(r)
        v["confirmed"] = bool(r["ack_at"])
        v["confirmed_at"] = nice_stamp(r["ack_at"]) if r["ack_at"] else ""
        alerts.append(v)
    # Opening the page counts as having seen them (so no pop-up repeats them).
    db.execute("UPDATE pager_recipients SET seen_at=? WHERE user_id=? AND seen_at IS NULL", (_now(), uid))
    row = db.fetch_one("SELECT phone FROM pager_contacts WHERE user_id=?", (uid,))
    return render_template("pager/inbox.html", alerts=alerts, unread=sum(1 for a in alerts if not a["confirmed"]),
                           my_phone=(row["phone"] if row else "") or "", sms=sms_status_info())


def _confirm(alert_id):
    """Mark one alert confirmed for the signed-in person. True if they were a recipient."""
    uid = g.user["id"]
    now = _now()
    exists = db.fetch_one("SELECT 1 AS x FROM pager_recipients WHERE alert_id=? AND user_id=?", (alert_id, uid))
    if not exists:
        return False
    db.execute("UPDATE pager_recipients SET ack_at=COALESCE(ack_at, ?), seen_at=COALESCE(seen_at, ?) "
               "WHERE alert_id=? AND user_id=?", (now, now, alert_id, uid))
    return True


@pager_bp.route("/pager/ack/<int:alert_id>", methods=["POST"])
def ack(alert_id):
    if not _confirm(alert_id):
        flash("That alert wasn't sent to you.", "error")
    return redirect(request.referrer or url_for("pager.inbox"))


@pager_bp.route("/api/pager/ack/<int:alert_id>", methods=["POST"])
def api_ack(alert_id):
    ok = _confirm(alert_id)
    return jsonify({"ok": ok, "unread": _my_unread()}), (200 if ok else 404)


@pager_bp.route("/api/pager/summary")
def api_summary():
    """Polled by every page: how many alerts are waiting, and any that haven't
    been shown to this person yet (each is returned once, then marked seen)."""
    uid = g.user["id"]
    fresh = db.fetch_all(
        "SELECT a.* FROM pager_recipients r JOIN pager_alerts a ON a.id = r.alert_id "
        "WHERE r.user_id=? AND r.seen_at IS NULL AND r.ack_at IS NULL AND a.withdrawn=0 "
        "ORDER BY a.id DESC LIMIT 3", (uid,))
    if fresh:
        now = _now()
        db.execute_many([("UPDATE pager_recipients SET seen_at=? WHERE alert_id=? AND user_id=?",
                          (now, a["id"], uid)) for a in fresh])
    return jsonify({"ok": True, "unread": _my_unread(), "alerts": [_alert_view(a) for a in fresh]})


@pager_bp.route("/pager/my-phone", methods=["POST"])
def my_phone():
    err = _save_phone(g.user["id"], request.form.get("phone", ""))
    flash(err or "Your phone number was saved.", "error" if err else "success")
    return redirect(url_for("pager.inbox"))


def _save_phone(user_id, raw):
    """Save (or clear) one person's number. Returns an error message or None."""
    phone = normalize_phone(raw)
    if not phone:
        db.execute("DELETE FROM pager_contacts WHERE user_id=?", (user_id,))
        return None
    if not valid_phone(phone):
        return "That doesn't look like a phone number. Try 0712 345 678 or +254 712 345 678."
    db.execute("INSERT INTO pager_contacts (user_id, phone) VALUES (?, ?) "
               "ON CONFLICT (user_id) DO UPDATE SET phone = EXCLUDED.phone", (user_id, phone))
    return None


# ---------------------------------------------------------------- senders: compose + send
def _render_compose(values=None, status=200):
    roles = [{"key": k, "name": v["name"]} for k, v in auth.load_roles().items()]
    staff = _staff()
    for r in roles:
        r["count"] = sum(1 for s in staff if s["role"] == r["key"])
    values = values or {}
    return render_template(
        "pager/compose.html", kinds=KINDS, roles=roles, staff=staff, sms=sms_status_info(),
        school_name=_school_name(), me=g.user["id"], v=values,
        picked={int(x) for x in values.get("people", []) if str(x).isdigit()},
        picked_roles=set(values.get("roles", [])), sms_max=SMS_MAX_CHARS,
        today=_now()[:10]), status


@pager_bp.route("/pager/send", methods=["GET"])
def compose():
    return _render_compose()


def resolve_audience(form, sender_id):
    """-> (recipient users, human summary). 'Everyone' and roles leave the sender
    out; people ticked by name are always included."""
    staff = _staff()
    by_id = {s["id"]: s for s in staff}
    chosen = {}
    summary = []
    if form.get("everyone"):
        for s in staff:
            if s["id"] != sender_id:
                chosen[s["id"]] = s
        summary.append("Everyone")
    else:
        valid_roles = auth.load_roles()
        role_keys = [k for k in form.getlist("roles") if k in valid_roles]
        for s in staff:
            if s["role"] in role_keys and s["id"] != sender_id:
                chosen[s["id"]] = s
        summary += [valid_roles[k]["name"] for k in role_keys]
    picked = [int(x) for x in form.getlist("people") if x.isdigit() and int(x) in by_id]
    for pid in picked:
        chosen[pid] = by_id[pid]
    if picked:
        summary.append(f"{len(picked)} named {'person' if len(picked) == 1 else 'people'}")
    return list(chosen.values()), ", ".join(summary)


@pager_bp.route("/pager/send", methods=["POST"])
def send():
    f = request.form
    kind = f.get("kind") if f.get("kind") in KINDS else "notice"
    title = f.get("title", "").strip()[:120]
    body = f.get("body", "").strip()[:1000]
    venue = f.get("venue", "").strip()[:100]
    event_date, event_time = _clean_date(f.get("event_date")), _clean_time(f.get("event_time"))
    want_sms = bool(f.get("send_sms"))
    recipients, audience = resolve_audience(f, g.user["id"])

    problem = None
    if not title:
        problem = "Give the alert a title."
    elif event_date is None:
        problem = "That date isn't valid."
    elif event_time is None:
        problem = "That time isn't valid."
    elif not recipients:
        problem = "Choose who should get this alert (everyone, a role, or tick some people)."
    elif want_sms and not sms_config()["configured"]:
        problem = "SMS isn't connected yet, so it can't be sent. Untick \"Also send by SMS\" or connect SMS first."
    if problem:
        flash(problem, "error")
        values = {k: f.get(k, "") for k in ("kind", "title", "body", "event_date", "event_time", "venue")}
        values.update(everyone=bool(f.get("everyone")), roles=f.getlist("roles"),
                      people=f.getlist("people"), send_sms=want_sms)
        return _render_compose(values, 400)

    alert_id = db.execute(
        "INSERT INTO pager_alerts (kind, title, body, event_date, event_time, venue, audience, sent_by, "
        "sent_by_name, created_at, sms_requested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, title, body or None, event_date or None, event_time or None, venue or None, audience,
         g.user["id"], g.user["full_name"], _now(), 1 if want_sms else 0))
    db.execute_many([("INSERT INTO pager_recipients (alert_id, user_id, phone) VALUES (?, ?, ?)",
                      (alert_id, r["id"], r["phone"] or None)) for r in recipients])

    message = f"Alert sent to {len(recipients)} {'person' if len(recipients) == 1 else 'people'} in the app."
    if want_sms:
        text = sms_text(_school_name(), kind, title, event_date, event_time, venue, body)
        counts = _apply_sms(alert_id, [{"user_id": r["id"], "phone": r["phone"]} for r in recipients], text)
        message += " " + _sms_summary(counts)
        flash(message, "error" if counts["failed"] else "success")
    else:
        flash(message, "success")
    return redirect(url_for("pager.detail", alert_id=alert_id))


# ---------------------------------------------------------------- senders: history + detail
@pager_bp.route("/pager/sent")
def sent():
    rows = db.fetch_all(
        "SELECT a.*, "
        "(SELECT COUNT(*) FROM pager_recipients r WHERE r.alert_id=a.id) AS total, "
        "(SELECT COUNT(*) FROM pager_recipients r WHERE r.alert_id=a.id AND r.ack_at IS NOT NULL) AS confirmed, "
        "(SELECT COUNT(*) FROM pager_recipients r WHERE r.alert_id=a.id AND r.sms_status='sent') AS sms_sent, "
        "(SELECT COUNT(*) FROM pager_recipients r WHERE r.alert_id=a.id AND r.sms_status='failed') AS sms_failed "
        "FROM pager_alerts a ORDER BY a.id DESC LIMIT 60")
    items = []
    for a in rows:
        v = _alert_view(a)
        v.update(total=a["total"], confirmed=a["confirmed"], sms_sent=a["sms_sent"],
                 sms_failed=a["sms_failed"], audience=a["audience"] or "", withdrawn=bool(a["withdrawn"]),
                 sms_requested=bool(a["sms_requested"]))
        items.append(v)
    return render_template("pager/sent.html", items=items)


@pager_bp.route("/pager/sent/<int:alert_id>")
def detail(alert_id):
    a = db.fetch_one("SELECT * FROM pager_alerts WHERE id=?", (alert_id,))
    if not a:
        flash("That alert doesn't exist.", "error")
        return redirect(url_for("pager.sent"))
    rows = db.fetch_all(
        "SELECT r.*, u.full_name, u.role FROM pager_recipients r JOIN users u ON u.id = r.user_id "
        "WHERE r.alert_id=? ORDER BY (r.ack_at IS NULL) DESC, u.full_name", (alert_id,))
    roles = auth.role_names()
    people = [{
        "name": r["full_name"], "role_name": roles.get(r["role"], r["role"]),
        "state": "confirmed" if r["ack_at"] else ("seen" if r["seen_at"] else "waiting"),
        "at": nice_stamp(r["ack_at"] or r["seen_at"] or ""),
        "sms_status": r["sms_status"], "sms_detail": r["sms_detail"] or "",
        "phone": r["phone"] or "",
    } for r in rows]
    stats = {
        "total": len(people),
        "confirmed": sum(1 for p in people if p["state"] == "confirmed"),
        "seen": sum(1 for p in people if p["state"] == "seen"),
        "waiting": sum(1 for p in people if p["state"] == "waiting"),
        "sms_sent": sum(1 for p in people if p["sms_status"] == "sent"),
        "sms_failed": sum(1 for p in people if p["sms_status"] == "failed"),
        "sms_none": sum(1 for p in people if p["sms_status"] == "no_number"),
    }
    v = _alert_view(a)
    v.update(audience=a["audience"] or "", withdrawn=bool(a["withdrawn"]), sms_requested=bool(a["sms_requested"]))
    return render_template("pager/detail.html", a=v, people=people, stats=stats, sms=sms_status_info(),
                           sms_text=sms_text(_school_name(), a["kind"], a["title"], a["event_date"],
                                             a["event_time"], a["venue"], a["body"]))


@pager_bp.route("/pager/sent/<int:alert_id>/withdraw", methods=["POST"])
def withdraw(alert_id):
    db.execute("UPDATE pager_alerts SET withdrawn=1 WHERE id=?", (alert_id,))
    flash("The alert was withdrawn. It no longer shows in anyone's alerts. (SMS already sent can't be recalled.)",
          "success")
    return redirect(url_for("pager.detail", alert_id=alert_id))


@pager_bp.route("/pager/sent/<int:alert_id>/retry-sms", methods=["POST"])
def retry_sms(alert_id):
    a = db.fetch_one("SELECT * FROM pager_alerts WHERE id=?", (alert_id,))
    if not a or not a["sms_requested"] or a["withdrawn"]:
        flash("There is nothing to resend for that alert.", "error")
        return redirect(url_for("pager.sent"))
    if not sms_config()["configured"]:
        flash("SMS isn't connected yet.", "error")
        return redirect(url_for("pager.detail", alert_id=alert_id))
    rows = db.fetch_all(
        "SELECT r.user_id, COALESCE(c.phone, r.phone) AS phone FROM pager_recipients r "
        "LEFT JOIN pager_contacts c ON c.user_id = r.user_id "
        "WHERE r.alert_id=? AND r.sms_status IN ('failed', 'no_number')", (alert_id,))
    rows = [{"user_id": r["user_id"], "phone": normalize_phone(r["phone"])} for r in rows]
    if not rows:
        flash("Every SMS for this alert went through.", "success")
        return redirect(url_for("pager.detail", alert_id=alert_id))
    text = sms_text(_school_name(), a["kind"], a["title"], a["event_date"], a["event_time"], a["venue"], a["body"])
    counts = _apply_sms(alert_id, rows, text)
    flash("Tried again. " + _sms_summary(counts), "error" if counts["failed"] else "success")
    return redirect(url_for("pager.detail", alert_id=alert_id))


# ---------------------------------------------------------------- senders: phone numbers
@pager_bp.route("/pager/contacts")
def contacts():
    return render_template("pager/contacts.html", staff=_staff(), sms=sms_status_info())


@pager_bp.route("/pager/contacts", methods=["POST"])
def contacts_save():
    bad = []
    for s in _staff():
        key = f"phone_{s['id']}"
        if key not in request.form:
            continue
        if normalize_phone(request.form[key]) == s["phone"]:
            continue
        if _save_phone(s["id"], request.form[key]):
            bad.append(s["name"])
    if bad:
        flash("These numbers weren't saved because they don't look like phone numbers: " + ", ".join(bad) + ".",
              "error")
    else:
        flash("Phone numbers saved.", "success")
    return redirect(url_for("pager.contacts"))


@pager_bp.route("/pager/contacts/test-sms", methods=["POST"])
def sms_test():
    if not sms_config()["configured"]:
        flash("SMS isn't connected yet.", "error")
        return redirect(url_for("pager.contacts"))
    row = db.fetch_one("SELECT phone FROM pager_contacts WHERE user_id=?", (g.user["id"],))
    phone = normalize_phone(row["phone"]) if row else ""
    if not valid_phone(phone):
        flash("Save your own phone number first (type it in the list below), then try the test again.", "error")
        return redirect(url_for("pager.contacts"))
    ok, detail = send_sms([(g.user["id"], phone)], f"{_school_name()}: this is a test SMS from ShuleHub.")[g.user["id"]]
    flash(f"Test SMS to {phone}: {detail}." if ok else f"The test SMS to {phone} failed: {detail}.",
          "success" if ok else "error")
    return redirect(url_for("pager.contacts"))
