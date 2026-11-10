"""
tenancy.py - several schools in one ShuleHub (a school group).

How it works
------------
* Every school keeps its OWN set of tables (grades, students, exams, fees,
  attendance, timetable ...), so one school can never see another's records.
    - Postgres: one schema per school ("school_2", "school_3" ...) inside the
      same database. Your original school stays exactly where it is and is
      School 1, so nothing is moved or copied.
    - SQLite (desktop build): one extra file per school next to the main one.
* Staff logins and roles are shared by the whole group (they live with
  School 1), so one person can work in several schools with one password.
* A request is routed to a school by the signed-in person's choice
  (session["school_id"]). Existing code is untouched: database.get_connection()
  is wrapped so every query lands in the right school.
* The group registry (`schools`, `user_schools`, and a `group_admin` flag on
  users) lives with School 1.

Nothing here runs until tenancy.install(app) is called from app.py.
"""
import os
import re
import time
import contextvars
from contextlib import contextmanager
from datetime import date

import database as db

PRIMARY = 1                       # the original school: the existing tables
SHARED_TABLES = ("users", "roles")  # live only with the primary school

_current = contextvars.ContextVar("shulehub_school", default=PRIMARY)
_orig_get_connection = db.get_connection
_orig_init_db = db.init_db


# ------------------------------------------------------------------ routing
def current():
    return _current.get()


@contextmanager
def using(school_id):
    """Run a block of code against another school's data."""
    token = _current.set(int(school_id))
    try:
        yield
    finally:
        _current.reset(token)


def sqlite_path(school_id):
    if school_id == PRIMARY:
        return db.DB_PATH
    base, ext = os.path.splitext(db.DB_PATH)
    return f"{base}_school{int(school_id)}{ext or '.db'}"


def schema_name(school_id):
    return f"school_{int(school_id)}"


def get_connection():
    sid = _current.get()
    if sid == PRIMARY:
        return _orig_get_connection()
    if db.USE_POSTGRES:
        url = db.DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        # search_path as a connection option (not a SET) so a rollback can't undo it
        return db.psycopg2.connect(
            url, cursor_factory=db.psycopg2.extras.RealDictCursor,
            options=f"-c search_path={schema_name(sid)},public")
    import sqlite3
    path = sqlite_path(sid)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    # users and roles are not in this file: SQLite finds them in the main file
    conn.execute("ATTACH DATABASE ? AS ctl", (db.DB_PATH,))
    return conn


# ------------------------------------------------------------------ levels
def _grades(prefix, nums, curr, order_base=0):
    return [(f"{prefix} {n}", curr, order_base + n) for n in nums]


# key -> (label, what it covers, [(grade name, curriculum code, sort order)])
LEVELS = {
    "pre_primary": ("Pre-Primary", "PP1 and PP2",
                    [("PP1", "PP", -2), ("PP2", "PP", -1)]),
    "lower_primary": ("Lower Primary", "Grade 1 to 3", _grades("Grade", (1, 2, 3), "CBC-LP")),
    "upper_primary": ("Upper Primary", "Grade 4 to 6", _grades("Grade", (4, 5, 6), "CBC-UP")),
    "primary": ("Primary School", "Grade 1 to 6",
                _grades("Grade", (1, 2, 3), "CBC-LP") + _grades("Grade", (4, 5, 6), "CBC-UP")),
    "junior": ("Junior School", "Grade 7 to 9", _grades("Grade", (7, 8, 9), "CBC-JS")),
    "primary_junior": ("Primary and Junior", "Grade 1 to 9",
                       _grades("Grade", (1, 2, 3), "CBC-LP") + _grades("Grade", (4, 5, 6), "CBC-UP")
                       + _grades("Grade", (7, 8, 9), "CBC-JS")),
    "senior": ("Senior School (CBE)", "Grade 10 to 12, with pathways",
               _grades("Grade", (10, 11, 12), "CBC")),
    "secondary_844": ("Secondary (8-4-4)", "Form 1 to 4", _grades("Form", (1, 2, 3, 4), "8-4-4", 20)),
    "custom": ("Other / mixed", "No grades added: set up your own", []),
}
MIXED_LABEL = "Mixed levels"   # the original school, which already has its own grades

_PATHWAYS = {
    "STEM": ["Biology", "Chemistry", "Physics", "Computer Studies", "Agriculture", "Home Science",
             "General Science", "Advanced Mathematics", "Building Construction", "Electrical Technology",
             "Power Mechanics", "Woodwork"],
    "Social Sciences": ["History & Citizenship", "Geography", "CRE/IRE/HRE", "Business Studies",
                        "Literature in English", "Fasihi ya Kiswahili", "French", "German", "Arabic"],
    "Arts & Sports Science": ["Fine Art", "Theatre & Film", "Music", "Sports Science"],
}
_SENIOR_CORE = ["English", "Kiswahili / KSL", "Mathematics", "Community Service Learning",
                "Physical Education", "ICT"]
_LEGACY = ["English", "Kiswahili", "Mathematics", "Biology", "Chemistry", "Physics", "History & Government",
           "Geography", "CRE", "Business Studies", "Agriculture", "Computer Studies", "French",
           "Home Science", "Art & Design"]


def level_label(key):
    if key in LEVELS:
        return LEVELS[key][0]
    return MIXED_LABEL


def apply_level(school_id, level_key):
    """Add the standard grades (and their subjects) for a level to a school.
    Safe to repeat: a grade that already exists is left exactly as it is.
    Returns how many grades were added."""
    spec = LEVELS.get(level_key, LEVELS["custom"])[2]
    if not spec:
        return 0
    added = 0
    with using(school_id):
        conn = db.get_connection()
        cur = conn.cursor()
        try:
            pathways = {}
            if any(c == "CBC" for _, c, _ in spec):
                for pname in _PATHWAYS:
                    cur.execute(db._adapt("SELECT id FROM pathways WHERE name=?"), (pname,))
                    row = cur.fetchone()
                    pathways[pname] = row["id"] if row else db._insert_and_get_id(
                        cur, "INSERT INTO pathways (name) VALUES (?)", (pname,))
            for name, curr, order in spec:
                cur.execute(db._adapt("SELECT id FROM grades WHERE name=?"), (name,))
                if cur.fetchone():
                    continue
                gid = db._insert_and_get_id(
                    cur, "INSERT INTO grades (name, curriculum, sort_order) VALUES (?, ?, ?)", (name, curr, order))
                added += 1
                if curr == "CBC":
                    for s in _SENIOR_CORE:
                        cur.execute(db._adapt("INSERT INTO subjects (name, category, grade_id, pathway_id) "
                                              "VALUES (?, 'core', ?, NULL)"), (s, gid))
                    for pname, subs in _PATHWAYS.items():
                        for s in subs:
                            cur.execute(db._adapt("INSERT INTO subjects (name, category, grade_id, pathway_id) "
                                                  "VALUES (?, 'pathway', ?, ?)"), (s, gid, pathways[pname]))
                elif curr == "8-4-4":
                    for s in _LEGACY:
                        cur.execute(db._adapt("INSERT INTO subjects (name, category, grade_id, pathway_id) "
                                              "VALUES (?, 'general', ?, NULL)"), (s, gid))
                else:
                    db._insert_level_subjects(cur, gid, curr)
            conn.commit()
        finally:
            conn.close()
    return added


# ------------------------------------------------------------------ the registry (lives with School 1)
_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS schools (
    id {PK},
    name TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'custom',
    active INTEGER NOT NULL DEFAULT 1,
    created_on TEXT
);
CREATE TABLE IF NOT EXISTS user_schools (
    user_id INTEGER NOT NULL,
    school_id INTEGER NOT NULL,
    teacher_id INTEGER,
    PRIMARY KEY (user_id, school_id)
);
"""
db.TABLES_WITHOUT_ID.update({"user_schools"})


def ensure_control():
    """Create the group registry next to School 1's tables. Idempotent."""
    with using(PRIMARY):
        conn = _orig_get_connection()
        try:
            cur = conn.cursor()
            ddl = _REGISTRY_DDL.replace("{PK}", "SERIAL PRIMARY KEY" if db.USE_POSTGRES
                                        else "INTEGER PRIMARY KEY AUTOINCREMENT")
            if db.USE_POSTGRES:
                cur.execute(ddl)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS group_admin INTEGER NOT NULL DEFAULT 0")
            else:
                conn.executescript(ddl)
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(users)")
                if "group_admin" not in {r["name"] for r in cur.fetchall()}:
                    cur.execute("ALTER TABLE users ADD COLUMN group_admin INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            cur.execute("SELECT COUNT(*) AS c FROM schools")
            if cur.fetchone()["c"] == 0:
                cur.execute(db._adapt("SELECT name FROM school_settings WHERE id=1"))
                row = cur.fetchone()
                cur.execute(db._adapt("INSERT INTO schools (id, name, level, active, created_on) VALUES (1, ?, 'mixed', 1, ?)"),
                            ((row["name"] if row else "My School"), date.today().isoformat()))
                if db.USE_POSTGRES:   # keep the id counter ahead of the row we just placed
                    cur.execute("SELECT setval(pg_get_serial_sequence('schools', 'id'), "
                                "(SELECT MAX(id) FROM schools))")
            # the people who run the school today are the group's administrators
            cur.execute(db._adapt("SELECT value FROM generation_settings WHERE key='group_admins_seeded_v1'"))
            if cur.fetchone() is None:
                cur.execute("UPDATE users SET group_admin = 1 WHERE role = 'admin'")
                cur.execute(db._adapt("INSERT INTO generation_settings (key, value) VALUES (?, ?)"),
                            ("group_admins_seeded_v1", "1"))
            conn.commit()
        finally:
            conn.close()


def _drop_shared(conn):
    """A sister school must not have its own users/roles tables (they would
    hide the shared ones). Always schema/file qualified so the shared tables
    are never touched."""
    cur = conn.cursor()
    sid = _current.get()
    for t in SHARED_TABLES:
        if db.USE_POSTGRES:
            cur.execute(f'DROP TABLE IF EXISTS "{schema_name(sid)}"."{t}" CASCADE')
        else:
            cur.execute(f"DROP TABLE IF EXISTS main.{t}")
    conn.commit()


def init_school(school_id):
    """Create or upgrade one sister school's tables. Idempotent: runs on every
    start so each school gets the same upgrades School 1 does."""
    if school_id == PRIMARY:
        return
    with using(school_id):
        if db.USE_POSTGRES:
            c = _orig_get_connection()
            c.autocommit = True
            c.cursor().execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name(school_id)}"')
            c.close()
        conn = get_connection()
        try:
            cur = conn.cursor()
            if db.USE_POSTGRES:
                cur.execute(db.SCHEMA_POSTGRES)
            else:
                conn.executescript(db.SCHEMA_SQLITE)
                cur = conn.cursor()
            conn.commit()
            db._migrate_schema(conn)
            # a sister school starts empty: don't add School 1's sample grades
            cur.execute(db._adapt("SELECT 1 FROM generation_settings WHERE key='levels_seeded_v1'"))
            if cur.fetchone() is None:
                cur.execute(db._adapt("INSERT INTO generation_settings (key, value) VALUES (?, ?)"),
                            ("levels_seeded_v1", "1"))
                conn.commit()
            db._post_init(conn)
            _drop_shared(conn)
        finally:
            conn.close()


def init_db():
    """Replacement for database.init_db: School 1 as before, then the registry,
    then every sister school."""
    _orig_init_db()
    ensure_control()
    for s in list_schools(include_inactive=True):
        if s["id"] != PRIMARY:
            init_school(s["id"])


# ------------------------------------------------------------------ reading the registry
def list_schools(include_inactive=False):
    with using(PRIMARY):
        rows = db.fetch_all("SELECT id, name, level, active, created_on FROM schools ORDER BY id")
    out = [dict(r) for r in rows]
    return out if include_inactive else [s for s in out if s["active"]]


_names = {}


def school_name(school_id):
    """The name the school shows in its own header (cached for a minute)."""
    hit = _names.get(school_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        with using(school_id):
            row = db.fetch_one("SELECT name FROM school_settings WHERE id=1")
        name = row["name"] if row else None
    except Exception:
        name = None
    if not name:
        with using(PRIMARY):
            row = db.fetch_one("SELECT name FROM schools WHERE id=?", (school_id,))
        name = row["name"] if row else f"School {school_id}"
    _names[school_id] = (time.time(), name)
    return name


def forget_names():
    _names.clear()


def user_access(user_id):
    """-> (is_group_admin, {school_id: teacher_id or None}) for the schools a
    person was given. People with no assignment keep working in School 1, so
    everyone who existed before this feature is unaffected."""
    with using(PRIMARY):
        row = db.fetch_one("SELECT group_admin, role FROM users WHERE id=?", (user_id,))
        rows = db.fetch_all("SELECT school_id, teacher_id FROM user_schools WHERE user_id=?", (user_id,))
        flagged = bool(row and row["group_admin"])
        if row and not flagged and row["role"] == "admin":
            # a brand-new install (or the last group administrator was switched off):
            # administrators run the group until someone is chosen on the People page
            flagged = db.fetch_one("SELECT COUNT(*) AS c FROM users WHERE group_admin = 1 AND active = 1")["c"] == 0
    return flagged, {r["school_id"]: r["teacher_id"] for r in rows}


def allowed_schools(user_id):
    """The active schools this person can open, School 1 first."""
    group_admin, assigned = user_access(user_id)
    return _visible(list_schools(), group_admin, assigned)


def _visible(schools, group_admin, assigned):
    if group_admin:
        return schools
    if not assigned:
        return [s for s in schools if s["id"] == PRIMARY]
    return [s for s in schools if s["id"] in assigned]


def is_group_admin(user):
    return bool(user) and user_access(user["id"])[0]


def users_for_school(school_id):
    """ids of active people who may work in a school (used to limit alerts and
    pickers to the right staff)."""
    with using(PRIMARY):
        rows = db.fetch_all("SELECT id, group_admin FROM users WHERE active = 1")
        assigned = {r["user_id"] for r in db.fetch_all(
            "SELECT user_id FROM user_schools WHERE school_id=?", (school_id,))}
        anyone = {r["user_id"] for r in db.fetch_all("SELECT DISTINCT user_id FROM user_schools")}
    out = set()
    for r in rows:
        if r["group_admin"] or r["id"] in assigned or (school_id == PRIMARY and r["id"] not in anyone):
            out.add(r["id"])
    return out


# ------------------------------------------------------------------ creating schools
def _brand_mark(name):
    letters = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name) if w)[:3].upper()
    return letters or "SH"


def create_school(name, level, tagline=""):
    """Add a sister school and return its id."""
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("Give the school a name.")
    level = level if level in LEVELS else "custom"
    with using(PRIMARY):
        sid = db.execute("INSERT INTO schools (name, level, active, created_on) VALUES (?, ?, 1, ?)",
                         (name, level, date.today().isoformat()))
    init_school(sid)
    with using(sid):
        db.set_school_settings(name, tagline or LEVELS[level][1], _brand_mark(name), None)
        db.regenerate_periods()
        db.set_onboarding_completed(True)
    apply_level(sid, level)
    forget_names()
    return sid


def snapshot(school_id):
    """The numbers shown for one school on the group page."""
    out = {"students": None, "teachers": None, "attendance_pct": None, "collected_month": None,
           "outstanding": None, "exams": None}
    with using(school_id):
        def one(sql, params=()):
            try:
                return db.fetch_one(sql, params)
            except Exception:
                return None
        r = one("SELECT COUNT(*) AS c FROM students WHERE status='active'")
        out["students"] = r["c"] if r else None
        r = one("SELECT COUNT(*) AS c FROM teachers")
        out["teachers"] = r["c"] if r else None
        r = one("SELECT COUNT(*) AS c FROM exams")
        out["exams"] = r["c"] if r else None
        r = one("SELECT COUNT(*) AS n, SUM(CASE WHEN status IN ('P','L') THEN 1 ELSE 0 END) AS p "
                "FROM attendance WHERE att_date=?", (date.today().isoformat(),))
        if r and r["n"]:
            out["attendance_pct"] = round(100.0 * (r["p"] or 0) / r["n"], 1)
        r = one("SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE voided=0 AND paid_on >= ?",
                (date.today().replace(day=1).isoformat(),))
        out["collected_month"] = float(r["s"]) if r else None
        try:
            import fees
            out["outstanding"] = round(sum(x["balance"] for x in fees._balance_rows(view="all") if x["balance"] > 0), 2)
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ per-request wiring
def _resolve_school():
    """Runs after the login check on every request: pick this person's school."""
    from flask import g, request, session
    if (request.endpoint or "") == "static" or request.path.startswith("/static/"):
        return None
    user = getattr(g, "user", None)
    if not user:
        _current.set(PRIMARY)
        return None
    group_admin, assigned = user_access(user["id"])
    schools = _visible(list_schools(), group_admin, assigned)
    ids = [s["id"] for s in schools] or [PRIMARY]
    sid = session.get("school_id")
    if sid not in ids:
        sid = ids[0]
        session["school_id"] = sid
    _current.set(sid)
    g.school_id = sid
    g.schools = schools
    g.group_admin = group_admin
    # a teacher login is linked to a teacher profile PER SCHOOL
    if sid != PRIMARY or assigned.get(PRIMARY) is not None:
        user["teacher_id"] = assigned.get(sid)
    return None


def _reset(exc=None):
    _current.set(PRIMARY)


def _context():
    from flask import g
    return {
        "school_choices": getattr(g, "schools", []) if getattr(g, "user", None) else [],
        "current_school_id": getattr(g, "school_id", PRIMARY),
        "group_admin": bool(getattr(g, "group_admin", False)),
        "school_level_label": level_label(_level_of(getattr(g, "school_id", PRIMARY))),
        "school_display_name": school_name,
    }


_levels = {}


def _level_of(school_id):
    hit = _levels.get(school_id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        with using(PRIMARY):
            row = db.fetch_one("SELECT level FROM schools WHERE id=?", (school_id,))
        level = row["level"] if row else "mixed"
    except Exception:
        level = "mixed"
    _levels[school_id] = (time.time(), level)
    return level


def install(app):
    """Wire everything into the Flask app. Call once, right after the login
    gate is registered and before db.init_db()."""
    import auth
    db.get_connection = get_connection
    db.init_db = init_db
    auth.COMMON.update({"schools.switch"})
    app.before_request(_resolve_school)
    app.teardown_request(_reset)

    @app.after_request
    def _after(resp):
        from flask import request
        if request.method != "GET":
            forget_names()
            _levels.clear()
        return resp
    app.context_processor(_context)
