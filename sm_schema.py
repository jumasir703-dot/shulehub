"""
sm_schema.py — database tables for the School Management module
(staff logins, guardians, promotion history, attendance, fees & payments).

Called once from database._post_init() on every start. Everything is
idempotent: tables use CREATE TABLE IF NOT EXISTS and new columns on the
existing `students` table are only added when missing, so existing schools
upgrade in place without losing data.
"""

import permissions

_TABLES = """
CREATE TABLE IF NOT EXISTS roles (
    id {PK},
    role_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    services TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS school_logo (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mime TEXT NOT NULL DEFAULT 'image/png',
    data {BLOB} NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id {PK},
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'teacher',
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS guardians (
    id {PK},
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    relationship TEXT,
    phone TEXT,
    email TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS promotion_log (
    id {PK},
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    from_grade_id INTEGER,
    from_stream_id INTEGER,
    from_label TEXT,
    to_label TEXT,
    logged_on TEXT,
    logged_by TEXT
);

CREATE TABLE IF NOT EXISTS attendance (
    id {PK},
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    att_date TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    marked_by TEXT,
    UNIQUE(student_id, att_date)
);

CREATE TABLE IF NOT EXISTS fee_items (
    id {PK},
    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
    term INTEGER NOT NULL,
    year INTEGER NOT NULL,
    name TEXT NOT NULL,
    amount {REAL} NOT NULL,
    UNIQUE(grade_id, term, year, name)
);

CREATE TABLE IF NOT EXISTS invoices (
    id {PK},
    student_id INTEGER NOT NULL REFERENCES students(id),
    term INTEGER NOT NULL,
    year INTEGER NOT NULL,
    created_on TEXT,
    UNIQUE(student_id, term, year)
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id {PK},
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    amount {REAL} NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fee'
);

CREATE TABLE IF NOT EXISTS payments (
    id {PK},
    student_id INTEGER NOT NULL REFERENCES students(id),
    amount {REAL} NOT NULL,
    method TEXT NOT NULL DEFAULT 'Cash',
    reference TEXT,
    paid_on TEXT NOT NULL,
    receipt_no TEXT UNIQUE,
    received_by TEXT,
    note TEXT,
    voided INTEGER NOT NULL DEFAULT 0,
    voided_reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_attendance_date ON attendance(att_date);
CREATE INDEX IF NOT EXISTS ix_payments_student ON payments(student_id);
CREATE INDEX IF NOT EXISTS ix_guardians_student ON guardians(student_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_reference
    ON payments(reference) WHERE reference IS NOT NULL AND voided = 0;
"""

# New columns on the existing `students` table: (name, definition)
_STUDENT_COLUMNS = [
    ("dob", "TEXT"),
    ("admission_date", "TEXT"),
    ("upi", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("notes", "TEXT"),
]


def apply(conn, use_postgres):
    cur = conn.cursor()
    ddl = _TABLES.replace(
        "{PK}", "SERIAL PRIMARY KEY" if use_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ).replace("{REAL}", "DOUBLE PRECISION" if use_postgres else "REAL"
    ).replace("{BLOB}", "BYTEA" if use_postgres else "BLOB")

    if use_postgres:
        cur.execute(ddl)
        for name, definition in _STUDENT_COLUMNS:
            cur.execute(f"ALTER TABLE students ADD COLUMN IF NOT EXISTS {name} {definition}")
    else:
        conn.executescript(ddl)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(students)")
        have = {row["name"] for row in cur.fetchall()}
        for name, definition in _STUDENT_COLUMNS:
            if name not in have:
                cur.execute(f"ALTER TABLE students ADD COLUMN {name} {definition}")

    # The three roles that always exist. Inserted only when missing, so edits
    # the administrator makes to Teacher/Bursar are never overwritten at startup.
    ph = "%s" if use_postgres else "?"
    for key, (name, services) in permissions.DEFAULT_ROLES.items():
        cur.execute(
            f"INSERT INTO roles (role_key, name, services, builtin) "
            f"SELECT {ph}, {ph}, {ph}, 1 WHERE NOT EXISTS (SELECT 1 FROM roles WHERE role_key = {ph})",
            (key, name, ",".join(services), key))

    # Students that were switched off before this module existed were,
    # in practice, withdrawn. Keep `status` consistent with `active`.
    cur.execute("UPDATE students SET status='withdrawn' WHERE active = 0 AND status = 'active'")
    conn.commit()
