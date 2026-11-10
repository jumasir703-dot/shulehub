"""
remedial.py — Extra working hours (remedial / extra tuition) with their own timetable.

The normal timetable (periods, breaks, timetable_entries) is never touched.
Extra hours live in their own tables:

    remedial_settings   which days, start time, session length, sessions per day, gap
    remedial_slots      the sessions that result (e.g. Monday S1 16:00-16:40) — rebuilt on save
    remedial_plan       per subject (so, per grade): how many remedial sessions a week
    remedial_teachers   per subject: who may teach its remedial sessions (chosen
                        separately from the normal timetable's teachers)
    remedial_entries    the generated timetable: class + subject + teacher + slot

Pages (all under /remedial):

    /                 the remedial timetable (class or teacher grid, print, CSV, PDF)
    /setup            Extra hours: days, start time, session length, sessions per day
    /plan             Subjects & teachers: which subjects each grade gets, and who teaches them
    POST /generate    build the remedial timetable   POST /clear   empty it

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

# These two tables have no 'id' column. On Postgres, database.execute() adds
# "RETURNING id" to every INSERT unless the table is listed here.
db.TABLES_WITHOUT_ID.update({"remedial_plan", "remedial_teachers"})

DEFAULTS = {
    "days": ["Monday", "Tuesday", "Wednesday", "Thursday"],
    "start_time": "16:00",
    "session_minutes": 40,
    "sessions_per_day": 2,
    "gap_minutes": 0,
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
"""

_ready = set()   # schools whose tables are known to exist


def _school_key():
    try:
        import tenancy
        return tenancy.current()
    except ImportError:
        return 1


def ensure_schema():
    """Create the remedial tables if they're missing. Idempotent, so existing
    databases (SQLite or Postgres) upgrade in place and nothing is lost."""
    key = _school_key()
    if key in _ready:
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
    _ready.add(key)


@remedial_bp.before_request
def _before():
    ensure_schema()


# ---------------------------------------------------------------- settings
def _fmt(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def slot_times(start_time, session_minutes, sessions_per_day, gap_minutes):
    """[(slot_number, 'HH:MM', 'HH:MM'), ...] for one day."""
    h, m = (int(x) for x in start_time.split(":"))
    t = h * 60 + m
    out = []
    for n in range(1, sessions_per_day + 1):
        out.append((n, _fmt(t), _fmt(t + session_minutes)))
        t += session_minutes + gap_minutes
    return out


def get_settings():
    row = db.fetch_one(
        "SELECT days, start_time, session_minutes, sessions_per_day, gap_minutes "
        "FROM remedial_settings WHERE id=1")
    if not row:
        return {**DEFAULTS, "days": list(DEFAULTS["days"]), "configured": False}
    days = [d for d in db.ALL_WEEKDAYS if d in (row["days"] or "").split(",")]
    return {"days": days, "start_time": row["start_time"], "session_minutes": row["session_minutes"],
            "sessions_per_day": row["sessions_per_day"], "gap_minutes": row["gap_minutes"],
            "configured": True}


def _valid_time(text):
    if not re.fullmatch(r"\d{1,2}:\d{2}", text or ""):
        return None
    h, m = (int(x) for x in text.split(":"))
    return f"{h:02d}:{m:02d}" if h < 24 and m < 60 else None


def rebuild_slots(settings):
    """Bring remedial_slots in line with the settings. Slots that still exist
    are updated in place (so generated sessions stay valid); slots for days or
    session numbers that no longer exist are removed along with their sessions.
    Returns how many generated sessions were removed."""
    times = slot_times(settings["start_time"], settings["session_minutes"],
                       settings["sessions_per_day"], settings["gap_minutes"])
    count = len(times)
    existing = db.fetch_all("SELECT id, day, slot_number FROM remedial_slots")
    removed = 0
    keep = {}
    for r in existing:
        if r["day"] not in settings["days"] or r["slot_number"] > count:
            removed += db.fetch_one(
                "SELECT COUNT(*) AS c FROM remedial_entries WHERE slot_id=?", (r["id"],))["c"]
            db.execute("DELETE FROM remedial_entries WHERE slot_id=?", (r["id"],))
            db.execute("DELETE FROM remedial_slots WHERE id=?", (r["id"],))
        else:
            keep[(r["day"], r["slot_number"])] = r["id"]
    for day in settings["days"]:
        for n, start, end in times:
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


def overlap_warnings(settings):
    """Plain-English warnings when extra hours run into normal lessons."""
    if not settings["configured"]:
        return []
    times = slot_times(settings["start_time"], settings["session_minutes"],
                       settings["sessions_per_day"], settings["gap_minutes"])
    extra_start, extra_end = times[0][1], times[-1][2]
    warns = []
    for d in settings["days"]:
        bounds = _main_day_bounds(d)
        if bounds and not (extra_end <= bounds[0] or extra_start >= bounds[1]):
            warns.append(f"{d}: extra hours ({extra_start}–{extra_end}) overlap normal lessons "
                         f"({bounds[0]}–{bounds[1]}).")
    return warns


def day_timeline(settings):
    """Segments for a 'day at a glance' bar: normal lessons, gap, extra sessions."""
    if not settings["configured"] or not settings["days"]:
        return None
    day = next((d for d in settings["days"] if _main_day_bounds(d)), settings["days"][0])
    times = slot_times(settings["start_time"], settings["session_minutes"],
                       settings["sessions_per_day"], settings["gap_minutes"])

    def mins(t):
        h, m = (int(x) for x in t.split(":"))
        return h * 60 + m

    segs = []
    bounds = _main_day_bounds(day)
    if bounds:
        segs.append({"kind": "main", "label": "Normal lessons", "start": bounds[0], "end": bounds[1]})
    for n, s, e in times:
        segs.append({"kind": "extra", "label": f"S{n}", "start": s, "end": e})
    segs.sort(key=lambda x: (mins(x["start"]), mins(x["end"])))
    # fill the empty stretches between segments so the bar is to scale
    filled, cursor = [], None
    for sg in segs:
        if cursor is not None and mins(sg["start"]) > cursor:
            filled.append({"kind": "gap", "label": "", "start": _fmt(cursor), "end": sg["start"]})
        filled.append(sg)
        cursor = max(cursor or 0, mins(sg["end"]))
    total = mins(filled[-1]["end"]) - mins(filled[0]["start"])
    for sg in filled:
        sg["pct"] = max(round(100 * (mins(sg["end"]) - mins(sg["start"])) / total, 2), 1.5) if total > 0 else 0
    return {"day": day, "segments": filled}


# ---------------------------------------------------------------- plan
def _subjects():
    return db.fetch_all(
        "SELECT s.id, s.name, s.grade_id, s.pathway_id, g.name AS grade_name "
        "FROM subjects s JOIN grades g ON s.grade_id = g.id "
        "ORDER BY g.sort_order, g.id, s.name")


def get_plan():
    return {r["subject_id"]: r["sessions_per_week"]
            for r in db.fetch_all("SELECT subject_id, sessions_per_week FROM remedial_plan")}


def get_teacher_pool():
    pool = defaultdict(list)
    for r in db.fetch_all("SELECT subject_id, teacher_id FROM remedial_teachers ORDER BY teacher_id"):
        pool[r["subject_id"]].append(r["teacher_id"])
    return pool


def total_slots():
    return db.fetch_one("SELECT COUNT(*) AS c FROM remedial_slots")["c"]


# ---------------------------------------------------------------- generator
def generate():
    """Build the remedial timetable from scratch.
    Returns [{"stream", "needed", "scheduled", "skipped": [str]}]."""
    day_order = {d: i for i, d in enumerate(db.ALL_WEEKDAYS)}
    slots = sorted(db.fetch_all("SELECT id, day, slot_number FROM remedial_slots"),
                   key=lambda s: (day_order.get(s["day"], 99), s["slot_number"]))
    plan = get_plan()
    pool = get_teacher_pool()
    subjects = {s["id"]: s for s in _subjects()}
    streams = db.fetch_all(
        "SELECT s.id, s.name, s.grade_id, s.pathway_id FROM streams s JOIN grades g ON s.grade_id = g.id "
        "ORDER BY g.sort_order, g.id, s.name")

    db.execute("DELETE FROM remedial_entries")

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
                    score = (sl["day"] in subject_days, day_count[sl["day"]],
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
                placed += 1
            rep["scheduled"] += placed
            if placed < spw:
                rep["skipped"].append(
                    f"{subject['name']}: only {placed}/{spw} sessions placed (no free session or teacher left)")
        report.append(rep)
    return report


# ---------------------------------------------------------------- grid for the view / exports
def build_remedial(layout="class", day=master.WEEK, grade=None):
    """Same shape as master.build_master(), so the grid template, CSV and PDF are shared."""
    settings = get_settings()
    days = settings["days"] if settings["configured"] else []
    grades = master._grades()
    layout, day, grade = master._clean_choice(layout, day, grade, days, grades)
    shown_days = days if day == master.WEEK else [day]

    lesson_cols = [{"type": "lesson", "period_number": r["slot_number"],
                    "start_time": r["start_time"], "end_time": r["end_time"]}
                   for r in db.fetch_all(
                       "SELECT DISTINCT slot_number, start_time, end_time FROM remedial_slots ORDER BY slot_number")]

    entries = db.fetch_all(
        """SELECT re.stream_id, re.teacher_id, sl.day, sl.slot_number,
                  sub.name AS subject_name, st.name AS stream_name, t.name AS teacher_name
           FROM remedial_entries re
           JOIN remedial_slots sl ON re.slot_id = sl.id
           JOIN subjects sub ON re.subject_id = sub.id
           JOIN streams st ON re.stream_id = st.id
           LEFT JOIN teachers t ON re.teacher_id = t.id
           ORDER BY sub.name""")

    if layout == "class":
        planned_grades = {s["grade_id"] for s in _subjects() if s["id"] in get_plan()}
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
            "SELECT id, name FROM teachers WHERE id IN (SELECT teacher_id FROM remedial_teachers) "
            "OR id IN (SELECT teacher_id FROM remedial_entries WHERE teacher_id IS NOT NULL) ORDER BY name")]

    wanted = {r["id"] for r in row_defs}
    cells = defaultdict(list)
    for e in entries:
        if layout == "class":
            row_id, other = e["stream_id"], e["teacher_name"] or "—"
        else:
            row_id, other = e["teacher_id"], e["stream_name"]
        if row_id in wanted:
            cells[(row_id, e["day"], e["slot_number"])].append(
                {"subject": e["subject_name"], "other": other, "room": "", "clash": False})

    tables = []
    for d in shown_days:
        rows = []
        for r in row_defs:
            row_cells = {c["period_number"]: cells.get((r["id"], d, c["period_number"]), [])
                         for c in lesson_cols}
            busy = sum(1 for v in row_cells.values() if v)
            rows.append({**r, "cells": row_cells, "lessons": busy, "free": len(lesson_cols) - busy})
        tables.append({"day": d, "rows": rows})

    scope = "Whole week" if day == master.WEEK else day
    extra = f" · {next((g['name'] for g in grades if g['id'] == grade), '')}" if grade else ""
    title = (f"{db.get_school_settings()['name']} — Remedial Timetable "
             f"({master.LAYOUTS[layout].lower()}) · {scope}{extra}")
    return {
        "title": title, "layout": layout, "day": day, "grade": grade,
        "days": days, "shown_days": shown_days, "grades": grades,
        "columns": lesson_cols, "lesson_cols": lesson_cols,
        "tables": tables, "clashes": [], "has_entries": bool(entries),
        "col_prefix": "S", "count_label": "Sessions", "kind": "remedial",
        "configured": settings["configured"] and bool(lesson_cols),
    }


# ---------------------------------------------------------------- pages
@remedial_bp.route("/")
def view_page():
    m = build_remedial(*master._args())
    return render_template("remedial/view.html", m=m, layouts=master.LAYOUTS, week=master.WEEK,
                           has_plan=bool(get_plan()))


@remedial_bp.route("/export.csv")
def export_csv():
    return master.render_csv(build_remedial(*master._args()))


@remedial_bp.route("/export.pdf")
def export_pdf():
    return master.render_pdf(build_remedial(*master._args()))


# ---------------------------------------------------------------- setup (extra hours)
@remedial_bp.route("/setup")
def setup():
    s = get_settings()
    times = slot_times(s["start_time"], s["session_minutes"], s["sessions_per_day"], s["gap_minutes"])
    main_bounds = {d: _main_day_bounds(d) for d in db.get_active_days()}
    return render_template("remedial/setup.html", s=s, times=times, weekdays=db.ALL_WEEKDAYS,
                           warnings=overlap_warnings(s), timeline=day_timeline(s),
                           main_bounds=main_bounds)


@remedial_bp.route("/setup/save", methods=["POST"])
def setup_save():
    days = [d for d in db.ALL_WEEKDAYS if d in request.form.getlist("days")]
    start = _valid_time(request.form.get("start_time", "").strip())
    minutes = request.form.get("session_minutes", type=int)
    per_day = request.form.get("sessions_per_day", type=int)
    gap = request.form.get("gap_minutes", type=int)
    gap = 0 if gap is None else gap

    problem = None
    if not days:
        problem = "Tick at least one day for the extra hours."
    elif not start:
        problem = "Enter a valid start time."
    elif not minutes or not 10 <= minutes <= 180:
        problem = "Session length must be between 10 and 180 minutes."
    elif not per_day or not 1 <= per_day <= 8:
        problem = "Sessions per day must be between 1 and 8."
    elif not 0 <= gap <= 60:
        problem = "The gap between sessions must be between 0 and 60 minutes."
    else:
        last_end = slot_times(start, minutes, per_day, gap)[-1][2]
        h, m = (int(x) for x in last_end.split(":"))
        if h * 60 + m > 23 * 60 + 59 or last_end <= start:
            problem = "The sessions would run past midnight. Start earlier or shorten them."
    if problem:
        flash(problem, "error")
        return redirect(url_for("remedial.setup"))

    days_str = ",".join(days)
    db.execute(
        "INSERT INTO remedial_settings (id, days, start_time, session_minutes, sessions_per_day, gap_minutes) "
        "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET days = EXCLUDED.days, "
        "start_time = EXCLUDED.start_time, session_minutes = EXCLUDED.session_minutes, "
        "sessions_per_day = EXCLUDED.sessions_per_day, gap_minutes = EXCLUDED.gap_minutes",
        (days_str, start, minutes, per_day, gap))
    removed = rebuild_slots(get_settings())
    flash("Extra hours saved.", "success")
    if removed:
        flash(f"{removed} generated remedial session(s) were in slots that no longer exist and were removed. "
              f"Generate the remedial timetable again to refill them.", "error")
    return redirect(url_for("remedial.setup"))


# ---------------------------------------------------------------- plan (subjects & teachers)
@remedial_bp.route("/plan")
def plan():
    plan_map = get_plan()
    pool = get_teacher_pool()
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
    return render_template("remedial/plan.html", grades=grades, teachers=teachers,
                           slots=total_slots(), configured=get_settings()["configured"])


@remedial_bp.route("/plan/save", methods=["POST"])
def plan_save():
    valid_teachers = {t["id"] for t in db.fetch_all("SELECT id FROM teachers")}
    db.execute("DELETE FROM remedial_plan")
    db.execute("DELETE FROM remedial_teachers")
    on = 0
    for s in _subjects():
        sid = s["id"]
        if f"on_{sid}" in request.form:
            spw = request.form.get(f"spw_{sid}", type=int) or 1
            db.execute("INSERT INTO remedial_plan (subject_id, sessions_per_week) VALUES (?, ?)",
                       (sid, max(1, min(spw, 10))))
            on += 1
        for tid in request.form.getlist(f"t_{sid}"):
            if tid.isdigit() and int(tid) in valid_teachers:
                db.execute("INSERT INTO remedial_teachers (teacher_id, subject_id) VALUES (?, ?)",
                           (int(tid), sid))
    flash(f"Saved: {on} subject(s) in the remedial plan.", "success")
    return redirect(url_for("remedial.plan"))


# ---------------------------------------------------------------- generate / clear
@remedial_bp.route("/generate", methods=["POST"])
def generate_view():
    if not get_settings()["configured"] or not total_slots():
        flash("Set up the extra hours first.", "error")
        return redirect(url_for("remedial.setup"))
    if not get_plan():
        flash("Choose the subjects for remedial first.", "error")
        return redirect(url_for("remedial.plan"))
    report = generate()
    placed = sum(r["scheduled"] for r in report)
    flash(f"Remedial timetable generated: {placed} session(s) across {len(report)} class(es).", "success")
    lines = [f"{r['stream']} — {line}" for r in report for line in r["skipped"]]
    for line in lines[:MAX_REPORT_LINES]:
        flash(line, "error")
    if len(lines) > MAX_REPORT_LINES:
        flash(f"…and {len(lines) - MAX_REPORT_LINES} more things that couldn't be placed.", "error")
    return redirect(url_for("remedial.view_page"))


@remedial_bp.route("/clear", methods=["POST"])
def clear():
    db.execute("DELETE FROM remedial_entries")
    flash("The remedial timetable was cleared.", "success")
    return redirect(url_for("remedial.view_page"))
