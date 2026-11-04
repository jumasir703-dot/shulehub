"""
remedial.py — Extra working hours (remedial / extra tuition) with their own timetable.

The normal timetable (periods, breaks, timetable_entries) is never touched.

There are two independent schedules, each with its own settings, subjects,
teachers, Generate button and timetable:

    weekday   Monday to Friday
    weekend   Saturday and Sunday

Inside a schedule the extra hours have two blocks, a MORNING block (before
school, labelled M1, M2 ...) and an EVENING block (after school, labelled
E1, E2 ...). Each block has its own days, start time and number of sessions;
session length and the gap between sessions are shared by the two blocks.

Tables:

    remedial_config     one row per schedule: morning/evening days, start, sessions, length, gap
    remedial_slots      the sessions that result (e.g. Monday M1 06:30-07:10) — rebuilt on save
                        (slot_number 1-8 = morning, 11-18 = evening)
    remedial_plan2      per schedule and subject: how many remedial sessions a week
    remedial_teachers2  per schedule and subject: who may teach it
    remedial_entries    the generated timetable: class + subject + teacher + slot

(remedial_settings, remedial_plan and remedial_teachers are the old single-schedule
tables; their data is copied across once, automatically, and they are not used again.)

Pages (all under /remedial, each takes ?schedule=weekday or ?schedule=weekend):

    /                 the remedial timetable (class or teacher grid, print, CSV, PDF)
    /setup            Extra hours: morning and evening days, start times, sessions
    /plan             Subjects & teachers
    POST /generate    build the timetable of ONE schedule   POST /clear   empty it

Generator rules: a teacher is never in two classes in the same session, a class
has only one subject per session, and a subject is spread over different days
before it is repeated on a day. Anything that can't be placed is reported.

Viewing/exporting uses the "timetable_view" service; setup, plan, generate and
clear use "timetable_edit" (see permissions.py).
"""

import re
from collections import defaultdict

from flask import Blueprint, flash, redirect, render_template, request, url_for

import database as db
import master

remedial_bp = Blueprint("remedial", __name__, url_prefix="/remedial")

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PROFILES = {
    "weekday": {"label": "Weekdays", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
    "weekend": {"label": "Weekend", "days": ["Saturday", "Sunday"]},
}
PERIODS = (("morning", 0, "M", "Morning"), ("evening", 10, "E", "Evening"))   # key, slot_number base, label, name

DEFAULTS = {
    "weekday": {
        "morning": {"days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "start": "06:30", "sessions": 1},
        "evening": {"days": ["Monday", "Tuesday", "Wednesday", "Thursday"], "start": "16:00", "sessions": 2},
        "session_minutes": 40, "gap_minutes": 0,
    },
    "weekend": {
        "morning": {"days": ["Saturday"], "start": "08:00", "sessions": 4},
        "evening": {"days": [], "start": "16:00", "sessions": 2},
        "session_minutes": 40, "gap_minutes": 10,
    },
}
MAX_REPORT_LINES = 10

_DDL = """
CREATE TABLE IF NOT EXISTS remedial_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    days TEXT NOT NULL DEFAULT 'Monday,Tuesday,Wednesday,Thursday',
    start_time TEXT NOT NULL DEFAULT '16:00',
    session_minutes INTEGER NOT NULL DEFAULT 40,
    sessions_per_day INTEGER NOT NULL DEFAULT 2,
    gap_minutes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS remedial_slots (
    id {PK},
    day TEXT NOT NULL,
    slot_number INTEGER NOT NULL,
    start_time TEXT,
    end_time TEXT,
    UNIQUE(day, slot_number)
);

CREATE TABLE IF NOT EXISTS remedial_plan (
    subject_id INTEGER PRIMARY KEY REFERENCES subjects(id) ON DELETE CASCADE,
    sessions_per_week INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS remedial_teachers (
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (teacher_id, subject_id)
);

CREATE TABLE IF NOT EXISTS remedial_entries (
    id {PK},
    stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    slot_id INTEGER NOT NULL REFERENCES remedial_slots(id) ON DELETE CASCADE,
    UNIQUE(stream_id, slot_id)
);

CREATE TABLE IF NOT EXISTS remedial_config (
    profile TEXT PRIMARY KEY,
    morning_days TEXT NOT NULL DEFAULT '',
    morning_start TEXT NOT NULL DEFAULT '06:30',
    morning_sessions INTEGER NOT NULL DEFAULT 1,
    evening_days TEXT NOT NULL DEFAULT '',
    evening_start TEXT NOT NULL DEFAULT '16:00',
    evening_sessions INTEGER NOT NULL DEFAULT 2,
    session_minutes INTEGER NOT NULL DEFAULT 40,
    gap_minutes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS remedial_plan2 (
    profile TEXT NOT NULL,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    sessions_per_week INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (profile, subject_id)
);

CREATE TABLE IF NOT EXISTS remedial_teachers2 (
    profile TEXT NOT NULL,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (profile, teacher_id, subject_id)
);

CREATE TABLE IF NOT EXISTS remedial_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_ready = False


def ensure_schema():
    """Create the remedial tables if they're missing and copy the old single-schedule
    data across once. Idempotent, so existing databases (SQLite or Postgres) upgrade
    in place and nothing is lost."""
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
    _migrate_old_data()
    _ready = True


def _migrate_old_data():
    """One time: the first version had a single evening schedule. Keep it as the
    evening block, keep its generated sessions, and carry the subject plan over."""
    if db.fetch_one("SELECT value FROM remedial_meta WHERE key='v2'"):
        return
    old = db.fetch_one("SELECT days, start_time, session_minutes, sessions_per_day, gap_minutes "
                       "FROM remedial_settings WHERE id=1")
    if old:
        old_days = (old["days"] or "").split(",")
        for profile, info in PROFILES.items():
            days = ",".join(d for d in info["days"] if d in old_days)
            if days:
                db.execute(
                    "INSERT INTO remedial_config (profile, morning_days, morning_start, morning_sessions, "
                    "evening_days, evening_start, evening_sessions, session_minutes, gap_minutes) "
                    "VALUES (?, '', '06:30', 1, ?, ?, ?, ?, ?)",
                    (profile, days, old["start_time"], old["sessions_per_day"],
                     old["session_minutes"], old["gap_minutes"]))
        # the old sessions become evening sessions (numbers 11, 12 ...); safe to repeat
        db.execute("UPDATE remedial_slots SET slot_number = slot_number + 10 WHERE slot_number < 10")
    db.execute("INSERT INTO remedial_plan2 (profile, subject_id, sessions_per_week) "
               "SELECT 'weekday', subject_id, sessions_per_week FROM remedial_plan")
    db.execute("INSERT INTO remedial_teachers2 (profile, teacher_id, subject_id) "
               "SELECT 'weekday', teacher_id, subject_id FROM remedial_teachers")
    db.execute("INSERT INTO remedial_meta (key, value) VALUES ('v2', '1')")


@remedial_bp.before_request
def _before():
    ensure_schema()


# ---------------------------------------------------------------- helpers
def _profile():
    """Which schedule a request is about: ?schedule= (or the form field)."""
    key = request.values.get("schedule")
    if key in PROFILES:
        return key
    if get_config("weekday")["configured"] or not get_config("weekend")["configured"]:
        return "weekday"
    return "weekend"


def _render(template, profile, **kw):
    return render_template(template, schedule=profile, profiles=PROFILES, **kw)


def _in(values):
    return ",".join("?" for _ in values)


def _fmt(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _mins(t):
    h, m = (int(x) for x in t.split(":"))
    return h * 60 + m


def block_label(slot_number):
    """1 -> 'M1' (morning), 11 -> 'E1' (evening)."""
    return f"M{slot_number}" if slot_number < 10 else f"E{slot_number - 10}"


def slot_times(start_time, session_minutes, sessions_per_day, gap_minutes):
    """[(slot_number, 'HH:MM', 'HH:MM'), ...] for one block on one day."""
    t = _mins(start_time)
    out = []
    for n in range(1, sessions_per_day + 1):
        out.append((n, _fmt(t), _fmt(t + session_minutes)))
        t += session_minutes + gap_minutes
    return out


# ---------------------------------------------------------------- settings
def get_config(profile):
    valid = PROFILES[profile]["days"]
    row = db.fetch_one(
        "SELECT morning_days, morning_start, morning_sessions, evening_days, evening_start, "
        "evening_sessions, session_minutes, gap_minutes FROM remedial_config WHERE profile=?", (profile,))
    if not row:
        d = DEFAULTS[profile]
        return {"profile": profile, "configured": False,
                "morning": {**d["morning"], "days": list(d["morning"]["days"])},
                "evening": {**d["evening"], "days": list(d["evening"]["days"])},
                "session_minutes": d["session_minutes"], "gap_minutes": d["gap_minutes"]}

    def block(prefix):
        return {"days": [d for d in valid if d in (row[f"{prefix}_days"] or "").split(",")],
                "start": row[f"{prefix}_start"], "sessions": row[f"{prefix}_sessions"]}
    return {"profile": profile, "configured": True,
            "morning": block("morning"), "evening": block("evening"),
            "session_minutes": row["session_minutes"], "gap_minutes": row["gap_minutes"]}


def cfg_days(cfg):
    """Every day that has any extra session, in week order."""
    used = set(cfg["morning"]["days"]) | set(cfg["evening"]["days"])
    return [d for d in DAY_ORDER if d in used]


def has_sessions(cfg):
    return cfg["configured"] and bool(cfg_days(cfg))


def block_times(cfg):
    """{'morning': [(slot_number, start, end), ...], 'evening': [...]} (slot numbers 1.. / 11..)."""
    out = {}
    for key, base, _, _ in PERIODS:
        b = cfg[key]
        out[key] = [(base + n, s, e) for n, s, e in
                    slot_times(b["start"], cfg["session_minutes"], b["sessions"], cfg["gap_minutes"])]
    return out


def _valid_time(text):
    if not re.fullmatch(r"\d{1,2}:\d{2}", text or ""):
        return None
    h, m = (int(x) for x in text.split(":"))
    return f"{h:02d}:{m:02d}" if h < 24 and m < 60 else None


def desired_slots(cfg):
    """{(day, slot_number): (start, end)} for everything the settings ask for."""
    times = block_times(cfg)
    want = {}
    for key, _, _, _ in PERIODS:
        for day in cfg[key]["days"]:
            for n, s, e in times[key]:
                want[(day, n)] = (s, e)
    return want


def rebuild_slots(profile, cfg):
    """Bring remedial_slots in line with the settings for ONE schedule. Slots that
    still exist are updated in place (so generated sessions stay valid); slots that
    no longer exist are removed along with their sessions. Returns how many
    generated sessions were removed."""
    want = desired_slots(cfg) if cfg["configured"] else {}
    profile_days = PROFILES[profile]["days"]
    removed, keep = 0, {}
    for r in db.fetch_all("SELECT id, day, slot_number FROM remedial_slots"):
        if r["day"] not in profile_days:
            continue
        key = (r["day"], r["slot_number"])
        if key in want:
            keep[key] = r["id"]
        else:
            removed += db.fetch_one(
                "SELECT COUNT(*) AS c FROM remedial_entries WHERE slot_id=?", (r["id"],))["c"]
            db.execute("DELETE FROM remedial_entries WHERE slot_id=?", (r["id"],))
            db.execute("DELETE FROM remedial_slots WHERE id=?", (r["id"],))
    for (day, n), (start, end) in sorted(want.items(), key=lambda kv: (DAY_ORDER.index(kv[0][0]), kv[0][1])):
        if (day, n) in keep:
            db.execute("UPDATE remedial_slots SET start_time=?, end_time=? WHERE id=?",
                       (start, end, keep[(day, n)]))
        else:
            db.execute("INSERT INTO remedial_slots (day, slot_number, start_time, end_time) "
                       "VALUES (?, ?, ?, ?)", (day, n, start, end))
    return removed


def _main_day_bounds(day):
    """(first start, last end) of the normal lessons on a day, or None."""
    row = db.fetch_one(
        "SELECT MIN(start_time) AS s, MAX(end_time) AS e FROM periods WHERE day=? AND is_break=0", (day,))
    return (row["s"], row["e"]) if row and row["s"] and row["e"] else None


def overlap_warnings(cfg):
    """Plain-English warnings when extra hours run into normal lessons."""
    if not has_sessions(cfg):
        return []
    times = block_times(cfg)
    warns = []
    for key, _, _, name in PERIODS:
        if not cfg[key]["days"]:
            continue
        b_start, b_end = times[key][0][1], times[key][-1][2]
        for d in cfg[key]["days"]:
            bounds = _main_day_bounds(d)
            if bounds and not (b_end <= bounds[0] or b_start >= bounds[1]):
                warns.append(f"{d}: {name.lower()} sessions ({b_start}–{b_end}) overlap normal "
                             f"lessons ({bounds[0]}–{bounds[1]}).")
    return warns


def day_timeline(cfg):
    """Segments for a 'day at a glance' bar: morning sessions, normal lessons, evening sessions."""
    if not has_sessions(cfg):
        return None
    days = cfg_days(cfg)
    day = next((d for d in days if _main_day_bounds(d)), days[0])
    times = block_times(cfg)
    segs = []
    bounds = _main_day_bounds(day)
    if bounds:
        segs.append({"kind": "main", "label": "Normal lessons", "start": bounds[0], "end": bounds[1]})
    for key, _, _, _ in PERIODS:
        if day in cfg[key]["days"]:
            for n, s, e in times[key]:
                segs.append({"kind": "extra", "label": block_label(n), "start": s, "end": e})
    segs.sort(key=lambda x: (_mins(x["start"]), _mins(x["end"])))
    filled, cursor = [], None
    for sg in segs:
        if cursor is not None and _mins(sg["start"]) > cursor:
            filled.append({"kind": "gap", "label": "", "start": _fmt(cursor), "end": sg["start"]})
        filled.append(sg)
        cursor = max(cursor or 0, _mins(sg["end"]))
    total = _mins(filled[-1]["end"]) - _mins(filled[0]["start"])
    for sg in filled:
        sg["pct"] = max(round(100 * (_mins(sg["end"]) - _mins(sg["start"])) / total, 2), 1.5) if total > 0 else 0
    return {"day": day, "segments": filled}


# ---------------------------------------------------------------- plan
def _subjects():
    return db.fetch_all(
        "SELECT s.id, s.name, s.grade_id, s.pathway_id, g.name AS grade_name "
        "FROM subjects s JOIN grades g ON s.grade_id = g.id "
        "ORDER BY g.sort_order, g.id, s.name")


def get_plan(profile):
    return {r["subject_id"]: r["sessions_per_week"]
            for r in db.fetch_all("SELECT subject_id, sessions_per_week FROM remedial_plan2 WHERE profile=?",
                                  (profile,))}


def get_teacher_pool(profile):
    pool = defaultdict(list)
    for r in db.fetch_all("SELECT subject_id, teacher_id FROM remedial_teachers2 WHERE profile=? "
                          "ORDER BY teacher_id", (profile,)):
        pool[r["subject_id"]].append(r["teacher_id"])
    return pool


def profile_slots(profile):
    days = PROFILES[profile]["days"]
    return db.fetch_all(
        f"SELECT id, day, slot_number, start_time, end_time FROM remedial_slots WHERE day IN ({_in(days)})",
        tuple(days))


def total_slots(profile):
    return len(profile_slots(profile))


def _clear_entries(profile):
    days = PROFILES[profile]["days"]
    db.execute(f"DELETE FROM remedial_entries WHERE slot_id IN "
               f"(SELECT id FROM remedial_slots WHERE day IN ({_in(days)}))", tuple(days))


# ---------------------------------------------------------------- generator
def generate(profile):
    """Build the remedial timetable of ONE schedule from scratch (the other schedule
    is left alone). Returns [{"stream", "needed", "scheduled", "skipped": [str]}]."""
    day_order = {d: i for i, d in enumerate(DAY_ORDER)}
    slots = sorted(profile_slots(profile), key=lambda s: (day_order.get(s["day"], 99), s["slot_number"]))
    plan = get_plan(profile)
    pool = get_teacher_pool(profile)
    subjects = {s["id"]: s for s in _subjects()}
    streams = db.fetch_all(
        "SELECT s.id, s.name, s.grade_id, s.pathway_id FROM streams s JOIN grades g ON s.grade_id = g.id "
        "ORDER BY g.sort_order, g.id, s.name")

    _clear_entries(profile)

    by_grade = defaultdict(list)
    for sid, spw in plan.items():
        if sid in subjects:
            by_grade[subjects[sid]["grade_id"]].append((subjects[sid], spw))

    teacher_busy = set()          # (teacher_id, slot_id)
    teacher_load = defaultdict(int)
    report = []

    for stream in streams:
        eligible = [(s, spw) for s, spw in by_grade.get(stream["grade_id"], [])
                    if s["pathway_id"] is None or s["pathway_id"] == stream["pathway_id"]]
        if not eligible:
            continue
        eligible.sort(key=lambda x: (-x[1], x[0]["name"]))
        needed = sum(spw for _, spw in eligible)
        rep = {"stream": stream["name"], "needed": needed, "scheduled": 0, "skipped": []}
        if needed > len(slots):
            rep["skipped"].append(
                f"The plan asks for {needed} sessions a week but there are only {len(slots)} "
                f"remedial sessions, so some can't be placed.")

        free = list(slots)
        day_count = defaultdict(int)
        block_count = defaultdict(int)    # morning vs evening, so a class isn't all in one block
        for subject, spw in eligible:
            teachers = pool.get(subject["id"], [])
            if not teachers:
                rep["skipped"].append(f"{subject['name']}: no remedial teacher chosen, skipped")
                continue
            subject_days = set()
            placed = 0
            for _ in range(spw):
                best = None
                for sl in free:
                    t = min((t for t in teachers if (t, sl["id"]) not in teacher_busy),
                            key=lambda t: teacher_load[t], default=None)
                    if t is None:
                        continue
                    blk = sl["slot_number"] >= 10
                    score = (sl["day"] in subject_days, day_count[sl["day"]], block_count[blk],
                             day_order.get(sl["day"], 99), sl["slot_number"])
                    if best is None or score < best[0]:
                        best = (score, sl, t)
                if best is None:
                    break
                _, sl, t = best
                db.execute(
                    "INSERT INTO remedial_entries (stream_id, subject_id, teacher_id, slot_id) "
                    "VALUES (?, ?, ?, ?)", (stream["id"], subject["id"], t, sl["id"]))
                teacher_busy.add((t, sl["id"]))
                teacher_load[t] += 1
                free.remove(sl)
                subject_days.add(sl["day"])
                day_count[sl["day"]] += 1
                block_count[sl["slot_number"] >= 10] += 1
                placed += 1
            rep["scheduled"] += placed
            if placed < spw:
                rep["skipped"].append(
                    f"{subject['name']}: only {placed}/{spw} sessions placed (no free session or teacher left)")
        report.append(rep)
    return report


# ---------------------------------------------------------------- grid for the view / exports
def build_remedial(layout="class", day=master.WEEK, grade=None, profile="weekday"):
    """Same shape as master.build_master(), so the grid template, CSV and PDF are shared.
    Columns are labelled M1, M2 ... (morning) and E1, E2 ... (evening)."""
    cfg = get_config(profile)
    active = has_sessions(cfg)
    days = cfg_days(cfg) if active else []
    grades = master._grades()
    layout, day, grade = master._clean_choice(layout, day, grade, days, grades)
    shown_days = days if day == master.WEEK else [day]

    lesson_cols, exists = [], set()          # exists: (day, label) pairs that are real sessions
    if active:
        times = block_times(cfg)
        for key, _, _, _ in PERIODS:
            if not cfg[key]["days"]:
                continue
            for n, s, e in times[key]:
                label = block_label(n)
                lesson_cols.append({"type": "lesson", "period_number": label, "start_time": s, "end_time": e})
                exists.update((d, label) for d in cfg[key]["days"])

    entries = []
    if days:
        entries = db.fetch_all(
            f"""SELECT re.stream_id, re.teacher_id, sl.day, sl.slot_number,
                      sub.name AS subject_name, st.name AS stream_name, t.name AS teacher_name
               FROM remedial_entries re
               JOIN remedial_slots sl ON re.slot_id = sl.id
               JOIN subjects sub ON re.subject_id = sub.id
               JOIN streams st ON re.stream_id = st.id
               LEFT JOIN teachers t ON re.teacher_id = t.id
               WHERE sl.day IN ({_in(PROFILES[profile]['days'])})
               ORDER BY sub.name""", tuple(PROFILES[profile]["days"]))

    plan = get_plan(profile)
    if layout == "class":
        planned_grades = {s["grade_id"] for s in _subjects() if s["id"] in plan}
        with_entries = {e["stream_id"] for e in entries}
        sql = ("SELECT s.id, s.name, s.grade_id, g.name AS grade_name FROM streams s "
               "JOIN grades g ON s.grade_id = g.id ")
        params = ()
        if grade:
            sql += "WHERE s.grade_id=? "
            params = (grade,)
        sql += "ORDER BY g.sort_order, g.id, s.name"
        row_defs = [{"id": r["id"], "label": r["name"], "sub": r["grade_name"]}
                    for r in db.fetch_all(sql, params)
                    if r["grade_id"] in planned_grades or r["id"] in with_entries]
    else:
        row_defs = [{"id": r["id"], "label": r["name"], "sub": ""} for r in db.fetch_all(
            "SELECT id, name FROM teachers WHERE id IN "
            "(SELECT teacher_id FROM remedial_teachers2 WHERE profile=?) "
            "OR id IN (SELECT teacher_id FROM remedial_entries WHERE teacher_id IS NOT NULL) ORDER BY name",
            (profile,))]
        used = {e["teacher_id"] for e in entries}
        pool_ids = {t for ts in get_teacher_pool(profile).values() for t in ts}
        row_defs = [r for r in row_defs if r["id"] in used or r["id"] in pool_ids]

    wanted = {r["id"] for r in row_defs}
    cells = defaultdict(list)
    for e in entries:
        if layout == "class":
            row_id, other = e["stream_id"], e["teacher_name"] or "—"
        else:
            row_id, other = e["teacher_id"], e["stream_name"]
        if row_id in wanted:
            cells[(row_id, e["day"], block_label(e["slot_number"]))].append(
                {"subject": e["subject_name"], "other": other, "room": "", "clash": False})

    tables = []
    for d in shown_days:
        day_cols = [c for c in lesson_cols if (d, c["period_number"]) in exists]
        rows = []
        for r in row_defs:
            row_cells = {c["period_number"]: cells.get((r["id"], d, c["period_number"]), [])
                         for c in lesson_cols}
            busy = sum(1 for v in row_cells.values() if v)
            rows.append({**r, "cells": row_cells, "lessons": busy, "free": len(day_cols) - busy})
        tables.append({"day": d, "rows": rows})

    scope = "Whole week" if day == master.WEEK else day
    extra = f" · {next((g['name'] for g in grades if g['id'] == grade), '')}" if grade else ""
    title = (f"{db.get_school_settings()['name']} — Remedial Timetable, {PROFILES[profile]['label']} "
             f"({master.LAYOUTS[layout].lower()}) · {scope}{extra}")
    return {
        "title": title, "layout": layout, "day": day, "grade": grade,
        "days": days, "shown_days": shown_days, "grades": grades,
        "columns": lesson_cols, "lesson_cols": lesson_cols,
        "tables": tables, "clashes": [], "has_entries": bool(entries),
        "col_prefix": "", "count_label": "Sessions", "kind": "remedial",
        "profile": profile, "configured": active and bool(lesson_cols),
    }


# ---------------------------------------------------------------- pages
@remedial_bp.route("/")
def view_page():
    profile = _profile()
    m = build_remedial(*master._args(), profile=profile)
    return _render("remedial/view.html", profile, m=m, layouts=master.LAYOUTS, week=master.WEEK,
                   has_plan=bool(get_plan(profile)))


@remedial_bp.route("/export.csv")
def export_csv():
    return master.render_csv(build_remedial(*master._args(), profile=_profile()))


@remedial_bp.route("/export.pdf")
def export_pdf():
    return master.render_pdf(build_remedial(*master._args(), profile=_profile()))


# ---------------------------------------------------------------- setup (extra hours)
@remedial_bp.route("/setup")
def setup():
    profile = _profile()
    cfg = get_config(profile)
    return _render("remedial/setup.html", profile, s=cfg, times=block_times(cfg),
                   weekdays=PROFILES[profile]["days"], periods=PERIODS,
                   warnings=overlap_warnings(cfg), timeline=day_timeline(cfg),
                   per_class=total_slots(profile))


@remedial_bp.route("/setup/save", methods=["POST"])
def setup_save():
    profile = _profile()
    valid_days = PROFILES[profile]["days"]
    old = get_config(profile)
    form = request.form

    blocks, problem = {}, None
    for key, _, _, name in PERIODS:
        p = key[0]                     # form prefix: m_ / e_
        days = [d for d in valid_days if d in form.getlist(f"{p}_days")]
        start = _valid_time(form.get(f"{p}_start", "").strip())
        sessions = form.get(f"{p}_sessions", type=int)
        if days:
            if not start:
                problem = f"{name}: enter a valid start time."
            elif not sessions or not 1 <= sessions <= 8:
                problem = f"{name}: sessions per day must be between 1 and 8."
        else:                          # block switched off: keep sensible values for later
            start = start or old[key]["start"]
            sessions = sessions if sessions and 1 <= sessions <= 8 else old[key]["sessions"]
        blocks[key] = {"days": days, "start": start, "sessions": sessions}
        if problem:
            break

    minutes = form.get("session_minutes", type=int)
    gap = form.get("gap_minutes", type=int)
    gap = 0 if gap is None else gap
    if not problem:
        if not minutes or not 10 <= minutes <= 180:
            problem = "Session length must be between 10 and 180 minutes."
        elif not 0 <= gap <= 60:
            problem = "The gap between sessions must be between 0 and 60 minutes."

    if not problem:
        ends = {}
        for key, _, _, name in PERIODS:
            b = blocks[key]
            if b["days"]:
                last = slot_times(b["start"], minutes, b["sessions"], gap)[-1][2]
                if _mins(last) > 23 * 60 + 59 or _mins(last) <= _mins(b["start"]):
                    problem = f"{name} sessions would run past midnight. Start earlier or shorten them."
                ends[key] = last
        shared = set(blocks["morning"]["days"]) & set(blocks["evening"]["days"])
        if not problem and shared and "morning" in ends and \
                _mins(ends["morning"]) > _mins(blocks["evening"]["start"]):
            problem = ("The morning sessions end after the evening sessions start on a day that has both. "
                       "Move the evening start later or shorten the morning block.")
    if problem:
        flash(problem, "error")
        return redirect(url_for("remedial.setup", schedule=profile))

    db.execute(
        "INSERT INTO remedial_config (profile, morning_days, morning_start, morning_sessions, "
        "evening_days, evening_start, evening_sessions, session_minutes, gap_minutes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (profile) DO UPDATE SET "
        "morning_days = EXCLUDED.morning_days, morning_start = EXCLUDED.morning_start, "
        "morning_sessions = EXCLUDED.morning_sessions, evening_days = EXCLUDED.evening_days, "
        "evening_start = EXCLUDED.evening_start, evening_sessions = EXCLUDED.evening_sessions, "
        "session_minutes = EXCLUDED.session_minutes, gap_minutes = EXCLUDED.gap_minutes",
        (profile, ",".join(blocks["morning"]["days"]), blocks["morning"]["start"], blocks["morning"]["sessions"],
         ",".join(blocks["evening"]["days"]), blocks["evening"]["start"], blocks["evening"]["sessions"],
         minutes, gap))
    cfg = get_config(profile)
    removed = rebuild_slots(profile, cfg)
    label = PROFILES[profile]["label"].lower()
    if cfg_days(cfg):
        flash(f"Extra hours saved for the {label} schedule.", "success")
    else:
        flash(f"Saved. No days are ticked, so the {label} schedule has no extra sessions.", "success")
    if removed:
        flash(f"{removed} generated remedial session(s) were in slots that no longer exist and were removed. "
              f"Generate the {label} timetable again to refill them.", "error")
    return redirect(url_for("remedial.setup", schedule=profile))


# ---------------------------------------------------------------- plan (subjects & teachers)
@remedial_bp.route("/plan")
def plan():
    profile = _profile()
    plan_map = get_plan(profile)
    pool = get_teacher_pool(profile)
    teachers = db.fetch_all("SELECT id, name FROM teachers ORDER BY name")
    grades, seen = [], {}
    for s in _subjects():
        g = seen.get(s["grade_id"])
        if g is None:
            g = seen[s["grade_id"]] = {"id": s["grade_id"], "name": s["grade_name"], "subjects": []}
            grades.append(g)
        g["subjects"].append({
            "id": s["id"], "name": s["name"],
            "on": s["id"] in plan_map, "spw": plan_map.get(s["id"], 1),
            "teacher_ids": set(pool.get(s["id"], [])),
        })
    return _render("remedial/plan.html", profile, grades=grades, teachers=teachers,
                   slots=total_slots(profile), configured=has_sessions(get_config(profile)))


@remedial_bp.route("/plan/save", methods=["POST"])
def plan_save():
    profile = _profile()
    valid_teachers = {t["id"] for t in db.fetch_all("SELECT id FROM teachers")}
    db.execute("DELETE FROM remedial_plan2 WHERE profile=?", (profile,))
    db.execute("DELETE FROM remedial_teachers2 WHERE profile=?", (profile,))
    on = 0
    for s in _subjects():
        sid = s["id"]
        if f"on_{sid}" in request.form:
            spw = request.form.get(f"spw_{sid}", type=int) or 1
            db.execute("INSERT INTO remedial_plan2 (profile, subject_id, sessions_per_week) VALUES (?, ?, ?)",
                       (profile, sid, max(1, min(spw, 10))))
            on += 1
        for tid in request.form.getlist(f"t_{sid}"):
            if tid.isdigit() and int(tid) in valid_teachers:
                db.execute("INSERT INTO remedial_teachers2 (profile, teacher_id, subject_id) VALUES (?, ?, ?)",
                           (profile, int(tid), sid))
    flash(f"Saved: {on} subject(s) in the {PROFILES[profile]['label'].lower()} remedial plan.", "success")
    return redirect(url_for("remedial.plan", schedule=profile))


# ---------------------------------------------------------------- generate / clear
@remedial_bp.route("/generate", methods=["POST"])
def generate_view():
    profile = _profile()
    label = PROFILES[profile]["label"].lower()
    if not has_sessions(get_config(profile)) or not total_slots(profile):
        flash(f"Set up the {label} extra hours first.", "error")
        return redirect(url_for("remedial.setup", schedule=profile))
    if not get_plan(profile):
        flash(f"Choose the subjects for the {label} remedial first.", "error")
        return redirect(url_for("remedial.plan", schedule=profile))
    report = generate(profile)
    placed = sum(r["scheduled"] for r in report)
    flash(f"{PROFILES[profile]['label']} remedial timetable generated: {placed} session(s) "
          f"across {len(report)} class(es).", "success")
    lines = [f"{r['stream']} — {line}" for r in report for line in r["skipped"]]
    for line in lines[:MAX_REPORT_LINES]:
        flash(line, "error")
    if len(lines) > MAX_REPORT_LINES:
        flash(f"…and {len(lines) - MAX_REPORT_LINES} more things that couldn't be placed.", "error")
    return redirect(url_for("remedial.view_page", schedule=profile))


@remedial_bp.route("/clear", methods=["POST"])
def clear():
    profile = _profile()
    _clear_entries(profile)
    flash(f"The {PROFILES[profile]['label'].lower()} remedial timetable was cleared.", "success")
    return redirect(url_for("remedial.view_page", schedule=profile))
