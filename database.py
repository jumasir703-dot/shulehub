"""
database.py
Data layer for the School Timetable Manager (Render free-plan version).

Backend selection is automatic, based on environment:

- If DATABASE_URL is set (Render's free PostgreSQL sets this for you
  automatically when the database is linked in render.yaml), we use
  Postgres. This is what makes data survive on Render's FREE plan,
  since free web services have no persistent disk but a free Postgres
  database is available.
- Otherwise (e.g. running locally with no database configured) we fall
  back to a local SQLite file, so local development still needs zero
  setup.

Every other module (app.py) is unaware of this split: it always calls
fetch_all() / fetch_one() / execute() with '?' placeholders, exactly as
before. This module translates that into the right SQL dialect.

Curriculum model implemented:
- Grade 10  -> Kenyan CBC / CBE Senior School (pathway-based: STEM,
               Social Sciences, Arts & Sports Science)
- Form 3    -> legacy 8-4-4 curriculum
- Form 4    -> legacy 8-4-4 curriculum
- Form 1 and Form 2 (8-4-4) do NOT exist; Form 2 has been replaced by
  Grade 10 under CBC.

Note on Render's free Postgres plan: the free database expires 30 days
after creation (14-day grace period to upgrade before deletion), has no
backups, and is capped at 1GB. See README.md for what that means for you.
"""

import os
import re

import curriculum
import sm_schema

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "timetable.db"),
)

# All weekdays the school could possibly teach on. Which of these are
# actually "live" (i.e. generate columns on the timetable) is a per-school
# choice stored in `school_settings.active_days` — see get_active_days().
ALL_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_ACTIVE_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# Kept for any code (or old imports) that still expects a static list —
# new code should call get_active_days() instead, since this no longer
# reflects a school's actual configured days.
DAYS = DEFAULT_ACTIVE_DAYS


# --------------------------------------------------------------------------
# Connection / query translation helpers
# --------------------------------------------------------------------------
def get_connection():
    if USE_POSTGRES:
        url = DATABASE_URL
        if url.startswith("postgres://"):
            # Render (and Heroku-style) URLs use the old "postgres://" scheme;
            # psycopg2 wants "postgresql://".
            url = "postgresql://" + url[len("postgres://"):]
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn


def _adapt(query):
    """Translate SQLite-style '?' placeholders to psycopg2 '%s' on Postgres."""
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


# Tables that use a composite primary key instead of an auto-incrementing
# 'id' column. INSERTs into these must never get 'RETURNING id' appended
# under Postgres, since that column doesn't exist there.
TABLES_WITHOUT_ID = {"teacher_subjects", "generation_settings", "exam_subjects"}

_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)


def _insert_table_name(query):
    """Extract the target table name from an INSERT query, or None."""
    m = _INSERT_TABLE_RE.match(query)
    return m.group(1).lower() if m else None


def _wants_returning_id(query):
    """Whether it's safe/appropriate to append RETURNING id to this INSERT."""
    table = _insert_table_name(query)
    return table is not None and table not in TABLES_WITHOUT_ID


def _insert_and_get_id(cur, query, params=()):
    """Run an INSERT and return the new row's id, for either backend."""
    query = _adapt(query)
    if USE_POSTGRES:
        if "RETURNING" not in query.upper() and _wants_returning_id(query):
            query = query.rstrip().rstrip(";") + " RETURNING id"
        cur.execute(query, params)
        if "RETURNING" in query.upper():
            return cur.fetchone()["id"]
        return None
    else:
        cur.execute(query, params)
        return cur.lastrowid


# --------------------------------------------------------------------------
# Schema (one flavor per backend — Postgres needs SERIAL instead of
# SQLite's INTEGER PRIMARY KEY AUTOINCREMENT)
# --------------------------------------------------------------------------
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    curriculum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pathways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    UNIQUE(grade_id, pathway_id, name)
);

CREATE TABLE IF NOT EXISTS subject_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
    periods_per_week INTEGER DEFAULT 5,
    UNIQUE(name, grade_id, pathway_id)
);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT,
    category TEXT NOT NULL,
    grade_id INTEGER REFERENCES grades(id) ON DELETE CASCADE,
    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
    periods_per_week INTEGER DEFAULT 5,
    group_id INTEGER REFERENCES subject_groups(id) ON DELETE SET NULL,
    UNIQUE(name, grade_id, pathway_id)
);

CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    rank INTEGER
);

CREATE TABLE IF NOT EXISTS teacher_subjects (
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (teacher_id, subject_id)
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    capacity INTEGER
);

CREATE TABLE IF NOT EXISTS periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    period_number INTEGER NOT NULL,
    start_time TEXT,
    end_time TEXT,
    is_break INTEGER DEFAULT 0,
    label TEXT,
    UNIQUE(day, period_number)
);

CREATE TABLE IF NOT EXISTS timetable_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    UNIQUE(stream_id, period_id, subject_id)
);

CREATE TABLE IF NOT EXISTS generation_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS subject_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS day_structure (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    start_time TEXT NOT NULL DEFAULT '08:00',
    lesson_minutes INTEGER NOT NULL DEFAULT 40,
    periods_per_day INTEGER NOT NULL DEFAULT 8
);

CREATE TABLE IF NOT EXISTS breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    after_period INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL,
    label TEXT
);
CREATE TABLE IF NOT EXISTS school_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT 'My School',
    tagline TEXT NOT NULL DEFAULT 'Timetable & Exam Analysis',
    brand_mark TEXT NOT NULL DEFAULT 'TT',
    active_days TEXT NOT NULL DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday'
);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS grades (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    curriculum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pathways (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    id SERIAL PRIMARY KEY,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    UNIQUE(grade_id, pathway_id, name)
);

CREATE TABLE IF NOT EXISTS subject_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
    periods_per_week INTEGER DEFAULT 5,
    UNIQUE(name, grade_id, pathway_id)
);

CREATE TABLE IF NOT EXISTS subjects (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT,
    category TEXT NOT NULL,
    grade_id INTEGER REFERENCES grades(id) ON DELETE CASCADE,
    pathway_id INTEGER REFERENCES pathways(id) ON DELETE SET NULL,
    periods_per_week INTEGER DEFAULT 5,
    group_id INTEGER REFERENCES subject_groups(id) ON DELETE SET NULL,
    UNIQUE(name, grade_id, pathway_id)
);

CREATE TABLE IF NOT EXISTS teachers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    rank INTEGER
);

CREATE TABLE IF NOT EXISTS teacher_subjects (
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (teacher_id, subject_id)
);

CREATE TABLE IF NOT EXISTS rooms (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    capacity INTEGER
);

CREATE TABLE IF NOT EXISTS periods (
    id SERIAL PRIMARY KEY,
    day TEXT NOT NULL,
    period_number INTEGER NOT NULL,
    start_time TEXT,
    end_time TEXT,
    is_break INTEGER DEFAULT 0,
    label TEXT,
    UNIQUE(day, period_number)
);

CREATE TABLE IF NOT EXISTS timetable_entries (
    id SERIAL PRIMARY KEY,
    stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
    UNIQUE(stream_id, period_id, subject_id)
);

CREATE TABLE IF NOT EXISTS generation_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS subject_rules (
    id SERIAL PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS day_structure (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    start_time TEXT NOT NULL DEFAULT '08:00',
    lesson_minutes INTEGER NOT NULL DEFAULT 40,
    periods_per_day INTEGER NOT NULL DEFAULT 8
);

CREATE TABLE IF NOT EXISTS breaks (
    id SERIAL PRIMARY KEY,
    after_period INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL,
    label TEXT
);
CREATE TABLE IF NOT EXISTS school_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT 'My School',
    tagline TEXT NOT NULL DEFAULT 'Timetable & Exam Analysis',
    brand_mark TEXT NOT NULL DEFAULT 'TT',
    active_days TEXT NOT NULL DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday'
);
"""

EXAM_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admission_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    gender TEXT,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    term INTEGER NOT NULL DEFAULT 1,
    year INTEGER NOT NULL,
    exam_date TEXT,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    UNIQUE(name, term, year, grade_id)
);

CREATE TABLE IF NOT EXISTS exam_subjects (
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    out_of INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (exam_id, subject_id)
);

CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    UNIQUE(exam_id, student_id, subject_id)
);

CREATE TABLE IF NOT EXISTS grading_bands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scale TEXT NOT NULL,
    label TEXT NOT NULL,
    min_pct REAL NOT NULL,
    points INTEGER NOT NULL,
    remark TEXT,
    UNIQUE(scale, label)
);
"""

EXAM_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS students (
    id SERIAL PRIMARY KEY,
    admission_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    gender TEXT,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exams (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    term INTEGER NOT NULL DEFAULT 1,
    year INTEGER NOT NULL,
    exam_date TEXT,
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    UNIQUE(name, term, year, grade_id)
);

CREATE TABLE IF NOT EXISTS exam_subjects (
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    out_of INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (exam_id, subject_id)
);

CREATE TABLE IF NOT EXISTS marks (
    id SERIAL PRIMARY KEY,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    score DOUBLE PRECISION NOT NULL,
    UNIQUE(exam_id, student_id, subject_id)
);

CREATE TABLE IF NOT EXISTS grading_bands (
    id SERIAL PRIMARY KEY,
    scale TEXT NOT NULL,
    label TEXT NOT NULL,
    min_pct DOUBLE PRECISION NOT NULL,
    points INTEGER NOT NULL,
    remark TEXT,
    UNIQUE(scale, label)
);
"""

SCHEMA_SQLITE = SCHEMA_SQLITE + EXAM_SCHEMA_SQLITE
SCHEMA_POSTGRES = SCHEMA_POSTGRES + EXAM_SCHEMA_POSTGRES


# --------------------------------------------------------------------------
# Init / seed
# --------------------------------------------------------------------------
def init_db():
    """Create the schema and seed default data if the DB doesn't exist yet."""
    if USE_POSTGRES:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.grades') AS reg")
        row = cur.fetchone()
        first_run = row["reg"] is None
        cur.execute(SCHEMA_POSTGRES)
        conn.commit()
        _migrate_schema(conn)
        if first_run:
            seed_data(conn)
        _post_init(conn)
        conn.close()
    else:
        first_run = not os.path.exists(DB_PATH)
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = get_connection()
        conn.executescript(SCHEMA_SQLITE)
        conn.commit()
        _migrate_schema(conn)
        if first_run:
            seed_data(conn)
        _post_init(conn)
        conn.close()


def _migrate_schema(conn):
    """Apply small additive schema changes to databases that already existed
    before this column/table was introduced. Safe to run on every startup:
    each change is only applied if it isn't already present."""
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute("ALTER TABLE subjects ADD COLUMN IF NOT EXISTS periods_per_week INTEGER DEFAULT 5")
        cur.execute("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS rank INTEGER")
        cur.execute("ALTER TABLE school_settings ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE grades ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
        cur.execute(
            "ALTER TABLE subjects ADD COLUMN IF NOT EXISTS group_id INTEGER "
            "REFERENCES subject_groups(id) ON DELETE SET NULL"
        )
    else:
        cur.execute("PRAGMA table_info(subjects)")
        cols = {row["name"] for row in cur.fetchall()}
        if "periods_per_week" not in cols:
            cur.execute("ALTER TABLE subjects ADD COLUMN periods_per_week INTEGER DEFAULT 5")
        if "group_id" not in cols:
            cur.execute(
                "ALTER TABLE subjects ADD COLUMN group_id INTEGER "
                "REFERENCES subject_groups(id) ON DELETE SET NULL"
            )

        cur.execute("PRAGMA table_info(grades)")
        grade_cols = {row["name"] for row in cur.fetchall()}
        if "sort_order" not in grade_cols:
            cur.execute("ALTER TABLE grades ADD COLUMN sort_order INTEGER DEFAULT 0")

        cur.execute("PRAGMA table_info(teachers)")
        teacher_cols = {row["name"] for row in cur.fetchall()}
        if "rank" not in teacher_cols:
            cur.execute("ALTER TABLE teachers ADD COLUMN rank INTEGER")

        cur.execute("PRAGMA table_info(school_settings)")
        settings_cols = {row["name"] for row in cur.fetchall()}
        if "onboarding_completed" not in settings_cols:
            cur.execute("ALTER TABLE school_settings ADD COLUMN onboarding_completed BOOLEAN DEFAULT 0")

    _migrate_relax_timetable_entries_unique(conn, cur)


    # Seed day_structure/breaks defaults for databases that already existed
    # before this feature, matching the values their periods were originally
    # generated with (08:00 start, 40-min lessons, 20-min break after period
    # 4, 8 periods/day) — so the Day Structure page shows what they already
    # effectively have, rather than blanks, until they choose to change it.
    cur.execute("SELECT COUNT(*) AS c FROM day_structure")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            _adapt("INSERT INTO day_structure (id, start_time, lesson_minutes, periods_per_day) VALUES (1, ?, ?, ?)"),
            ("08:00", 40, 8),
        )
    cur.execute("SELECT COUNT(*) AS c FROM breaks")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            _adapt("INSERT INTO breaks (after_period, duration_minutes, label) VALUES (?, ?, ?)"),
            (4, 20, "Break"),
        )

    # Seed the single school_settings row (id=1). Runs for brand-new
    # databases too, since CREATE TABLE's own column defaults only apply
    # to columns not explicitly provided — here we provide them explicitly
    # so the school name shown really is editable from day one.
    cur.execute("SELECT COUNT(*) AS c FROM school_settings")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            _adapt(
                "INSERT INTO school_settings (id, name, tagline, brand_mark, active_days) "
                "VALUES (1, ?, ?, ?, ?)"
            ),
            ("My School", "Timetable, Exams & School Management", "TT", ",".join(DEFAULT_ACTIVE_DAYS)),
        )

    # If this database already has teachers in it, it existed before the
    # onboarding wizard was added — don't force an already-set-up school
    # through first-run setup. Only brand-new/empty databases see it.
    cur.execute("SELECT COUNT(*) AS c FROM teachers")
    has_teachers = cur.fetchone()["c"] > 0
    cur.execute("SELECT onboarding_completed FROM school_settings WHERE id=1")
    row = cur.fetchone()
    if has_teachers and row is not None and not row["onboarding_completed"]:
        cur.execute(_adapt("UPDATE school_settings SET onboarding_completed = ? WHERE id=1"), (True,))

    conn.commit()


def _migrate_relax_timetable_entries_unique(conn, cur):
    """One-time migration: relax the UNIQUE constraint on timetable_entries
    from (stream_id, period_id) to (stream_id, period_id, subject_id).

    This is what allows "optional subject groups" (e.g. Business Studies /
    Computer Studies / Home Science / Agriculture taught in parallel, with
    students split across them) to occupy the same stream+period slot with
    more than one timetable_entries row — one per subject in the group.

    Safe to run on every startup: it only touches the table if the old,
    narrower constraint is still in place; once migrated, it's a no-op.
    """
    if USE_POSTGRES:
        cur.execute(
            """SELECT con.conname,
                      array_agg(a.attname) AS cols
               FROM pg_constraint con
               JOIN pg_class rel ON rel.oid = con.conrelid
               JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey)
               WHERE rel.relname = 'timetable_entries' AND con.contype = 'u'
               GROUP BY con.conname"""
        )
        for row in cur.fetchall():
            cols = set(row["cols"])
            if cols == {"stream_id", "period_id"}:
                cur.execute(f'ALTER TABLE timetable_entries DROP CONSTRAINT "{row["conname"]}"')
                cur.execute(
                    "ALTER TABLE timetable_entries ADD CONSTRAINT "
                    "timetable_entries_stream_period_subject_key "
                    "UNIQUE (stream_id, period_id, subject_id)"
                )
        conn.commit()
    else:
        cur.execute("PRAGMA index_list(timetable_entries)")
        needs_rebuild = False
        for idx in cur.fetchall():
            if not idx["unique"]:
                continue
            cur.execute(f"PRAGMA index_info({idx['name']})")
            idx_cols = {r["name"] for r in cur.fetchall()}
            if idx_cols == {"stream_id", "period_id"}:
                needs_rebuild = True
                break
        if needs_rebuild:
            cur.execute("ALTER TABLE timetable_entries RENAME TO timetable_entries_old")
            cur.execute(
                """CREATE TABLE timetable_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
                    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
                    period_id INTEGER NOT NULL REFERENCES periods(id) ON DELETE CASCADE,
                    UNIQUE(stream_id, period_id, subject_id)
                )"""
            )
            cur.execute(
                "INSERT INTO timetable_entries "
                "(id, stream_id, subject_id, teacher_id, room_id, period_id) "
                "SELECT id, stream_id, subject_id, teacher_id, room_id, period_id "
                "FROM timetable_entries_old"
            )
            cur.execute("DROP TABLE timetable_entries_old")
        conn.commit()


def _post_init(conn):
    """Runs after schema creation, migration and (on a brand-new database)
    seeding. Everything here is idempotent and safe on every startup."""
    # School Management tables (users, guardians, attendance, fees, ...) and
    # the extra columns on `students`. Idempotent.
    sm_schema.apply(conn, USE_POSTGRES)
    cur = conn.cursor()

    # a) Give every grade a sort_order so the new primary / junior-secondary
    #    grades slot in at the right place (Grade 1 ... Grade 10, Form 3, Form 4)
    #    instead of being listed after Form 4 in creation order.
    cur.execute("SELECT id, name FROM grades WHERE sort_order IS NULL OR sort_order = 0")
    for row in cur.fetchall():
        order = curriculum.LEGACY_SORT_ORDER.get(row["name"], 100 + row["id"])
        cur.execute(_adapt("UPDATE grades SET sort_order=? WHERE id=?"), (order, row["id"]))

    # b) Add the lower-primary / junior-secondary grades + their CBC subjects
    #    exactly ONCE per database. A flag in generation_settings records that
    #    it's been done, so a school that later deletes (say) Grade 8 doesn't
    #    get it back on the next restart.
    cur.execute("SELECT value FROM generation_settings WHERE key='levels_seeded_v1'")
    if cur.fetchone() is None:
        for name, curr, order in curriculum.DEFAULT_GRADES:
            cur.execute(_adapt("SELECT id FROM grades WHERE name=?"), (name,))
            if cur.fetchone() is None:
                gid = _insert_and_get_id(
                    cur, "INSERT INTO grades (name, curriculum, sort_order) VALUES (?, ?, ?)",
                    (name, curr, order),
                )
                _insert_level_subjects(cur, gid, curr)
        cur.execute(_adapt("INSERT INTO generation_settings (key, value) VALUES (?, ?)"),
                    ("levels_seeded_v1", "1"))

    # c) Default grading bands for any scale that has none yet.
    for scale, info in curriculum.SCALES.items():
        cur.execute(_adapt("SELECT COUNT(*) AS c FROM grading_bands WHERE scale=?"), (scale,))
        if cur.fetchone()["c"] == 0:
            for label, min_pct, points, remark in info["bands"]:
                cur.execute(
                    _adapt("INSERT INTO grading_bands (scale, label, min_pct, points, remark) "
                           "VALUES (?, ?, ?, ?, ?)"),
                    (scale, label, min_pct, points, remark),
                )
    conn.commit()


def _insert_level_subjects(cur, grade_id, curr):
    """Insert the standard CBC subject list for a curriculum level into a grade."""
    for sname, periods in curriculum.LEVEL_SUBJECTS.get(curr, []):
        cur.execute(
            _adapt("INSERT INTO subjects (name, category, grade_id, pathway_id, periods_per_week) "
                   "VALUES (?, 'core', ?, NULL, ?)"),
            (sname, grade_id, periods),
        )


def create_grade(name, curr, with_subjects=True):
    """Create a grade (used by the Add Grade form). Returns the new id.
    Raises on duplicate name. Optionally preloads the level's CBC subjects."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(MAX(sort_order), 0) AS m FROM grades")
        next_order = (cur.fetchone()["m"] or 0) + 1
        gid = _insert_and_get_id(
            cur, "INSERT INTO grades (name, curriculum, sort_order) VALUES (?, ?, ?)",
            (name, curr, next_order),
        )
        if with_subjects:
            _insert_level_subjects(cur, gid, curr)
        conn.commit()
        return gid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_many(statements):
    """Run several (query, params) statements in ONE connection/transaction.
    Used for bulk writes such as saving a whole page of exam marks, where
    opening a connection per cell (as execute() does) would be very slow.
    Returns the total number of rows affected."""
    conn = get_connection()
    cur = conn.cursor()
    affected = 0
    try:
        for query, params in statements:
            cur.execute(_adapt(query), params)
            if cur.rowcount and cur.rowcount > 0:
                affected += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return affected


def _regenerate_periods(cur):
    """
    Recompute start_time/end_time (and insert/remove rows as needed) for
    every day's periods, based on the day_structure + breaks settings.

    Existing period rows are UPDATEd in place whenever their period_number
    still exists in the new structure, so their id (and therefore any
    timetable_entries pointing at them) stays valid — only the times/label
    change. Rows are only inserted (new period_number) or deleted (old
    period_number beyond the new periods_per_day) when the day actually
    grew or shrank; deleting cascades to remove any timetable_entries that
    were sitting in a period that no longer exists.
    """
    cur.execute("SELECT start_time, lesson_minutes, periods_per_day FROM day_structure WHERE id=1")
    ds = cur.fetchone()
    if not ds:
        return
    start_time, lesson_minutes, periods_per_day = ds["start_time"], ds["lesson_minutes"], ds["periods_per_day"]
    start_hour, start_min = (int(x) for x in start_time.split(":"))

    cur.execute("SELECT after_period, duration_minutes FROM breaks ORDER BY after_period")
    break_map = {}
    for row in cur.fetchall():
        break_map[row["after_period"]] = break_map.get(row["after_period"], 0) + row["duration_minutes"]

    active_days = _get_active_days(cur)

    # Drop every period belonging to a day that is no longer switched on in
    # School Settings (e.g. the school stopped teaching on Saturdays). This
    # cascades to remove any timetable_entries sitting in those periods.
    cur.execute("SELECT DISTINCT day FROM periods")
    for row in cur.fetchall():
        if row["day"] not in active_days:
            cur.execute(_adapt("DELETE FROM periods WHERE day=?"), (row["day"],))

    for day in active_days:
        cur.execute(_adapt("SELECT period_number FROM periods WHERE day=?"), (day,))
        existing_numbers = {row["period_number"] for row in cur.fetchall()}

        for num in existing_numbers:
            if num > periods_per_day:
                cur.execute(_adapt("DELETE FROM periods WHERE day=? AND period_number=?"), (day, num))

        minutes = start_hour * 60 + start_min
        for p in range(1, periods_per_day + 1):
            s_h, s_m = divmod(minutes, 60)
            e_min = minutes + lesson_minutes
            e_h, e_m = divmod(e_min, 60)
            start_str, end_str = f"{s_h:02d}:{s_m:02d}", f"{e_h:02d}:{e_m:02d}"
            if p in existing_numbers:
                cur.execute(
                    _adapt("UPDATE periods SET start_time=?, end_time=?, label=? WHERE day=? AND period_number=?"),
                    (start_str, end_str, f"Lesson {p}", day, p),
                )
            else:
                cur.execute(
                    _adapt(
                        "INSERT INTO periods (day, period_number, start_time, end_time, is_break, label) "
                        "VALUES (?, ?, ?, ?, 0, ?)"
                    ),
                    (day, p, start_str, end_str, f"Lesson {p}"),
                )
            minutes = e_min
            if p in break_map:
                minutes += break_map[p]


def regenerate_periods():
    """Public entry point: open a connection, apply the current
    day_structure/breaks settings to every period, commit, close."""
    conn = get_connection()
    cur = conn.cursor()
    _regenerate_periods(cur)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# School settings (name/tagline/brand + which weekdays generate columns)
# --------------------------------------------------------------------------
def _get_active_days(cur):
    """Same as get_active_days() but reuses an already-open cursor, for use
    inside functions (like _regenerate_periods) that are mid-transaction."""
    cur.execute("SELECT active_days FROM school_settings WHERE id=1")
    row = cur.fetchone()
    if not row or not row["active_days"]:
        return list(DEFAULT_ACTIVE_DAYS)
    days = [d.strip() for d in row["active_days"].split(",") if d.strip()]
    # Keep them in canonical Monday->Sunday order regardless of storage order.
    return [d for d in ALL_WEEKDAYS if d in days] or list(DEFAULT_ACTIVE_DAYS)


def get_active_days():
    """The ordered list of weekdays this school currently teaches on — these
    become the columns of every generated timetable grid. Falls back to
    Monday-Friday if nothing has been configured yet."""
    conn = get_connection()
    cur = conn.cursor()
    days = _get_active_days(cur)
    conn.close()
    return days


def get_school_settings():
    """Branding + config for the currently running school. Every template
    and export title pulls the school's name from here, so multiple
    deployments (or, later, multiple tenants) can each show their own
    name/tagline instead of a hardcoded one. `has_logo` / `logo_version` say
    whether a logo has been uploaded (the image itself is served by /logo)."""
    row = fetch_one("SELECT name, tagline, brand_mark, active_days FROM school_settings WHERE id=1")
    if not row:
        settings = {
            "name": "My School",
            "tagline": "Timetable & Exam Analysis",
            "brand_mark": "TT",
            "active_days": list(DEFAULT_ACTIVE_DAYS),
        }
    else:
        settings = dict(row)
        settings["active_days"] = get_active_days()
    try:
        logo = fetch_one("SELECT updated_at FROM school_logo WHERE id=1")
    except Exception:
        logo = None
    settings["has_logo"] = bool(logo)
    settings["logo_version"] = logo["updated_at"] if logo else ""
    return settings


def get_onboarding_completed():
    row = fetch_one("SELECT onboarding_completed FROM school_settings WHERE id=1")
    if not row:
        return False
    return bool(row["onboarding_completed"])


def set_onboarding_completed(value):
    execute(
        "UPDATE school_settings SET onboarding_completed=? WHERE id=1",
        (bool(value),),
    )


def set_school_settings(name, tagline, brand_mark, active_days_list):
    """Save the school's name/tagline/brand mark and which weekdays it
    teaches on. Does NOT itself regenerate periods — call regenerate_periods()
    afterwards if active_days_list changed, so the grid's columns update."""
    name = (name or "My School").strip() or "My School"
    tagline = (tagline or "").strip()
    brand_mark = (brand_mark or "TT").strip()[:4] or "TT"
    days = [d for d in ALL_WEEKDAYS if d in (active_days_list or [])] or list(DEFAULT_ACTIVE_DAYS)
    active_days_str = ",".join(days)

    if USE_POSTGRES:
        execute(
            "INSERT INTO school_settings (id, name, tagline, brand_mark, active_days) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "name = EXCLUDED.name, tagline = EXCLUDED.tagline, "
            "brand_mark = EXCLUDED.brand_mark, active_days = EXCLUDED.active_days",
            (name, tagline, brand_mark, active_days_str),
        )
    else:
        execute(
            "INSERT INTO school_settings (id, name, tagline, brand_mark, active_days) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "name = excluded.name, tagline = excluded.tagline, "
            "brand_mark = excluded.brand_mark, active_days = excluded.active_days",
            (name, tagline, brand_mark, active_days_str),
        )


def get_day_structure():
    row = fetch_one("SELECT start_time, lesson_minutes, periods_per_day FROM day_structure WHERE id=1")
    if row:
        return dict(row)
    return {"start_time": "08:00", "lesson_minutes": 40, "periods_per_day": 8}


def set_day_structure(start_time, lesson_minutes, periods_per_day):
    if USE_POSTGRES:
        execute(
            "INSERT INTO day_structure (id, start_time, lesson_minutes, periods_per_day) "
            "VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "start_time = EXCLUDED.start_time, lesson_minutes = EXCLUDED.lesson_minutes, "
            "periods_per_day = EXCLUDED.periods_per_day",
            (start_time, lesson_minutes, periods_per_day),
        )
    else:
        execute(
            "INSERT INTO day_structure (id, start_time, lesson_minutes, periods_per_day) "
            "VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "start_time = excluded.start_time, lesson_minutes = excluded.lesson_minutes, "
            "periods_per_day = excluded.periods_per_day",
            (start_time, lesson_minutes, periods_per_day),
        )


def list_breaks():
    return fetch_all("SELECT * FROM breaks ORDER BY after_period")


def get_break_rows():
    """Display-only rows describing where each break sits in the day and
    what time range it covers, e.g. for showing a 'Break' row on the
    generated timetable between period 4 and period 5.

    Breaks aren't stored in the `periods` table (the scheduler only ever
    deals with real lesson periods), so this recomputes each break's
    start/end time the same way `_regenerate_periods` lays out the day:
    walking period 1..N, adding lesson_minutes each period, and adding the
    break's duration right after its `after_period`. Since every day uses
    the same day_structure, one pass covers all days (Mon-Fri identical).

    Returns a list of dicts sorted by after_period:
        {"after_period": int, "label": str, "start_time": "HH:MM", "end_time": "HH:MM"}
    """
    ds = get_day_structure()
    start_hour, start_min = (int(x) for x in ds["start_time"].split(":"))

    break_map = {}
    for b in list_breaks():
        entry = break_map.setdefault(b["after_period"], {"duration": 0, "label": b["label"]})
        entry["duration"] += b["duration_minutes"]
        if b["label"]:
            entry["label"] = b["label"]

    rows = []
    minutes = start_hour * 60 + start_min
    for p in range(1, ds["periods_per_day"] + 1):
        minutes += ds["lesson_minutes"]  # end of period p
        if p in break_map:
            info = break_map[p]
            b_start_h, b_start_m = divmod(minutes, 60)
            b_end_min = minutes + info["duration"]
            b_end_h, b_end_m = divmod(b_end_min, 60)
            rows.append({
                "after_period": p,
                "label": info["label"] or "Break",
                "start_time": f"{b_start_h:02d}:{b_start_m:02d}",
                "end_time": f"{b_end_h:02d}:{b_end_m:02d}",
            })
            minutes = b_end_min
    return rows


def replace_breaks(break_list):
    """break_list: list of (after_period, duration_minutes, label) tuples.
    Wipes and re-inserts, since breaks are always edited as a whole set
    from the Day Structure page rather than one at a time."""
    execute("DELETE FROM breaks")
    for after_period, duration_minutes, label in break_list:
        execute(
            "INSERT INTO breaks (after_period, duration_minutes, label) VALUES (?, ?, ?)",
            (after_period, duration_minutes, label),
        )


def seed_data(conn):
    cur = conn.cursor()

    grade10_id = _insert_and_get_id(cur, "INSERT INTO grades (name, curriculum) VALUES (?, ?)", ("Grade 10", "CBC"))
    form3_id = _insert_and_get_id(cur, "INSERT INTO grades (name, curriculum) VALUES (?, ?)", ("Form 3", "8-4-4"))
    form4_id = _insert_and_get_id(cur, "INSERT INTO grades (name, curriculum) VALUES (?, ?)", ("Form 4", "8-4-4"))

    pathway_names = ["STEM", "Social Sciences", "Arts & Sports Science"]
    pathway_ids = {}
    for p in pathway_names:
        pathway_ids[p] = _insert_and_get_id(cur, "INSERT INTO pathways (name) VALUES (?)", (p,))

    for p in pathway_names:
        cur.execute(
            _adapt("INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, ?, ?)"),
            (grade10_id, pathway_ids[p], f"Grade 10 {p}"),
        )
    cur.execute(_adapt("INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, NULL, 'Form 3 East')"), (form3_id,))
    cur.execute(_adapt("INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, NULL, 'Form 3 West')"), (form3_id,))
    cur.execute(_adapt("INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, NULL, 'Form 4 East')"), (form4_id,))
    cur.execute(_adapt("INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, NULL, 'Form 4 West')"), (form4_id,))

    core_subjects = [
        "English", "Kiswahili / KSL", "Mathematics",
        "Community Service Learning", "Physical Education", "ICT",
    ]
    for s in core_subjects:
        cur.execute(
            _adapt("INSERT INTO subjects (name, category, grade_id, pathway_id) VALUES (?, 'core', ?, NULL)"),
            (s, grade10_id),
        )

    pathway_subjects = {
        "STEM": [
            "Biology", "Chemistry", "Physics", "Computer Studies",
            "Agriculture", "Home Science", "General Science",
            "Advanced Mathematics", "Building Construction",
            "Electrical Technology", "Power Mechanics", "Woodwork",
        ],
        "Social Sciences": [
            "History & Citizenship", "Geography", "CRE/IRE/HRE",
            "Business Studies", "Literature in English",
            "Fasihi ya Kiswahili", "French", "German", "Arabic",
        ],
        "Arts & Sports Science": [
            "Fine Art", "Theatre & Film", "Music", "Sports Science",
        ],
    }
    for pathway, subs in pathway_subjects.items():
        for s in subs:
            cur.execute(
                _adapt("INSERT INTO subjects (name, category, grade_id, pathway_id) VALUES (?, 'pathway', ?, ?)"),
                (s, grade10_id, pathway_ids[pathway]),
            )

    legacy_subjects = [
        "English", "Kiswahili", "Mathematics", "Biology", "Chemistry",
        "Physics", "History & Government", "Geography", "CRE",
        "Business Studies", "Agriculture", "Computer Studies",
        "French", "Home Science", "Art & Design",
    ]
    for grade_id in (form3_id, form4_id):
        for s in legacy_subjects:
            cur.execute(
                _adapt("INSERT INTO subjects (name, category, grade_id, pathway_id) VALUES (?, 'general', ?, NULL)"),
                (s, grade_id),
            )

    for name, cap in [("Room 1", 45), ("Room 2", 45), ("Lab 1", 30), ("Lab 2", 30)]:
        cur.execute(_adapt("INSERT INTO rooms (name, capacity) VALUES (?, ?)"), (name, cap))

    conn.commit()
    _regenerate_periods(cur)
    conn.commit()


# --------------------------------------------------------------------------
# Public query helpers (used throughout app.py — unchanged call signatures)
# --------------------------------------------------------------------------
def fetch_all(query, params=()):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(_adapt(query), params)
    rows = cur.fetchall()
    conn.close()
    return rows


def fetch_one(query, params=()):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(_adapt(query), params)
    row = cur.fetchone()
    conn.close()
    return row


def execute(query, params=()):
    conn = get_connection()
    try:
        cur = conn.cursor()
        q = _adapt(query)
        if (
            USE_POSTGRES
            and q.strip().upper().startswith("INSERT")
            and "RETURNING" not in q.upper()
            and _wants_returning_id(q)
        ):
            q = q.rstrip().rstrip(";") + " RETURNING id"
            cur.execute(q, params)
            row = cur.fetchone()
            last_id = row["id"] if row else None
        else:
            cur.execute(q, params)
            last_id = cur.lastrowid if not USE_POSTGRES else None
        conn.commit()
        return last_id
    except Exception:
        # Always release the connection on failure (e.g. a duplicate key), or
        # SQLite stays write-locked and Postgres keeps an aborted transaction.
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Generation settings (scheduling rules toggles)
# --------------------------------------------------------------------------
DEFAULT_GENERATION_SETTINGS = {
    "avoid_same_day_repeat": "1",  # "1" = avoid scheduling a subject twice on the same day
    "vary_period_slot": "1",       # "1" = avoid a subject always landing in the same period number every day
}


def get_generation_settings():
    """Return all scheduling-rule settings as a dict of str->str, filled in
    with defaults for any key that hasn't been saved yet."""
    rows = fetch_all("SELECT key, value FROM generation_settings")
    settings = dict(DEFAULT_GENERATION_SETTINGS)
    for r in rows:
        settings[r["key"]] = r["value"]
    return settings


def set_generation_setting(key, value):
    if USE_POSTGRES:
        execute(
            "INSERT INTO generation_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
    else:
        execute(
            "INSERT INTO generation_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------
# Subject scheduling rules (double lessons, "not after period X", etc.)
# --------------------------------------------------------------------------
VALID_RULE_TYPES = {"double_lesson", "not_after_period"}


def list_subject_rules():
    """All subject rules with the subject's name attached, for display."""
    return fetch_all(
        "SELECT sr.id, sr.subject_id, sr.rule_type, sr.value, s.name AS subject_name "
        "FROM subject_rules sr JOIN subjects s ON sr.subject_id = s.id "
        "ORDER BY s.name, sr.rule_type"
    )


def get_subject_rules(subject_id):
    """Return {'not_after_period': int or None, 'double_lesson_count': int} for one subject."""
    rows = fetch_all("SELECT rule_type, value FROM subject_rules WHERE subject_id=?", (subject_id,))
    result = {"not_after_period": None, "double_lesson_count": 0}
    for r in rows:
        if r["rule_type"] == "not_after_period":
            # if more than one such rule exists, the strictest (smallest) wins
            if result["not_after_period"] is None or r["value"] < result["not_after_period"]:
                result["not_after_period"] = r["value"]
        elif r["rule_type"] == "double_lesson":
            result["double_lesson_count"] += r["value"]
    return result


def add_subject_rule(subject_id, rule_type, value):
    if rule_type not in VALID_RULE_TYPES:
        raise ValueError(f"Unknown rule_type: {rule_type}")
    execute(
        "INSERT INTO subject_rules (subject_id, rule_type, value) VALUES (?, ?, ?)",
        (subject_id, rule_type, value),
    )


def delete_subject_rule(rule_id):
    execute("DELETE FROM subject_rules WHERE id=?", (rule_id,))


# --------------------------------------------------------------------------
# Optional subject groups — e.g. Business Studies / Computer Studies /
# Home Science / Agriculture taught at the same time, with each student
# picking exactly one. The group has one common name (shown on the
# timetable in place of a single subject) and one periods_per_week value,
# since all its member subjects occupy the same slot(s) each week.
# --------------------------------------------------------------------------
def list_subject_groups(grade_id=None):
    """All optional-subject groups, with grade/pathway names and a count of
    member subjects, for display on the Subjects page."""
    query = (
        "SELECT sg.id, sg.name, sg.periods_per_week, sg.grade_id, sg.pathway_id, "
        "g.name AS grade_name, p.name AS pathway_name, "
        "(SELECT COUNT(*) FROM subjects s WHERE s.group_id = sg.id) AS member_count "
        "FROM subject_groups sg "
        "JOIN grades g ON sg.grade_id = g.id "
        "LEFT JOIN pathways p ON sg.pathway_id = p.id"
    )
    params = ()
    if grade_id:
        query += " WHERE sg.grade_id = ?"
        params = (grade_id,)
    query += " ORDER BY g.sort_order, g.id, sg.name"
    return fetch_all(query, params)


def get_subject_group(group_id):
    return fetch_one("SELECT * FROM subject_groups WHERE id=?", (group_id,))


def get_group_members(group_id):
    """Every subject currently assigned to this optional-subject group."""
    return fetch_all(
        "SELECT * FROM subjects WHERE group_id=? ORDER BY name", (group_id,)
    )


def create_subject_group(name, grade_id, pathway_id, periods_per_week):
    return execute(
        "INSERT INTO subject_groups (name, grade_id, pathway_id, periods_per_week) "
        "VALUES (?, ?, ?, ?)",
        (name, grade_id, pathway_id, periods_per_week),
    )


def update_subject_group(group_id, name, periods_per_week):
    execute(
        "UPDATE subject_groups SET name=?, periods_per_week=? WHERE id=?",
        (name, periods_per_week, group_id),
    )


def delete_subject_group(group_id):
    """Deletes the group. Member subjects aren't deleted — their group_id
    just reverts to NULL (ON DELETE SET NULL) and they go back to being
    scheduled individually."""
    execute("DELETE FROM subject_groups WHERE id=?", (group_id,))


def set_subject_group(subject_id, group_id):
    """Assign (or clear, with group_id=None) a subject's optional group."""
    execute("UPDATE subjects SET group_id=? WHERE id=?", (group_id, subject_id))
