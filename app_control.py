"""
ShuleHub Control - your private dashboard for ShuleHub clients.

  Tracked links   Send a link instead of a file. You see when it was opened, from how many devices,
                  and whether the file was downloaded. Link previews made by WhatsApp, Telegram,
                  email scanners and the like are recognised and NOT counted as a person opening it.
  Schools         Make a licence key per school. ShuleHub sends a small check-in (school name, key,
                  version, computer type, a random install id) and you see who is active, which
                  version they run, and whether one key is being used on several computers.

Only you can log in (password in the SHUBCTL_PASSWORD environment variable).
Nothing about students, fees, marks or any school record ever reaches this server.

Run locally:   set SHUBCTL_PASSWORD=your-password   then   python app.py     (http://127.0.0.1:8000)
Deploy:        see README.txt
"""
import datetime as dt
import hashlib
import os
import re
import secrets
import sqlite3

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SHUBCTL_DATA", BASE)
UPLOADS = os.path.join(DATA, "uploads")
DB_PATH = os.path.join(DATA, "control.db")
TZ_HOURS = float(os.environ.get("SHUBCTL_TZ_HOURS", "3"))          # shown in East Africa Time by default
TRUST_PROXY = os.environ.get("SHUBCTL_TRUST_PROXY", "1") == "1"      # hosts such as PythonAnywhere/Render sit behind a proxy
SILENT_DAYS = 14                                                      # "quiet" schools: no check-in for this long

os.makedirs(UPLOADS, exist_ok=True)
from licence import licence_bp
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024


def _secret():
    path = os.path.join(DATA, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    return open(path).read().strip()


app.secret_key = os.environ.get("SHUBCTL_SECRET") or _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

BOT_RE = re.compile(
    r"whatsapp|facebookexternalhit|facebot|slackbot|telegrambot|twitterbot|linkedinbot|discordbot|googlebot|"
    r"bingbot|skypeuripreview|googleimageproxy|applebot|mail\.ru|curl/|python-requests|headlesschrome|"
    r"microsoft office|outlook|barracuda|proofpoint|mimecast|safelinks", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, recipient TEXT NOT NULL, contact TEXT, note TEXT,
  file_name TEXT, file_path TEXT, url TEXT, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS link_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, link_id INTEGER NOT NULL REFERENCES links(id), at TEXT NOT NULL,
  kind TEXT NOT NULL, ip TEXT, ua TEXT, device TEXT);
CREATE TABLE IF NOT EXISTS licences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, school TEXT NOT NULL, contact TEXT, note TEXT,
  notice TEXT, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY AUTOINCREMENT, licence_id INTEGER REFERENCES licences(id), key TEXT, at TEXT NOT NULL,
  install_id TEXT, school_reported TEXT, version TEXT, os TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS ix_events_link ON link_events(link_id);
CREATE INDEX IF NOT EXISTS ix_checkins_lic ON checkins(licence_id);
"""


# ------------------------------------------------------------------ helpers
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.executescript(SCHEMA)
    return g.db


@app.teardown_appcontext
def _close(_):
    conn = g.pop("db", None)
    if conn:
        conn.commit()
        conn.close()


def now():
    return dt.datetime.utcnow().replace(microsecond=0)


def iso(t=None):
    return (t or now()).isoformat(sep=" ")


def parse(s):
    return dt.datetime.fromisoformat(s) if s else None


@app.template_filter("local")
def local(s):
    t = parse(s)
    return (t + dt.timedelta(hours=TZ_HOURS)).strftime("%d %b %Y, %H:%M") if t else "never"


@app.template_filter("ago")
def ago(s):
    t = parse(s)
    if not t:
        return "never"
    secs = int((now() - t).total_seconds())
    for name, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            v = secs // n
            return f"{v} {name}{'' if v == 1 else 's'} ago"
    return "just now"


def client_ip():
    if TRUST_PROXY and request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[0].strip()[:60]
    return (request.remote_addr or "")[:60]


def device_id(ip, ua):
    return hashlib.sha1(f"{ip}|{ua}".encode()).hexdigest()[:8]


def short_ua(ua):
    ua = ua or ""
    os_ = next((n for p, n in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                               ("Mac OS", "Mac"), ("Linux", "Linux")) if p in ua), "Unknown device")
    br = next((n for p, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"),
                              ("Safari/", "Safari")) if p in ua), "browser")
    return f"{br} on {os_}"


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def guard():
    ep = request.endpoint or ""
    if ep in ("static", "landing", "download", "api_checkin", "health", "login") or ep.startswith("static"):
        return
    if not session.get("admin"):
        return redirect(url_for("login", next=request.path))
    if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x")):
        abort(400)


# ------------------------------------------------------------------ login
@app.route("/login", methods=["GET", "POST"])
def login():
    expected = os.environ.get("SHUBCTL_PASSWORD", "")
    if request.method == "POST":
        if expected and secrets.compare_digest(request.form.get("password", ""), expected):
            session.clear()
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("home"))
        flash("Wrong password." if expected else "SHUBCTL_PASSWORD is not set on the server.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/health")
def health():
    return "ok"


# ------------------------------------------------------------------ dashboard
def link_stats():
    return db().execute("""
        SELECT l.*,
          (SELECT COUNT(*) FROM link_events e WHERE e.link_id=l.id AND e.kind='view') AS views,
          (SELECT COUNT(DISTINCT device) FROM link_events e WHERE e.link_id=l.id AND e.kind IN ('view','download')) AS devices,
          (SELECT COUNT(*) FROM link_events e WHERE e.link_id=l.id AND e.kind='download') AS downloads,
          (SELECT COUNT(*) FROM link_events e WHERE e.link_id=l.id AND e.kind='preview') AS previews,
          (SELECT MAX(at) FROM link_events e WHERE e.link_id=l.id AND e.kind IN ('view','download')) AS last_open
        FROM links l ORDER BY l.id DESC""").fetchall()


def licence_stats():
    return db().execute("""
        SELECT l.*,
          (SELECT MAX(at) FROM checkins c WHERE c.licence_id=l.id) AS last_seen,
          (SELECT COUNT(DISTINCT install_id) FROM checkins c WHERE c.licence_id=l.id) AS installs,
          (SELECT COUNT(*) FROM checkins c WHERE c.licence_id=l.id) AS pings,
          (SELECT version FROM checkins c WHERE c.licence_id=l.id ORDER BY at DESC, id DESC LIMIT 1) AS version
        FROM licences l ORDER BY l.school COLLATE NOCASE""").fetchall()


@app.route("/")
def home():
    links, lics = link_stats(), licence_stats()
    cutoff = iso(now() - dt.timedelta(days=SILENT_DAYS))
    quiet = [l for l in lics if l["active"] and l["last_seen"] and l["last_seen"] < cutoff]
    waiting = [l for l in lics if l["active"] and not l["last_seen"]]
    unopened = [l for l in links if l["active"] and not l["last_open"]]
    unknown = db().execute("SELECT key, COUNT(*) n, MAX(at) last FROM checkins WHERE licence_id IS NULL GROUP BY key ORDER BY last DESC LIMIT 10").fetchall()
    recent = db().execute("""
        SELECT e.at, 'link' AS src, l.recipient AS who, e.kind AS what, e.ip FROM link_events e JOIN links l ON l.id=e.link_id WHERE e.kind!='preview'
        UNION ALL
        SELECT c.at, 'school', COALESCE(l.school, 'Unknown key ' || COALESCE(c.key,'')), 'check-in v' || COALESCE(c.version,'?'), c.ip
        FROM checkins c LEFT JOIN licences l ON l.id=c.licence_id
        ORDER BY at DESC LIMIT 25""").fetchall()
    active_week = sum(1 for l in lics if l["last_seen"] and l["last_seen"] >= iso(now() - dt.timedelta(days=7)))
    return render_template("home.html", links=links, lics=lics, quiet=quiet, waiting=waiting, unopened=unopened,
                           unknown=unknown, recent=recent, active_week=active_week, silent_days=SILENT_DAYS)


# ------------------------------------------------------------------ tracked links
@app.route("/links")
def links():
    return render_template("links.html", links=link_stats())


@app.route("/links/new", methods=["POST"])
def link_new():
    recipient = request.form.get("recipient", "").strip()[:120]
    if not recipient:
        flash("Enter who this link is for.", "error")
        return redirect(url_for("links"))
    url = request.form.get("url", "").strip()[:500]
    f = request.files.get("file")
    file_name = file_path = None
    if f and f.filename:
        file_name = secure_filename(f.filename) or "file"
        file_path = f"{secrets.token_hex(6)}_{file_name}"
        f.save(os.path.join(UPLOADS, file_path))
    elif not re.match(r"^https?://", url):
        flash("Upload the file, or paste a web address (https://...) to send people to.", "error")
        return redirect(url_for("links"))
    token = secrets.token_urlsafe(9)
    db().execute("INSERT INTO links (token, recipient, contact, note, file_name, file_path, url, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (token, recipient, request.form.get("contact", "").strip()[:120], request.form.get("note", "").strip()[:300],
                  file_name, file_path, url if not file_path else None, iso()))
    flash("Link created. Copy it from the list and send it to " + recipient + ".", "success")
    return redirect(url_for("links"))


@app.route("/links/<int:lid>")
def link_detail(lid):
    link = db().execute("SELECT * FROM links WHERE id=?", (lid,)).fetchone() or abort(404)
    events = db().execute("SELECT * FROM link_events WHERE link_id=? ORDER BY id DESC LIMIT 200", (lid,)).fetchall()
    devices = db().execute("""SELECT device, MIN(ip) ip, MIN(ua) ua, MIN(at) first, MAX(at) last, COUNT(*) n
                              FROM link_events WHERE link_id=? AND kind IN ('view','download') GROUP BY device ORDER BY first""", (lid,)).fetchall()
    return render_template("link_detail.html", link=link, events=events, devices=devices, short_ua=short_ua,
                           public=request.host_url.rstrip("/") + url_for("landing", token=link["token"]))


@app.route("/links/<int:lid>/toggle", methods=["POST"])
def link_toggle(lid):
    db().execute("UPDATE links SET active = 1 - active WHERE id=?", (lid,))
    return redirect(request.referrer or url_for("links"))


def _log(link_id, kind):
    ip, ua = client_ip(), (request.headers.get("User-Agent") or "")[:300]
    if BOT_RE.search(ua):
        kind = "preview"
    db().execute("INSERT INTO link_events (link_id, at, kind, ip, ua, device) VALUES (?,?,?,?,?,?)",
                 (link_id, iso(), kind, ip, ua, device_id(ip, ua)))


@app.route("/d/<token>")
def landing(token):
    link = db().execute("SELECT * FROM links WHERE token=?", (token,)).fetchone() or abort(404)
    if not link["active"]:
        return render_template("gone.html"), 410
    _log(link["id"], "view")
    return render_template("landing.html", link=link, size=_size(link))


@app.route("/d/<token>/get")
def download(token):
    link = db().execute("SELECT * FROM links WHERE token=?", (token,)).fetchone() or abort(404)
    if not link["active"]:
        return render_template("gone.html"), 410
    _log(link["id"], "download")
    db().commit()
    if link["file_path"]:
        return send_file(os.path.join(UPLOADS, link["file_path"]), as_attachment=True, download_name=link["file_name"])
    return redirect(link["url"])


def _size(link):
    try:
        n = os.path.getsize(os.path.join(UPLOADS, link["file_path"]))
        return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"
    except Exception:
        return ""


# ------------------------------------------------------------------ schools / licences
def new_key():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "SHUB-" + "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


@app.route("/schools")
def schools():
    return render_template("schools.html", lics=licence_stats(), silent_days=SILENT_DAYS, cutoff=iso(now() - dt.timedelta(days=SILENT_DAYS)))


@app.route("/schools/new", methods=["POST"])
def school_new():
    school = request.form.get("school", "").strip()[:120]
    if not school:
        flash("Enter the school name.", "error")
        return redirect(url_for("schools"))
    key = new_key()
    db().execute("INSERT INTO licences (key, school, contact, note, created_at) VALUES (?,?,?,?,?)",
                 (key, school, request.form.get("contact", "").strip()[:120], request.form.get("note", "").strip()[:300], iso()))
    flash(f"Licence key for {school}: {key}. Give it to the school; they enter it in ShuleHub under Menu > Licence & Check-in.", "success")
    return redirect(url_for("schools"))


@app.route("/schools/<int:lid>")
def school_detail(lid):
    lic = db().execute("SELECT * FROM licences WHERE id=?", (lid,)).fetchone() or abort(404)
    pings = db().execute("SELECT * FROM checkins WHERE licence_id=? ORDER BY id DESC LIMIT 100", (lid,)).fetchall()
    machines = db().execute("""SELECT install_id, MIN(at) first, MAX(at) last, COUNT(*) n, MAX(version) version, MAX(os) os, MAX(ip) ip
                               FROM checkins WHERE licence_id=? GROUP BY install_id ORDER BY last DESC""", (lid,)).fetchall()
    return render_template("school_detail.html", lic=lic, pings=pings, machines=machines)


@app.route("/schools/<int:lid>/update", methods=["POST"])
def school_update(lid):
    db().execute("UPDATE licences SET school=?, contact=?, note=?, notice=? WHERE id=?",
                 (request.form.get("school", "").strip()[:120] or "School", request.form.get("contact", "").strip()[:120],
                  request.form.get("note", "").strip()[:300], request.form.get("notice", "").strip()[:300], lid))
    flash("Saved.", "success")
    return redirect(url_for("school_detail", lid=lid))


@app.route("/schools/<int:lid>/toggle", methods=["POST"])
def school_toggle(lid):
    db().execute("UPDATE licences SET active = 1 - active WHERE id=?", (lid,))
    return redirect(url_for("school_detail", lid=lid))


# ------------------------------------------------------------------ the check-in ShuleHub sends
@app.route("/api/checkin", methods=["POST"])
def api_checkin():
    d = request.get_json(silent=True) or {}
    clean = lambda v, n: str(v or "").strip()[:n]
    key, install = clean(d.get("key"), 40).upper(), clean(d.get("install_id"), 64)
    lic = db().execute("SELECT * FROM licences WHERE key=?", (key,)).fetchone() if key else None
    last = db().execute("SELECT at FROM checkins WHERE install_id=? AND key=? ORDER BY id DESC LIMIT 1", (install, key)).fetchone()
    if not (last and (now() - parse(last["at"])).total_seconds() < 120):          # ignore rapid repeats
        db().execute("INSERT INTO checkins (licence_id, key, at, install_id, school_reported, version, os, ip) VALUES (?,?,?,?,?,?,?,?)",
                     (lic["id"] if lic else None, key, iso(), install, clean(d.get("school"), 120), clean(d.get("version"), 20),
                      clean(d.get("os"), 60), client_ip()))
    return jsonify({"ok": bool(lic and lic["active"]), "known": bool(lic),
                    "school": lic["school"] if lic else "", "notice": (lic["notice"] or "") if lic else ""})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
