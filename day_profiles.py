"""
day_profiles.py
Per-level day structure for the timetable.

Before this module the whole school shared ONE day structure (start time,
lesson length, periods per day, breaks). Now there are several "day
profiles" (e.g. Pre-Primary, Lower Primary, Secondary) and every grade is
assigned to one. A grade with no profile of its own uses the Default profile,
so a school that never adds a second profile behaves exactly as before.

The `periods` table gains a profile_id: each profile has its own grid of
periods (per active day), so timetable_entries keep pointing at real period
rows and nothing else in the data model changes.

Because two profiles can place "Period 1" at different clock times, clash
checks (same teacher / same room) must compare real times, not period ids -
see conflict_clause().
"""

import database as db

DEFAULT_NAME = "Default"


# --------------------------------------------------------------------------
# Schema + one-time migration (idempotent, called from database._migrate_schema)
# --------------------------------------------------------------------------
def apply(conn, use_pg):
    cur = conn.cursor()
    pk = "SERIAL PRIMARY KEY" if use_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS day_profiles (
            id {pk},
            name TEXT UNIQUE NOT NULL,
            start_time TEXT NOT NULL DEFAULT '08:00',
            lesson_minutes INTEGER NOT NULL DEFAULT 40,
            periods_per_day INTEGER NOT NULL DEFAULT 8,
            is_default INTEGER NOT NULL DEFAULT 0
        )"""
    )
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS profile_breaks (
            id {pk},
            profile_id INTEGER NOT NULL REFERENCES day_profiles(id) ON DELETE CASCADE,
            after_period INTEGER NOT NULL,
            duration_minutes INTEGER NOT NULL,
            label TEXT
        )"""
    )

    # Seed the Default profile from the old single day structure, once.
    cur.execute("SELECT COUNT(*) AS c FROM day_profiles")
    if cur.fetchone()["c"] == 0:
        cur.execute("SELECT start_time, lesson_minutes, periods_per_day FROM day_structure WHERE id=1")
        ds = cur.fetchone()
        st, lm, pp = (ds["start_time"], ds["lesson_minutes"], ds["periods_per_day"]) if ds else ("08:00", 40, 8)
        cur.execute(
            db._adapt("INSERT INTO day_profiles (name, start_time, lesson_minutes, periods_per_day, is_default) "
                      "VALUES (?, ?, ?, ?, 1)"),
            (DEFAULT_NAME, st, lm, pp),
        )
        cur.execute("SELECT id FROM day_profiles WHERE is_default=1")
        pid = cur.fetchone()["id"]
        cur.execute("SELECT after_period, duration_minutes, label FROM breaks ORDER BY after_period")
        for b in cur.fetchall():
            cur.execute(
                db._adapt("INSERT INTO profile_breaks (profile_id, after_period, duration_minutes, label) "
                          "VALUES (?, ?, ?, ?)"),
                (pid, b["after_period"], b["duration_minutes"], b["label"]),
            )
    cur.execute("SELECT id FROM day_profiles WHERE is_default=1")
    default_id = cur.fetchone()["id"]

    # grades.day_profile_id (NULL = use the Default profile)
    if use_pg:
        cur.execute("ALTER TABLE grades ADD COLUMN IF NOT EXISTS day_profile_id INTEGER "
                    "REFERENCES day_profiles(id) ON DELETE SET NULL")
    else:
        cur.execute("PRAGMA table_info(grades)")
        if "day_profile_id" not in {r["name"] for r in cur.fetchall()}:
            cur.execute("ALTER TABLE grades ADD COLUMN day_profile_id INTEGER "
                        "REFERENCES day_profiles(id) ON DELETE SET NULL")
    conn.commit()

    _migrate_periods(conn, cur, use_pg, default_id)
    conn.commit()


def _migrate_periods(conn, cur, use_pg, default_id):
    """periods: add profile_id and widen UNIQUE(day, period_number) to
    UNIQUE(profile_id, day, period_number). Existing rows (and their ids, so
    timetable_entries stay valid) are kept and assigned to the Default profile."""
    if use_pg:
        cur.execute("ALTER TABLE periods ADD COLUMN IF NOT EXISTS profile_id INTEGER "
                    "REFERENCES day_profiles(id) ON DELETE CASCADE")
        cur.execute(db._adapt("UPDATE periods SET profile_id=? WHERE profile_id IS NULL"), (default_id,))
        cur.execute(
            """SELECT con.conname, array_agg(a.attname) AS cols
               FROM pg_constraint con
               JOIN pg_class rel ON rel.oid = con.conrelid
               JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey)
               WHERE rel.relname = 'periods' AND con.contype = 'u'
               GROUP BY con.conname""")
        for row in cur.fetchall():
            if set(row["cols"]) == {"day", "period_number"}:
                cur.execute(f'ALTER TABLE periods DROP CONSTRAINT "{row["conname"]}"')
        cur.execute("SELECT 1 FROM pg_constraint WHERE conname='periods_profile_day_number_key'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE periods ADD CONSTRAINT periods_profile_day_number_key "
                        "UNIQUE (profile_id, day, period_number)")
        cur.execute("ALTER TABLE periods ALTER COLUMN profile_id SET NOT NULL")
        return

    cur.execute("PRAGMA table_info(periods)")
    if "profile_id" in {r["name"] for r in cur.fetchall()}:
        return
    # SQLite cannot change a UNIQUE constraint in place: rebuild the table.
    # Foreign keys must be OFF while the old table is dropped, otherwise the
    # ON DELETE CASCADE would wipe every timetable entry.
    conn.commit()
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute(
            """CREATE TABLE periods_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL REFERENCES day_profiles(id) ON DELETE CASCADE,
                day TEXT NOT NULL,
                period_number INTEGER NOT NULL,
                start_time TEXT,
                end_time TEXT,
                is_break INTEGER DEFAULT 0,
                label TEXT,
                UNIQUE(profile_id, day, period_number)
            )""")
        cur.execute(
            "INSERT INTO periods_new (id, profile_id, day, period_number, start_time, end_time, is_break, label) "
            f"SELECT id, {int(default_id)}, day, period_number, start_time, end_time, is_break, label FROM periods")
        cur.execute("DROP TABLE periods")
        cur.execute("ALTER TABLE periods_new RENAME TO periods")
        conn.commit()
    finally:
        cur.execute("PRAGMA foreign_keys = ON")


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def default_id():
    return db.fetch_one("SELECT id FROM day_profiles WHERE is_default=1")["id"]


def list_profiles():
    """Every profile with the grades that use it (grades with no profile of
    their own count towards the Default)."""
    profiles = [dict(p) for p in db.fetch_all(
        "SELECT * FROM day_profiles ORDER BY is_default DESC, name")]
    did = default_id()
    grades = db.fetch_all("SELECT id, name, curriculum, day_profile_id FROM grades ORDER BY sort_order, id")
    for p in profiles:
        p["grades"] = [dict(g) for g in grades if (g["day_profile_id"] or did) == p["id"]]
        p["breaks"] = [dict(b) for b in breaks_for(p["id"])]
        p["day_end"] = day_end_time(p, p["breaks"])
    return profiles


def get_profile(pid):
    row = db.fetch_one("SELECT * FROM day_profiles WHERE id=?", (pid,)) if pid else None
    return dict(row) if row else dict(db.fetch_one("SELECT * FROM day_profiles WHERE is_default=1"))


def breaks_for(pid):
    return db.fetch_all("SELECT * FROM profile_breaks WHERE profile_id=? ORDER BY after_period", (pid,))


def profile_id_for_grade(grade_id):
    row = db.fetch_one("SELECT day_profile_id FROM grades WHERE id=?", (grade_id,))
    return (row["day_profile_id"] if row and row["day_profile_id"] else default_id())


def profile_id_for_stream(stream_id):
    row = db.fetch_one(
        "SELECT g.day_profile_id FROM streams s JOIN grades g ON g.id = s.grade_id WHERE s.id=?", (stream_id,))
    return (row["day_profile_id"] if row and row["day_profile_id"] else default_id())


def _layout(profile, breaks):
    """Walk the day once: returns (lesson_rows, break_rows, end_minutes)."""
    h, m = (int(x) for x in profile["start_time"].split(":"))
    t = h * 60 + m
    bmap = {}
    for b in breaks:
        e = bmap.setdefault(b["after_period"], {"duration": 0, "label": b["label"]})
        e["duration"] += b["duration_minutes"]
        if b["label"]:
            e["label"] = b["label"]
    lessons, brks = [], []
    fmt = lambda x: f"{x // 60:02d}:{x % 60:02d}"
    for p in range(1, profile["periods_per_day"] + 1):
        lessons.append({"period_number": p, "start_time": fmt(t), "end_time": fmt(t + profile["lesson_minutes"])})
        t += profile["lesson_minutes"]
        if p in bmap and p < profile["periods_per_day"]:
            brks.append({"after_period": p, "label": bmap[p]["label"] or "Break",
                         "start_time": fmt(t), "end_time": fmt(t + bmap[p]["duration"])})
            t += bmap[p]["duration"]
    return lessons, brks, t


def day_end_time(profile, breaks):
    t = _layout(profile, breaks)[2]
    return f"{t // 60:02d}:{t % 60:02d}"


def break_rows(pid):
    """Display rows for breaks (same shape database.get_break_rows returned)."""
    return _layout(get_profile(pid), breaks_for(get_profile(pid)["id"]))[1]


def period_rows(pid):
    return db.fetch_all(
        "SELECT DISTINCT period_number, start_time, end_time FROM periods "
        "WHERE profile_id=? AND is_break=0 ORDER BY period_number", (pid,))


def rows_with_breaks(pid):
    """Lesson + break rows for one profile, in day order (grid rows)."""
    brk = {b["after_period"]: b for b in break_rows(pid)}
    rows = []
    for p in period_rows(pid):
        rows.append({"type": "lesson", "period_number": p["period_number"],
                     "start_time": p["start_time"], "end_time": p["end_time"]})
        if p["period_number"] in brk:
            b = brk[p["period_number"]]
            rows.append({"type": "break", "label": b["label"],
                         "start_time": b["start_time"], "end_time": b["end_time"]})
    return rows


def teacher_rows():
    """One merged row per distinct clock slot across all profiles. Used by the
    teacher timetable, because a teacher can serve classes on different grids.
    period_number holds the start time ("08:00") and plabel the display text."""
    slots = db.fetch_all(
        "SELECT DISTINCT start_time, end_time FROM periods WHERE is_break=0 ORDER BY start_time, end_time")
    return [{"type": "lesson", "period_number": s["start_time"], "plabel": s["start_time"],
             "start_time": s["start_time"], "end_time": s["end_time"]} for s in slots]


def profile_count():
    return db.fetch_one("SELECT COUNT(*) AS c FROM day_profiles")["c"]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def create_profile(name, copy_from=None):
    src = get_profile(copy_from)
    pid = db.execute(
        "INSERT INTO day_profiles (name, start_time, lesson_minutes, periods_per_day, is_default) "
        "VALUES (?, ?, ?, ?, 0)",
        (name, src["start_time"], src["lesson_minutes"], src["periods_per_day"]))
    for b in breaks_for(src["id"]):
        db.execute("INSERT INTO profile_breaks (profile_id, after_period, duration_minutes, label) VALUES (?, ?, ?, ?)",
                   (pid, b["after_period"], b["duration_minutes"], b["label"]))
    return pid


def save_profile(pid, name, start_time, lesson_minutes, periods_per_day, break_list):
    """break_list: [(after_period, minutes, label)]. Returns the number of breaks
    dropped because they no longer fit inside the day."""
    kept = [(a, d, l) for a, d, l in break_list if 1 <= a < periods_per_day and d >= 1]
    db.execute("UPDATE day_profiles SET name=?, start_time=?, lesson_minutes=?, periods_per_day=? WHERE id=?",
               (name, start_time, lesson_minutes, periods_per_day, pid))
    db.execute("DELETE FROM profile_breaks WHERE profile_id=?", (pid,))
    for a, d, l in kept:
        db.execute("INSERT INTO profile_breaks (profile_id, after_period, duration_minutes, label) VALUES (?, ?, ?, ?)",
                   (pid, a, d, l))
    regenerate(pid)
    return len(break_list) - len(kept)


def delete_profile(pid):
    """Grades on this profile move to the Default (their lessons are remapped by
    day + period number). The Default profile itself cannot be deleted."""
    if pid == default_id():
        return False
    for g in db.fetch_all("SELECT id FROM grades WHERE day_profile_id=?", (pid,)):
        assign_grades(default_id(), [g["id"]])
    db.execute("DELETE FROM day_profiles WHERE id=?", (pid,))
    return True


def assign_grades(pid, grade_ids):
    """Point grades at a profile and carry their existing lessons across by
    (day, period_number). Lessons whose period does not exist on the new grid
    are removed. Returns (moved, removed)."""
    moved = removed = 0
    new_value = None if pid == default_id() else pid
    for gid in grade_ids:
        old = profile_id_for_grade(gid)
        db.execute("UPDATE grades SET day_profile_id=? WHERE id=?", (new_value, gid))
        if old == pid:
            continue
        entries = db.fetch_all(
            "SELECT te.id, p.day, p.period_number FROM timetable_entries te "
            "JOIN periods p ON p.id = te.period_id JOIN streams s ON s.id = te.stream_id WHERE s.grade_id=?", (gid,))
        for e in entries:
            target = db.fetch_one("SELECT id FROM periods WHERE profile_id=? AND day=? AND period_number=?",
                                  (pid, e["day"], e["period_number"]))
            if target:
                db.execute("UPDATE timetable_entries SET period_id=? WHERE id=?", (target["id"], e["id"]))
                moved += 1
            else:
                db.execute("DELETE FROM timetable_entries WHERE id=?", (e["id"],))
                removed += 1
    return moved, removed


def assign_curriculum(pid, curriculum_code):
    ids = [g["id"] for g in db.fetch_all("SELECT id FROM grades WHERE curriculum=?", (curriculum_code,))]
    return assign_grades(pid, ids) if ids else (0, 0)


# --------------------------------------------------------------------------
# Period generation (per profile)
# --------------------------------------------------------------------------
def regenerate_all(cur):
    """Used by database._regenerate_periods: rebuild every profile's periods,
    and drop periods of days that are no longer switched on."""
    cur.execute("SELECT * FROM day_profiles")
    profiles = cur.fetchall()
    active = db._get_active_days(cur)
    cur.execute("SELECT DISTINCT day FROM periods")
    for r in cur.fetchall():
        if r["day"] not in active:
            cur.execute(db._adapt("DELETE FROM periods WHERE day=?"), (r["day"],))
    for p in profiles:
        _regenerate_profile(cur, dict(p), active)


def regenerate(pid=None):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM day_profiles" + ("" if pid is None else " WHERE id=%s" % int(pid)))
        profiles = cur.fetchall()
        active = db._get_active_days(cur)
        for p in profiles:
            _regenerate_profile(cur, dict(p), active)
        conn.commit()
    finally:
        conn.close()


def _regenerate_profile(cur, prof, active_days):
    cur.execute(db._adapt("SELECT after_period, duration_minutes, label FROM profile_breaks WHERE profile_id=?"),
                (prof["id"],))
    lessons, _, _ = _layout(prof, cur.fetchall())
    n = prof["periods_per_day"]
    for day in active_days:
        cur.execute(db._adapt("SELECT period_number FROM periods WHERE profile_id=? AND day=?"), (prof["id"], day))
        existing = {r["period_number"] for r in cur.fetchall()}
        for num in existing:
            if num > n:
                cur.execute(db._adapt("DELETE FROM periods WHERE profile_id=? AND day=? AND period_number=?"),
                            (prof["id"], day, num))
        for l in lessons:
            num = l["period_number"]
            if num in existing:
                cur.execute(db._adapt("UPDATE periods SET start_time=?, end_time=?, label=? "
                                      "WHERE profile_id=? AND day=? AND period_number=?"),
                            (l["start_time"], l["end_time"], f"Lesson {num}", prof["id"], day, num))
            else:
                cur.execute(db._adapt("INSERT INTO periods (profile_id, day, period_number, start_time, end_time, "
                                      "is_break, label) VALUES (?, ?, ?, ?, ?, 0, ?)"),
                            (prof["id"], day, num, l["start_time"], l["end_time"], f"Lesson {num}"))


# --------------------------------------------------------------------------
# Clash checking across different grids
# --------------------------------------------------------------------------
# Drop-in replacement for "te.period_id=?" in the builder's conflict queries:
# same parameter order, but matches ANY period on that day whose clock time
# overlaps the one being edited (identical to the old behaviour when there is
# only one profile).
OVERLAP = ("te.period_id IN (SELECT q.id FROM periods q JOIN periods p ON p.id=? "
           "WHERE q.day=p.day AND q.start_time < p.end_time AND p.start_time < q.end_time)")


def double_bookings():
    """Teachers (or rooms) booked in two different classes at overlapping times."""
    out = []
    for col, label in (("teacher_id", "teacher"), ("room_id", "room")):
        rows = db.fetch_all(
            f"""SELECT x.{col} AS who, p1.day, p1.start_time, p1.end_time,
                       s1.name AS class_a, s2.name AS class_b
                FROM timetable_entries x
                JOIN timetable_entries y ON y.{col} = x.{col} AND y.id > x.id AND y.stream_id != x.stream_id
                JOIN periods p1 ON p1.id = x.period_id JOIN periods p2 ON p2.id = y.period_id
                JOIN streams s1 ON s1.id = x.stream_id JOIN streams s2 ON s2.id = y.stream_id
                WHERE x.{col} IS NOT NULL AND p1.day = p2.day
                  AND p1.start_time < p2.end_time AND p2.start_time < p1.end_time""")
        names = {r["id"]: r["name"] for r in db.fetch_all("SELECT id, name FROM %s" % ("teachers" if col == "teacher_id" else "rooms"))}
        for r in rows:
            out.append({"kind": label, "name": names.get(r["who"], "?"), "day": r["day"],
                        "time": f"{r['start_time']}-{r['end_time']}", "classes": f"{r['class_a']} / {r['class_b']}"})
    return out
