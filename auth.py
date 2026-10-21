"""
auth.py — staff logins, roles and the access gate for the whole app.

Roles
-----
Every login has one role, and a role is a set of "services" (see
permissions.py) that the administrator ticks on the Roles & Access page.
  admin    always everything, cannot be edited
  teacher  starts as: timetable viewing, exam analysis, attendance, student records
  bursar   starts as: fees & payments, student records
The administrator can edit Teacher and Bursar and create any other roles
(Deputy Principal, Secretary, Exams Officer, ...).

The gate (`gate()`) runs before every request. It
  1. sends a brand-new install (no users yet) to /setup to create the first admin,
  2. sends anyone who isn't logged in to /login,
  3. checks the logged-in role's services against the page being opened
     (a page with no service in permissions.py is admin-only, so new routes
     are safe by default),
  4. checks the CSRF token on every POST.

Passwords are stored as salted hashes (werkzeug), never in clear.
"""

import hmac
import os
import secrets
import re
import time
from datetime import datetime, timedelta

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import database as db
import permissions

auth_bp = Blueprint("auth", __name__)

MIN_PASSWORD = 8
SESSION_HOURS = 12

# ---------------------------------------------------------------- policy
# Endpoints anyone can open without logging in. "logo_image" is the school
# logo, which also appears on the sign-in page.
PUBLIC = {"auth.login", "auth.setup", "offline_page", "guide_page", "get_app_page", "static", "logo_image"}

# Endpoints every logged-in user can open.
COMMON = {"home", "auth.logout", "auth.account", "auth.account_password",
          # "View example": every signed-in role may look at the sample school
          "demo.index", "demo.start", "demo.exit_demo"}


def load_roles():
    """{role_key: {key, name, builtin, services:set}} — read once per request,
    so a change the administrator saves applies to everyone's very next click."""
    cached = getattr(g, "_roles", None)
    if cached is not None:
        return cached
    rows = db.fetch_all(
        "SELECT role_key, name, services, builtin FROM roles ORDER BY "
        "CASE role_key WHEN 'admin' THEN 0 WHEN 'teacher' THEN 1 WHEN 'bursar' THEN 2 ELSE 3 END, name")
    roles = {}
    for r in rows:
        listed = [x for x in (r["services"] or "").split(",") if x]
        full = r["role_key"] == "admin" or "*" in listed
        roles[r["role_key"]] = {
            "key": r["role_key"], "name": r["name"], "builtin": bool(r["builtin"]),
            "services": set(permissions.SERVICE_KEYS) if full else set(listed) & set(permissions.SERVICE_KEYS),
            "full": full,
        }
    g._roles = roles
    return roles


def role_names():
    return {k: v["name"] for k, v in load_roles().items()}


def has_service(user, service):
    if not user:
        return False
    if user["role"] == "admin":
        return True
    role = load_roles().get(user["role"])
    return bool(role) and service in role["services"]


def allowed(role, endpoint):
    if role == "admin":
        return True
    if endpoint in COMMON:
        return True
    needed = permissions.services_for(endpoint)
    if not needed:
        return False  # not mapped to any service: administrator only
    info = load_roles().get(role)
    return bool(info) and any(s in info["services"] for s in needed)


# ---------------------------------------------------------------- CSRF
def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = session["_csrf"] = secrets.token_urlsafe(32)
    return token


def _csrf_ok():
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf") or ""
    return bool(expected) and hmac.compare_digest(sent, expected)


# ---------------------------------------------------------------- helpers
_has_users = False


def users_exist():
    """True once at least one login exists (it can never go back to zero:
    the last active administrator can't be removed)."""
    global _has_users
    if not _has_users:
        row = db.fetch_one("SELECT COUNT(*) AS c FROM users")
        _has_users = bool(row and row["c"] > 0)
    return _has_users


def _load_user():
    uid = session.get("uid")
    if not uid:
        return None
    row = db.fetch_one(
        "SELECT id, username, full_name, role, teacher_id, active FROM users WHERE id=?", (uid,))
    if not row or not row["active"]:
        session.pop("uid", None)
        return None
    return dict(row)


def _safe_next(target):
    """Only follow same-site relative paths after login."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


_FAILS = {}  # (username, ip) -> [timestamps]
_MAX_FAILS, _WINDOW = 5, 300


def _throttled(key):
    now = time.time()
    recent = [t for t in _FAILS.get(key, []) if now - t < _WINDOW]
    _FAILS[key] = recent
    return len(recent) >= _MAX_FAILS


def _record_fail(key):
    _FAILS.setdefault(key, []).append(time.time())


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _admin_count(exclude_id=None):
    q = "SELECT COUNT(*) AS c FROM users WHERE role='admin' AND active=1"
    params = ()
    if exclude_id:
        q += " AND id<>?"
        params = (exclude_id,)
    return db.fetch_one(q, params)["c"]


def _check_password(pw, confirm=None):
    if len(pw) < MIN_PASSWORD:
        return f"Use a password of at least {MIN_PASSWORD} characters."
    if confirm is not None and pw != confirm:
        return "The two passwords don't match."
    return None


# ---------------------------------------------------------------- the gate
def gate():
    ep = request.endpoint or ""
    if ep == "static" or request.path.startswith("/static/"):
        return None

    g.user = _load_user()

    if not users_exist():
        if ep == "auth.setup":
            return None
        return redirect(url_for("auth.setup"))

    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _csrf_ok():
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "Session expired — reload the page."}, 400
        flash("Your session expired. Please try again.", "error")
        return redirect(request.referrer or url_for("home"))

    if ep in PUBLIC:
        return None

    if g.user is None:
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "Please log in again."}, 401
        nxt = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("auth.login", next=nxt) if nxt and nxt != "/" else url_for("auth.login"))

    if not allowed(g.user["role"], ep):
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "Not allowed for your role."}, 403
        return render_template("auth/forbidden.html"), 403
    return None


def add_security_headers(response):
    if not request.path.startswith("/static/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def inject_user():
    user = getattr(g, "user", None)
    return {
        "user": user, "csrf_token": csrf_token, "ROLES": role_names(),
        # {% if can('fees') %} in templates
        "can": lambda service: has_service(user, service),
    }


# ---------------------------------------------------------------- first-run + login
@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    if users_exist():
        return redirect(url_for("auth.login"))
    needed = os.environ.get("SETUP_TOKEN", "")
    error = None
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        error = _check_password(pw, request.form.get("confirm", ""))
        if needed and not hmac.compare_digest(request.form.get("setup_token", ""), needed):
            error = "That setup code is not correct."
        elif not full_name or not username:
            error = "Enter your name and a username."
        if not error:
            db.execute(
                "INSERT INTO users (username, full_name, password_hash, role, active, created_at) "
                "VALUES (?, ?, ?, 'admin', 1, ?)",
                (username, full_name, generate_password_hash(pw), _now()))
            global _has_users
            _has_users = True
            flash("Administrator account created. Please log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/setup.html", error=error, needs_token=bool(needed))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        key = (username, request.remote_addr)
        if _throttled(key):
            error = "Too many attempts. Wait a few minutes and try again."
        else:
            row = db.fetch_one("SELECT * FROM users WHERE username=?", (username,))
            if row and row["active"] and check_password_hash(row["password_hash"], request.form.get("password", "")):
                _FAILS.pop(key, None)
                session.clear()
                session["uid"] = row["id"]
                session.permanent = True
                csrf_token()
                db.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), row["id"]))
                return redirect(_safe_next(request.args.get("next")) or url_for("home"))
            _record_fail(key)
            error = "Wrong username or password."
    return render_template("auth/login.html", error=error)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- own account
@auth_bp.route("/account")
def account():
    return render_template("auth/account.html")


@auth_bp.route("/account/password", methods=["POST"])
def account_password():
    row = db.fetch_one("SELECT password_hash FROM users WHERE id=?", (g.user["id"],))
    if not check_password_hash(row["password_hash"], request.form.get("current", "")):
        flash("Your current password is not right.", "error")
        return redirect(url_for("auth.account"))
    new = request.form.get("password", "")
    err = _check_password(new, request.form.get("confirm", ""))
    if err:
        flash(err, "error")
        return redirect(url_for("auth.account"))
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
    flash("Password changed.", "success")
    return redirect(url_for("auth.account"))


# ---------------------------------------------------------------- staff admin
@auth_bp.route("/staff")
def staff():
    users = db.fetch_all(
        "SELECT u.id, u.username, u.full_name, u.role, u.active, u.last_login, t.name AS teacher_name "
        "FROM users u LEFT JOIN teachers t ON u.teacher_id = t.id ORDER BY u.active DESC, u.role, u.full_name")
    teachers = db.fetch_all("SELECT id, name FROM teachers ORDER BY name")
    return render_template("auth/staff.html", users=users, teachers=teachers, roles=list(load_roles().values()))


@auth_bp.route("/staff/add", methods=["POST"])
def staff_add():
    full_name = request.form.get("full_name", "").strip()
    username = request.form.get("username", "").strip().lower()
    role = request.form.get("role", "")
    pw = request.form.get("password", "")
    teacher_id = request.form.get("teacher_id", type=int)
    err = None
    if not full_name or not username:
        err = "Enter a name and a username."
    elif role not in load_roles():
        err = "Choose a role."
    else:
        err = _check_password(pw)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.staff"))
    try:
        db.execute(
            "INSERT INTO users (username, full_name, password_hash, role, teacher_id, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (username, full_name, generate_password_hash(pw), role, teacher_id or None, _now()))
        flash(f"Login created for {full_name}. Give them the username and the temporary password.", "success")
    except Exception:
        flash(f"The username “{username}” is already taken.", "error")
    return redirect(url_for("auth.staff"))


@auth_bp.route("/staff/<int:user_id>/update", methods=["POST"])
def staff_update(user_id):
    row = db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        return redirect(url_for("auth.staff"))
    role = request.form.get("role", row["role"])
    active = 1 if request.form.get("active") else 0
    teacher_id = request.form.get("teacher_id", type=int)
    if role not in load_roles():
        role = row["role"]
    removes_admin = row["role"] == "admin" and row["active"] and (role != "admin" or not active)
    if removes_admin and _admin_count(exclude_id=user_id) == 0:
        flash("There must always be at least one active administrator.", "error")
        return redirect(url_for("auth.staff"))
    if user_id == g.user["id"] and not active:
        flash("You can't switch off your own login.", "error")
        return redirect(url_for("auth.staff"))
    db.execute("UPDATE users SET role=?, active=?, teacher_id=? WHERE id=?",
               (role, active, teacher_id or None, user_id))
    flash("Saved.", "success")
    return redirect(url_for("auth.staff"))


@auth_bp.route("/staff/<int:user_id>/password", methods=["POST"])
def staff_password(user_id):
    pw = request.form.get("password", "")
    err = _check_password(pw)
    if err:
        flash(err, "error")
    elif db.fetch_one("SELECT id FROM users WHERE id=?", (user_id,)):
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), user_id))
        flash("Password reset. Ask them to change it from My Account after logging in.", "success")
    return redirect(url_for("auth.staff"))


# ---------------------------------------------------------------- roles & access
def _role_user_counts():
    return {r["role"]: r["c"] for r in db.fetch_all("SELECT role, COUNT(*) AS c FROM users GROUP BY role")}


def _unique_key(name):
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:32] or "role"
    existing = set(load_roles())
    key, n = base, 2
    while key in existing or key in permissions.RESERVED_KEYS:
        key, n = f"{base}_{n}", n + 1
    return key


def _role_name_problem(name, ignore_key=None):
    if not 2 <= len(name) <= 40:
        return "Give the role a name of 2 to 40 characters."
    for r in load_roles().values():
        if r["key"] != ignore_key and r["name"].strip().lower() == name.lower():
            return f"There is already a role called “{r['name']}”."
    return None


@auth_bp.route("/staff/roles")
def roles_page():
    return render_template(
        "auth/roles.html", roles=list(load_roles().values()), counts=_role_user_counts(),
        services=permissions.SERVICES)


@auth_bp.route("/staff/roles/add", methods=["POST"])
def role_add():
    name = " ".join(request.form.get("name", "").split())
    err = _role_name_problem(name)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.roles_page"))
    services = permissions.clean_services(request.form.getlist("services"))
    db.execute("INSERT INTO roles (role_key, name, services, builtin) VALUES (?, ?, ?, 0)",
               (_unique_key(name), name, ",".join(services)))
    flash(f"Role “{name}” created. You can now choose it for anyone on the Staff & Logins page.", "success")
    return redirect(url_for("auth.roles_page"))


@auth_bp.route("/staff/roles/<role_key>/save", methods=["POST"])
def role_save(role_key):
    role = load_roles().get(role_key)
    if not role:
        flash("That role no longer exists.", "error")
        return redirect(url_for("auth.roles_page"))
    if role_key == "admin":
        flash("The Administrator role always has full access and can't be changed.", "error")
        return redirect(url_for("auth.roles_page"))
    name = " ".join(request.form.get("name", "").split()) or role["name"]
    err = _role_name_problem(name, ignore_key=role_key)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.roles_page"))
    services = permissions.clean_services(request.form.getlist("services"))
    db.execute("UPDATE roles SET name=?, services=? WHERE role_key=?", (name, ",".join(services), role_key))
    flash(f"Saved “{name}”. The change applies the next time each person opens a page.", "success")
    return redirect(url_for("auth.roles_page"))


@auth_bp.route("/staff/roles/<role_key>/delete", methods=["POST"])
def role_delete(role_key):
    role = load_roles().get(role_key)
    if not role:
        return redirect(url_for("auth.roles_page"))
    if role["builtin"]:
        flash("Administrator, Teacher and Bursar are built in and can't be deleted.", "error")
    elif _role_user_counts().get(role_key):
        flash(f"“{role['name']}” is still assigned to someone. Move them to another role first.", "error")
    else:
        db.execute("DELETE FROM roles WHERE role_key=?", (role_key,))
        flash(f"Role “{role['name']}” deleted.", "success")
    return redirect(url_for("auth.roles_page"))
