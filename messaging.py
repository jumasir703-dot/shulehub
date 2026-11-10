"""
messaging.py - Messages: text messages (SMS) and email to guardians.

This file decides WHO gets WHAT; msg_gateway.py only knows HOW to deliver one message.

Flow:  Compose -> Preview (who will get it, how many SMS parts, a sample) -> Send.
Sending queues one row per recipient (msg_deliveries) and the run page then works
through the queue a few at a time from the browser, so a big class list never hits a
timeout, can be paused, and can be resumed or retried later.

Placeholders you can use in a message:  {guardian} {student} {class} {school}
Gateway settings (SMS / email accounts, passwords, API keys) are administrator-only
(see permissions.py: msg.settings*).
"""
import re
from datetime import datetime

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for

import database as db
import msg_gateway as gw

msg_bp = Blueprint("msg", __name__, url_prefix="/messages")

# msg_settings has no id column, so the database layer must not append RETURNING id to its inserts.
_no_id = getattr(db, "TABLES_WITHOUT_ID", None)
if _no_id is not None:
    _no_id.add("msg_settings")

MAX_PER_RUN = 2000
STEP_SIZE = 8
MAX_SMS_PARTS = 10

# ------------------------------------------------------------------ schema
_DDL = """
CREATE TABLE IF NOT EXISTS msg_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS msg_runs (
    id {PK},
    created_at TEXT,
    created_by TEXT,
    channels TEXT,
    audience TEXT,
    subject TEXT,
    body TEXT,
    total INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS msg_deliveries (
    id {PK},
    run_id INTEGER NOT NULL REFERENCES msg_runs(id) ON DELETE CASCADE,
    student_id INTEGER,
    recipient_name TEXT,
    channel TEXT NOT NULL,
    address TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    ref TEXT,
    cost TEXT,
    parts INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_msg_deliveries_run ON msg_deliveries(run_id, status);
"""
_ready = False


def ensure_schema():
    global _ready
    if _ready:
        return
    conn = db.get_connection()
    try:
        ddl = _DDL.replace("{PK}", "SERIAL PRIMARY KEY" if db.USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT")
        if db.USE_POSTGRES:
            conn.cursor().execute(ddl)
        else:
            conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    _ready = True


@msg_bp.before_request
def _before():
    ensure_schema()


# ------------------------------------------------------------------ settings
DEFAULTS = {"sms_provider": "simulated", "email_provider": "simulated", "country_code": "254",
            "at_sandbox": "0", "generic_method": "POST", "generic_content": "form",
            "smtp_port": "587", "smtp_security": "starttls"}
TEXT_KEYS = ["sms_provider", "country_code", "at_username", "at_sender_id", "generic_url", "generic_method",
             "generic_content", "generic_body", "generic_headers", "generic_ok_text", "generic_sender",
             "generic_username", "email_provider", "email_from_address", "email_from_name", "email_reply_to",
             "smtp_host", "smtp_port", "smtp_security", "smtp_user"]
FLAG_KEYS = ["at_sandbox"]
SECRET_KEYS = ["at_api_key", "generic_api_key", "smtp_password", "brevo_api_key"]


def load_settings():
    """The msg_settings rows as a dict (what msg_gateway.py expects as `cfg`)."""
    cfg = dict(DEFAULTS)
    for r in db.fetch_all("SELECT key, value FROM msg_settings"):
        if r["value"] is not None:
            cfg[r["key"]] = r["value"]
    return cfg


def _put(key, value):
    db.execute("INSERT INTO msg_settings (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


def _school():
    try:
        return db.get_school_settings()["name"] or "School"
    except Exception:
        return "School"


def _is_admin():
    return bool(g.user and g.user["role"] == "admin")


def _who():
    return (g.user["full_name"] if g.user else "") or ""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ who gets it
_FIELD = re.compile(r"\{(guardian|student|class|school)\}")
_PERSONAL = re.compile(r"\{(student|class)\}")

QUICK_TEXTS = [
    ("Meeting", "Parents' meeting",
     "Dear {guardian}, you are invited to a parents' meeting at {school}. Date: ___ Time: ___ Venue: ___. Your attendance matters."),
    ("Fees reminder", "Fees reminder",
     "Dear {guardian}, this is a reminder that school fees for {student} ({class}) are due. Please clear the balance as soon as you can. Thank you. {school}"),
    ("Opening / closing", "School dates",
     "Dear {guardian}, {school} closes on ___ and reopens on ___. Please make sure {student} reports on time."),
    ("Results ready", "Exam results",
     "Dear {guardian}, the exam results for {student} ({class}) are ready. Please contact the school office to collect the report form. {school}"),
    ("Notice", "Notice from the school", "Dear {guardian}, ___ . Thank you. {school}"),
]


def _class_options():
    """Whole school, each grade, and each class (grade + stream) that has active students."""
    rows = db.fetch_all(
        "SELECT st.grade_id AS gid, st.stream_id AS sid, g.name AS gname, sm.name AS smname "
        "FROM students st LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "WHERE st.active = 1 GROUP BY st.grade_id, st.stream_id, g.name, sm.name ORDER BY g.name, sm.name")
    grades, classes, seen = [], [], set()
    for r in rows:
        if r["gid"] is not None and r["gid"] not in seen:
            seen.add(r["gid"])
            grades.append({"value": f"grade:{r['gid']}", "label": f"All of {r['gname']}"})
        if r["sid"]:
            classes.append({"value": f"class:{r['gid'] or 0}:{r['sid']}", "label": r["smname"] or r["gname"] or "Class"})
    return grades, classes


def _audience_sql(audience):
    kind, _, rest = (audience or "all").partition(":")
    where, params = "st.active = 1", []
    if kind == "grade" and rest.isdigit():
        where += " AND st.grade_id = ?"
        params.append(int(rest))
    elif kind == "class":
        parts = rest.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            where += " AND st.stream_id = ?"
            params.append(int(parts[1]))
    return where, tuple(params)


def build_recipients(audience, channels, primary_only, body, subject, cfg):
    """-> (items, skipped, duplicates). One item per message to send."""
    where, params = _audience_sql(audience)
    rows = db.fetch_all(
        "SELECT st.id AS sid, st.name AS sname, g.name AS gname, sm.name AS smname, "
        "gu.name AS guname, gu.phone AS phone, gu.email AS email, gu.is_primary AS is_primary, gu.id AS guid "
        "FROM students st LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "LEFT JOIN guardians gu ON gu.student_id = st.id WHERE " + where +
        " ORDER BY st.name, gu.is_primary DESC, gu.id", params)
    students, order = {}, []
    for r in rows:
        s = students.get(r["sid"])
        if s is None:
            s = students[r["sid"]] = {"name": r["sname"], "cls": r["smname"] or r["gname"] or "", "guardians": []}
            order.append(r["sid"])
        if r["guid"] is not None:
            s["guardians"].append(r)

    personal = bool(_PERSONAL.search((body or "") + " " + (subject or "")))
    country = cfg.get("country_code") or "254"
    items, skipped, seen, dupes = [], [], set(), 0
    for sid in order:
        s = students[sid]
        if not s["guardians"]:
            skipped.append((s["name"], "no guardian recorded"))
            continue
        for channel in channels:
            found = False
            for gu in s["guardians"]:
                addr = gw.normalize_phone(gu["phone"], country) if channel == "sms" else gw.valid_email(gu["email"])
                if not addr:
                    continue
                found = True
                key = (channel, addr, sid if personal else None)
                if key in seen:
                    dupes += 1
                else:
                    seen.add(key)
                    items.append({"channel": channel, "address": addr, "sid": sid, "student": s["name"],
                                  "cls": s["cls"], "guardian": gu["guname"] or "Parent/Guardian"})
                if primary_only:
                    break
            if not found:
                skipped.append((s["name"], "no valid phone number" if channel == "sms" else "no valid email address"))
    return items, skipped, dupes


def render_text(text, item, school):
    values = {"guardian": item["guardian"], "student": item["student"], "class": item["cls"], "school": school}
    return _FIELD.sub(lambda m: values[m.group(1)], text or "")


def _form_message():
    f = request.form
    channels = [c for c in ("sms", "email") if f.get(c) == "1"]
    return {"audience": f.get("audience", "all"), "channels": channels, "primary_only": f.get("primary_only") == "1",
            "subject": (f.get("subject") or "").strip(), "body": (f.get("body") or "").strip()}


def _problems(m, cfg):
    out = []
    if not m["channels"]:
        out.append("Tick at least one way to send: text message or email.")
    if not m["body"]:
        out.append("Write the message first.")
    if "email" in m["channels"] and not m["subject"]:
        out.append("An email needs a subject.")
    if "sms" in m["channels"]:
        ok, label, why = gw.sms_ready(cfg)
        if not ok:
            out.append(f"Text messages are not set up ({why}). An administrator can fix this in Messages > Settings.")
        if gw.sms_info(gw.sms_clean(m["body"]))["parts"] > MAX_SMS_PARTS:
            out.append(f"That is too long for a text message (more than {MAX_SMS_PARTS} parts). Shorten it.")
    if "email" in m["channels"]:
        ok, label, why = gw.email_ready(cfg)
        if not ok:
            out.append(f"Email is not set up ({why}). An administrator can fix this in Messages > Settings.")
    return out


# ------------------------------------------------------------------ pages
def _run_counts(run_id):
    c = {"queued": 0, "sending": 0, "sent": 0, "failed": 0}
    for r in db.fetch_all("SELECT status, COUNT(*) AS n FROM msg_deliveries WHERE run_id = ? GROUP BY status", (run_id,)):
        c[r["status"]] = r["n"]
    c["total"] = sum(c.values())
    c["done"] = c["queued"] == 0 and c["sending"] == 0
    return c


def _recent(limit):
    return db.fetch_all(
        "SELECT r.id, r.created_at, r.created_by, r.channels, r.subject, r.body, r.total, "
        "(SELECT COUNT(*) FROM msg_deliveries d WHERE d.run_id = r.id AND d.status = 'sent') AS sent, "
        "(SELECT COUNT(*) FROM msg_deliveries d WHERE d.run_id = r.id AND d.status = 'failed') AS failed, "
        "(SELECT COUNT(*) FROM msg_deliveries d WHERE d.run_id = r.id AND d.status IN ('queued','sending')) AS waiting "
        "FROM msg_runs r ORDER BY r.id DESC LIMIT " + str(int(limit)))


def _status_banner(cfg):
    sms = gw.sms_ready(cfg)
    email = gw.email_ready(cfg)
    return {"sms": sms, "email": email,
            "test_mode": (cfg.get("sms_provider") or "simulated") == "simulated"
                         or (cfg.get("email_provider") or "simulated") == "simulated"}


@msg_bp.route("/")
def index():
    cfg = load_settings()
    grades, classes = _class_options()
    return render_template("messaging/index.html", grades=grades, classes=classes, quick=QUICK_TEXTS,
                           banner=_status_banner(cfg), recent=_recent(5), is_admin=_is_admin(),
                           form=request.args, school=_school())


@msg_bp.route("/history")
def history():
    return render_template("messaging/history.html", runs=_recent(100), is_admin=_is_admin())


@msg_bp.route("/preview", methods=["POST"])
def preview():
    cfg = load_settings()
    m = _form_message()
    problems = _problems(m, cfg)
    if problems:
        for p in problems:
            flash(p, "error")
        return redirect(url_for("msg.index", **{k: v for k, v in request.form.items() if k != "_csrf"}))
    items, skipped, dupes = build_recipients(m["audience"], m["channels"], m["primary_only"], m["body"], m["subject"], cfg)
    if not items:
        flash("Nobody can receive this message: no guardian has a valid phone number or email for the people you chose.", "error")
        return redirect(url_for("msg.index", **{k: v for k, v in request.form.items() if k != "_csrf"}))
    school = _school()
    sample = items[0]
    sample_text = render_text(m["body"], sample, school)
    per_channel = {c: sum(1 for i in items if i["channel"] == c) for c in m["channels"]}
    sms_parts = 0
    if "sms" in m["channels"]:
        sms_parts = sum(max(gw.sms_info(gw.sms_clean(render_text(m["body"], i, school)))["parts"], 1)
                        for i in items if i["channel"] == "sms")
    return render_template("messaging/preview.html", m=m, items=items, per_channel=per_channel, skipped=skipped,
                           dupes=dupes, sample=sample, sample_text=sample_text,
                           sample_subject=render_text(m["subject"], sample, school), sms_parts=sms_parts,
                           too_many=len(items) > MAX_PER_RUN, max_per_run=MAX_PER_RUN,
                           banner=_status_banner(cfg), form=request.form)


@msg_bp.route("/send", methods=["POST"])
def send():
    cfg = load_settings()
    m = _form_message()
    problems = _problems(m, cfg)
    if problems:
        for p in problems:
            flash(p, "error")
        return redirect(url_for("msg.index"))
    items, _skipped, _dupes = build_recipients(m["audience"], m["channels"], m["primary_only"], m["body"], m["subject"], cfg)
    if not items:
        flash("Nobody can receive this message.", "error")
        return redirect(url_for("msg.index"))
    if len(items) > MAX_PER_RUN:
        flash(f"That is {len(items)} messages. Send at most {MAX_PER_RUN} at a time: choose a smaller group.", "error")
        return redirect(url_for("msg.index"))
    school = _school()
    run_id = db.execute(
        "INSERT INTO msg_runs (created_at, created_by, channels, audience, subject, body, total) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_now(), _who(), ",".join(m["channels"]), m["audience"], m["subject"], m["body"], len(items)))
    for it in items:
        text = render_text(m["body"], it, school)
        text = gw.sms_clean(text) if it["channel"] == "sms" else text
        subj = render_text(m["subject"], it, school) if it["channel"] == "email" else ""
        parts = max(gw.sms_info(text)["parts"], 1) if it["channel"] == "sms" else 1
        db.execute(
            "INSERT INTO msg_deliveries (run_id, student_id, recipient_name, channel, address, subject, body, status, parts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (run_id, it["sid"], it["guardian"], it["channel"], it["address"], subj, text, parts))
    return redirect(url_for("msg.run_page", run_id=run_id))


@msg_bp.route("/run/<int:run_id>")
def run_page(run_id):
    run = db.fetch_one("SELECT * FROM msg_runs WHERE id = ?", (run_id,))
    if not run:
        flash("That message run was not found.", "error")
        return redirect(url_for("msg.history"))
    failed = db.fetch_all("SELECT recipient_name, channel, address, error FROM msg_deliveries "
                          "WHERE run_id = ? AND status = 'failed' ORDER BY id LIMIT 200", (run_id,))
    return render_template("messaging/run.html", run=run, counts=_run_counts(run_id), failed=failed)


@msg_bp.route("/run/<int:run_id>/step", methods=["POST"])
def run_step(run_id):
    cfg = load_settings()
    rows = db.fetch_all("SELECT * FROM msg_deliveries WHERE run_id = ? AND status = 'queued' ORDER BY id LIMIT "
                        + str(STEP_SIZE), (run_id,))
    for r in rows:
        # claim the row first so two open tabs can never send the same message twice
        if not db.execute("UPDATE msg_deliveries SET status = 'sending' WHERE id = ? AND status = 'queued'", (r["id"],)):
            continue
        try:
            if r["channel"] == "sms":
                res = gw.send_sms(cfg, r["address"], r["body"])
            else:
                res = gw.send_email(cfg, r["address"], r["subject"], r["body"], r["recipient_name"])
        except Exception as err:  # a bug in a gateway must not strand the row
            res = gw.Result(False, f"Unexpected error: {err}")
        attempts = (r["attempts"] or 0) + 1
        if res.ok:
            db.execute("UPDATE msg_deliveries SET status = 'sent', ref = ?, cost = ?, parts = ?, attempts = ?, sent_at = ?, "
                       "error = NULL WHERE id = ?", (res.ref, res.cost, res.parts, attempts, _now(), r["id"]))
        elif res.retry and attempts < 3:
            db.execute("UPDATE msg_deliveries SET status = 'queued', attempts = ?, error = ? WHERE id = ?",
                       (attempts, res.error, r["id"]))
        else:
            db.execute("UPDATE msg_deliveries SET status = 'failed', attempts = ?, error = ? WHERE id = ?",
                       (attempts, res.error, r["id"]))
    return jsonify(_run_counts(run_id))


@msg_bp.route("/run/<int:run_id>/retry", methods=["POST"])
def retry(run_id):
    db.execute("UPDATE msg_deliveries SET status = 'queued', attempts = 0 WHERE run_id = ? AND status IN ('failed', 'sending')",
               (run_id,))
    flash("The failed messages are queued again.", "success")
    return redirect(url_for("msg.run_page", run_id=run_id))


# ------------------------------------------------------------------ gateway settings (administrator only)
@msg_bp.route("/settings")
def settings():
    cfg = load_settings()
    secrets_set = {k: bool(cfg.get(k)) for k in SECRET_KEYS}
    return render_template("messaging/settings.html", cfg=cfg, secrets_set=secrets_set,
                           sms_providers=gw.SMS_PROVIDERS, email_providers=gw.EMAIL_PROVIDERS,
                           banner=_status_banner(cfg))


@msg_bp.route("/settings/save", methods=["POST"])
def settings_save():
    f = request.form
    for k in TEXT_KEYS:
        if k in f:
            _put(k, f.get(k, "").strip())
    for k in FLAG_KEYS:
        _put(k, "1" if f.get(k) == "1" else "0")
    for k in SECRET_KEYS:
        if f.get(k):                                   # blank keeps the saved secret
            _put(k, f.get(k).strip())
        if f.get(k + "__clear") == "1":
            _put(k, "")
    flash("Messaging settings saved.", "success")
    return redirect(url_for("msg.settings"))


@msg_bp.route("/settings/test-sms", methods=["POST"])
def test_sms():
    cfg = load_settings()
    to = gw.normalize_phone(request.form.get("to", ""), cfg.get("country_code") or "254")
    if not to:
        flash("Type a valid mobile number to test with.", "error")
    else:
        res = gw.send_sms(cfg, to, f"Test message from {_school()} via ShuleHub.")
        flash((f"Test text accepted for {to}." + (" (Test mode: nothing was really sent.)" if cfg.get("sms_provider") in (None, "", "simulated") else ""))
              if res.ok else f"Test text failed: {res.error}", "success" if res.ok else "error")
    return redirect(url_for("msg.settings"))


@msg_bp.route("/settings/test-email", methods=["POST"])
def test_email():
    cfg = load_settings()
    to = gw.valid_email(request.form.get("to", ""))
    if not to:
        flash("Type a valid email address to test with.", "error")
    else:
        res = gw.send_email(cfg, to, "ShuleHub test email", f"This is a test email from {_school()} via ShuleHub.")
        flash((f"Test email accepted for {to}." + (" (Test mode: nothing was really sent.)" if cfg.get("email_provider") in (None, "", "simulated") else ""))
              if res.ok else f"Test email failed: {res.error}", "success" if res.ok else "error")
    return redirect(url_for("msg.settings"))
