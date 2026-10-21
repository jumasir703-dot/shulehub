"""
teacher_attendance.py  —  Staff > Teacher Attendance

Daily attendance register for teachers.

  * Pick a date, mark every teacher Present / Late / Absent / Excused (with an
    optional arrival time and note), and save. "Mark all present" does the
    common case in one click; only the exceptions need touching.
  * Each teacher's number of lessons that day is shown from the timetable, so
    an absent teacher's missed lessons are obvious (cover needed).
  * Report for any date range: per teacher counts, attendance %, lessons
    missed, with CSV and PDF export.

Attendance % = (Present + Late) / days marked. Excused days are shown but do
not count against the teacher.

The table is created on first use, so no change to database.py is needed.
"""

import csv
import io
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db

teacher_attendance_bp = Blueprint("teacher_attendance", __name__, url_prefix="/teacher-attendance")

STATUSES = (("P", "Present"), ("L", "Late"), ("A", "Absent"), ("E", "Excused"))
VALID = {s for s, _ in STATUSES}

_tables_ready = False


# --------------------------------------------------------------------------
def apply(conn, use_pg):
    """Optional: call from database.init_db(). The section also creates its own
    table on first use, so this is not required."""
    pk = "SERIAL PRIMARY KEY" if use_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cur = conn.cursor()
    cur.execute(_ddl(pk))
    conn.commit()


def _ddl(pk):
    return f"""CREATE TABLE IF NOT EXISTS teacher_attendance (
        id {pk},
        att_date TEXT NOT NULL,
        teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        time_in TEXT,
        note TEXT,
        UNIQUE(att_date, teacher_id)
    )"""


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    for pk in ("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"):
        try:
            db.execute(_ddl(pk))
            _tables_ready = True
            return
        except Exception:
            continue


@teacher_attendance_bp.before_request
def _make_tables():
    ensure_tables()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_date(raw, default):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return default


def _teachers():
    return db.fetch_all(
        "SELECT id, name FROM teachers ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, name")


def _lessons_on(d):
    """teacher_id -> number of timetable lessons on that weekday. The periods
    table may store 'Monday' or 'Mon', so both spellings are tried."""
    full = d.strftime("%A")
    out = {}
    try:
        for r in db.fetch_all(
                "SELECT te.teacher_id AS tid, COUNT(*) AS c FROM timetable_entries te "
                "JOIN periods p ON p.id = te.period_id "
                "WHERE te.teacher_id IS NOT NULL AND (p.day = ? OR p.day = ?) GROUP BY te.teacher_id",
                (full, full[:3])):
            out[r["tid"]] = r["c"]
    except Exception:
        pass
    return out


def day_register(d):
    """[{id, name, status, time_in, note, lessons}] for every teacher on date d."""
    marks = {r["teacher_id"]: r for r in db.fetch_all(
        "SELECT teacher_id, status, time_in, note FROM teacher_attendance WHERE att_date=?", (d.isoformat(),))}
    lessons = _lessons_on(d)
    done = _lesson_day_counts(d)
    out = []
    for t in _teachers():
        m = marks.get(t["id"])
        taken, recorded = done.get(t["id"], (0, 0))
        out.append({"id": t["id"], "name": t["name"],
                    "status": m["status"] if m else "", "time_in": (m["time_in"] if m else "") or "",
                    "note": (m["note"] if m else "") or "", "lessons": lessons.get(t["id"], 0),
                    "taken": taken, "recorded": recorded})
    return out


def _lesson_day_counts(d):
    """{teacher_id: (lessons taken, lessons recorded)} from Lesson Attendance;
    empty if that section has not been used yet."""
    try:
        import lesson_attendance
        lesson_attendance.ensure_tables()
        return lesson_attendance.day_counts(d)
    except Exception:
        return {}


def _lesson_range_counts(start, end):
    try:
        import lesson_attendance
        lesson_attendance.ensure_tables()
        return lesson_attendance.teacher_counts(start, end)
    except Exception:
        return {}


def summary(start, end):
    """Per-teacher totals over [start, end]: counts, days marked, attendance %."""
    rows = db.fetch_all(
        "SELECT teacher_id, status, COUNT(*) AS c FROM teacher_attendance "
        "WHERE att_date >= ? AND att_date <= ? GROUP BY teacher_id, status",
        (start.isoformat(), end.isoformat()))
    counts = {}
    for r in rows:
        counts.setdefault(r["teacher_id"], {})[r["status"]] = r["c"]
    lesson = _lesson_range_counts(start, end)
    out = []
    for t in _teachers():
        c = counts.get(t["id"], {})
        marked = sum(c.values())
        counted = marked - c.get("E", 0)
        pct = round(100 * (c.get("P", 0) + c.get("L", 0)) / counted, 1) if counted else None
        lc = lesson.get(t["id"], {})
        l_rec = lc.get("A", 0) + lc.get("L", 0) + lc.get("M", 0) + lc.get("C", 0)
        l_taken = lc.get("A", 0) + lc.get("L", 0)
        out.append({"id": t["id"], "name": t["name"], "P": c.get("P", 0), "L": c.get("L", 0),
                    "A": c.get("A", 0), "E": c.get("E", 0), "marked": marked, "pct": pct,
                    "l_taken": l_taken, "l_recorded": l_rec})
    return out


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@teacher_attendance_bp.route("/")
def index():
    d = _parse_date(request.args.get("date"), date.today())
    register = day_register(d)
    tally = {s: sum(1 for r in register if r["status"] == s) for s, _ in STATUSES}
    tally["unmarked"] = sum(1 for r in register if not r["status"])
    return render_template(
        "teacher_attendance/index.html", d=d, register=register, statuses=STATUSES, tally=tally,
        prev_day=(d - timedelta(days=1)).isoformat(), next_day=(d + timedelta(days=1)).isoformat(),
        today=date.today().isoformat(),
        missed=sum(r["lessons"] for r in register if r["status"] == "A"))


@teacher_attendance_bp.route("/save", methods=["POST"])
def save():
    d = _parse_date(request.form.get("date"), date.today())
    saved = 0
    for t in _teachers():
        tid = t["id"]
        status = (request.form.get(f"status_{tid}") or "").strip().upper()
        if status not in VALID:
            # left blank: clear any earlier mark for that teacher
            db.execute("DELETE FROM teacher_attendance WHERE att_date=? AND teacher_id=?", (d.isoformat(), tid))
            continue
        time_in = (request.form.get(f"time_{tid}") or "").strip() or None
        note = (request.form.get(f"note_{tid}") or "").strip()[:200] or None
        db.execute(
            "INSERT INTO teacher_attendance (att_date, teacher_id, status, time_in, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(att_date, teacher_id) DO UPDATE SET status=excluded.status, "
            "time_in=excluded.time_in, note=excluded.note",
            (d.isoformat(), tid, status, time_in, note))
        saved += 1
    flash(f"Attendance saved for {d.strftime('%A %d %B %Y')}: {saved} teacher(s) marked.", "success")
    return redirect(url_for("teacher_attendance.index", date=d.isoformat()))


@teacher_attendance_bp.route("/report")
def report():
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    if start > end:
        start, end = end, start
    rows = summary(start, end)
    return render_template("teacher_attendance/report.html", rows=rows, start=start, end=end,
                           statuses=STATUSES, any_marked=any(r["marked"] for r in rows))


@teacher_attendance_bp.route("/teacher/<int:teacher_id>")
def teacher_history(teacher_id):
    t = db.fetch_one("SELECT id, name FROM teachers WHERE id=?", (teacher_id,))
    if not t:
        return redirect(url_for("teacher_attendance.report"))
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    days = db.fetch_all(
        "SELECT att_date, status, time_in, note FROM teacher_attendance "
        "WHERE teacher_id=? AND att_date >= ? AND att_date <= ? ORDER BY att_date DESC",
        (teacher_id, start.isoformat(), end.isoformat()))
    return render_template("teacher_attendance/teacher.html", teacher=t, days=days, start=start, end=end,
                           labels=dict(STATUSES))


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def _table(start, end):
    head = ["Teacher", "Present", "Late", "Absent", "Excused", "Days marked", "Attendance %",
            "Lessons taken", "Lessons recorded"]
    rows = [[r["name"], r["P"], r["L"], r["A"], r["E"], r["marked"],
             "" if r["pct"] is None else f"{r['pct']}%", r["l_taken"], r["l_recorded"]]
            for r in summary(start, end)]
    return head, rows


@teacher_attendance_bp.route("/export.csv")
def export_csv():
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    head, rows = _table(start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename=Teacher_attendance_{start}_{end}.csv"})


@teacher_attendance_bp.route("/export.pdf")
def export_pdf():
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    head, rows = _table(start, end)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=9, leading=11)
    school = db.get_school_settings()["name"]
    title = f"{school} — Teacher attendance, {start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}"
    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    story = branding.logo_story() + [Paragraph(escape(title), styles["Title"]), Spacer(1, 8)]
    data = [head] + [[Paragraph(escape(str(c)), small) for c in r] for r in rows]
    if len(data) == 1:
        story.append(Paragraph("No teachers on record.", styles["BodyText"]))
    else:
        first = 70 * mm
        rest = (page[0] - 24 * mm - first) / (len(head) - 1)
        t = Table(data, repeatRows=1, colWidths=[first] + [rest] * (len(head) - 1))
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103524")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e3dcc9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbf8f0")])]))
        story.append(t)
    doc.build(story)
    return Response(buf.getvalue(), mimetype="application/pdf", headers={
        "Content-Disposition": f"attachment;filename=Teacher_attendance_{start}_{end}.pdf"})
