"""
lesson_attendance.py  —  Staff > Lesson Attendance

Records whether the teacher actually attended each timetabled lesson.

  * Pick a date. Every lesson on the timetable for that weekday is listed
    (time, class, subject, teacher). Mark each one:
        Attended · Late · Missed · Covered (another teacher took the class)
    For "Covered", choose who covered it. Add a note if needed.
  * Filter the day by class or by teacher.
  * If a teacher was marked Absent in Teacher Attendance for that date, their
    unrecorded lessons are pre-selected as Missed (you still press Save).
  * Report for any date range: per teacher lessons attended / late / missed /
    covered, lessons covered for others, and a lesson attendance %.

Lesson attendance % = (Attended + Late) / lessons recorded for that teacher.

The table is created on first use, so no change to database.py is needed.
Each record keeps a snapshot of the class, subject and time, so history stays
readable even after the timetable or day structure is changed.
"""

import csv
import io
from datetime import date, timedelta
from xml.sax.saxutils import escape

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db

lesson_attendance_bp = Blueprint("lesson_attendance", __name__, url_prefix="/lesson-attendance")

STATUSES = (("A", "Attended"), ("L", "Late"), ("M", "Missed"), ("C", "Covered"))
VALID = {s for s, _ in STATUSES}
_tables_ready = False


# --------------------------------------------------------------------------
def _ddl(pk):
    return f"""CREATE TABLE IF NOT EXISTS lesson_attendance (
        id {pk},
        att_date TEXT NOT NULL,
        stream_id INTEGER NOT NULL,
        period_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        teacher_id INTEGER,
        status TEXT NOT NULL,
        cover_teacher_id INTEGER,
        note TEXT,
        stream_label TEXT,
        subject_name TEXT,
        slot_label TEXT,
        UNIQUE(att_date, stream_id, period_id, subject_id)
    )"""


def apply(conn, use_pg):
    """Optional: call from database.init_db(). Tables also create themselves."""
    pk = "SERIAL PRIMARY KEY" if use_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cur = conn.cursor()
    cur.execute(_ddl(pk))
    conn.commit()


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


@lesson_attendance_bp.before_request
def _make_tables():
    ensure_tables()


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def _parse_date(raw, default):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return default


def _key(row):
    return f"{row['stream_id']}_{row['period_id']}_{row['subject_id']}"


def lessons_on(d, stream_id=None, teacher_id=None):
    """Every timetabled lesson on the weekday of date d, in time order."""
    full = d.strftime("%A")
    q = ("SELECT te.stream_id, te.period_id, te.subject_id, te.teacher_id, p.period_number, p.start_time, "
         "p.end_time, st.name AS stream_name, g.name AS grade_name, sub.name AS subject_name, "
         "t.name AS teacher_name FROM timetable_entries te "
         "JOIN periods p ON p.id = te.period_id JOIN streams st ON st.id = te.stream_id "
         "JOIN grades g ON g.id = st.grade_id JOIN subjects sub ON sub.id = te.subject_id "
         "LEFT JOIN teachers t ON t.id = te.teacher_id WHERE (p.day = ? OR p.day = ?)")
    params = [full, full[:3]]
    if stream_id:
        q += " AND te.stream_id = ?"
        params.append(stream_id)
    if teacher_id:
        q += " AND te.teacher_id = ?"
        params.append(teacher_id)
    q += " ORDER BY p.start_time, g.sort_order, st.name, sub.name"
    return [dict(r) for r in db.fetch_all(q, tuple(params))]


def _absent_teachers(d):
    try:
        return {r["teacher_id"] for r in db.fetch_all(
            "SELECT teacher_id FROM teacher_attendance WHERE att_date=? AND status='A'", (d.isoformat(),))}
    except Exception:
        return set()      # teacher attendance not set up yet


def day_register(d, stream_id=None, teacher_id=None):
    recorded = {}
    for r in db.fetch_all(
            "SELECT stream_id, period_id, subject_id, status, cover_teacher_id, note FROM lesson_attendance "
            "WHERE att_date=?", (d.isoformat(),)):
        recorded[_key(r)] = r
    absent = _absent_teachers(d)
    out = []
    for les in lessons_on(d, stream_id, teacher_id):
        k = _key(les)
        rec = recorded.get(k)
        les["key"] = k
        les["status"] = rec["status"] if rec else ""
        les["cover_id"] = rec["cover_teacher_id"] if rec else None
        les["note"] = (rec["note"] if rec else "") or ""
        les["suggest_missed"] = (not rec) and les["teacher_id"] in absent
        out.append(les)
    return out


def _teachers():
    return db.fetch_all(
        "SELECT id, name FROM teachers ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, name")


def teacher_counts(start, end):
    """{teacher_id: {'A':n,'L':n,'M':n,'C':n,'cover':n}} for lessons recorded in range."""
    out = {}
    for r in db.fetch_all(
            "SELECT teacher_id, status, COUNT(*) AS c FROM lesson_attendance "
            "WHERE att_date >= ? AND att_date <= ? AND teacher_id IS NOT NULL GROUP BY teacher_id, status",
            (start.isoformat(), end.isoformat())):
        out.setdefault(r["teacher_id"], {})[r["status"]] = r["c"]
    for r in db.fetch_all(
            "SELECT cover_teacher_id, COUNT(*) AS c FROM lesson_attendance "
            "WHERE status='C' AND cover_teacher_id IS NOT NULL AND att_date >= ? AND att_date <= ? "
            "GROUP BY cover_teacher_id", (start.isoformat(), end.isoformat())):
        out.setdefault(r["cover_teacher_id"], {})["cover"] = r["c"]
    return out


def summary(start, end):
    counts = teacher_counts(start, end)
    rows = []
    for t in _teachers():
        c = counts.get(t["id"], {})
        recorded = c.get("A", 0) + c.get("L", 0) + c.get("M", 0) + c.get("C", 0)
        pct = round(100 * (c.get("A", 0) + c.get("L", 0)) / recorded, 1) if recorded else None
        rows.append({"id": t["id"], "name": t["name"], "A": c.get("A", 0), "L": c.get("L", 0),
                     "M": c.get("M", 0), "C": c.get("C", 0), "cover": c.get("cover", 0),
                     "recorded": recorded, "pct": pct})
    return rows


def day_counts(d):
    """{teacher_id: (done, recorded)} for one date — used by Teacher Attendance."""
    out = {}
    for r in db.fetch_all(
            "SELECT teacher_id, status, COUNT(*) AS c FROM lesson_attendance "
            "WHERE att_date=? AND teacher_id IS NOT NULL GROUP BY teacher_id, status", (d.isoformat(),)):
        done, rec = out.get(r["teacher_id"], (0, 0))
        out[r["teacher_id"]] = (done + (r["c"] if r["status"] in ("A", "L") else 0), rec + r["c"])
    return out


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@lesson_attendance_bp.route("/")
def index():
    d = _parse_date(request.args.get("date"), date.today())
    stream_id = request.args.get("stream_id", type=int)
    teacher_id = request.args.get("teacher_id", type=int)
    lessons = day_register(d, stream_id, teacher_id)
    tally = {s: sum(1 for r in lessons if r["status"] == s) for s, _ in STATUSES}
    tally["unmarked"] = sum(1 for r in lessons if not r["status"])
    streams = db.fetch_all(
        "SELECT s.id, s.name, g.name AS grade_name FROM streams s JOIN grades g ON g.id = s.grade_id "
        "ORDER BY g.sort_order, g.id, s.name")
    keep = {k: v for k, v in (("stream_id", stream_id), ("teacher_id", teacher_id)) if v}
    return render_template(
        "lesson_attendance/index.html", d=d, lessons=lessons, statuses=STATUSES, tally=tally,
        streams=streams, teachers=_teachers(), stream_id=stream_id, teacher_id=teacher_id,
        prev_day=(d - timedelta(days=1)).isoformat(), next_day=(d + timedelta(days=1)).isoformat(),
        today=date.today().isoformat(), keep=keep, weekend=d.weekday() >= 5)


@lesson_attendance_bp.route("/save", methods=["POST"])
def save():
    d = _parse_date(request.form.get("date"), date.today())
    stream_id = request.form.get("stream_id", type=int)
    teacher_id = request.form.get("teacher_id", type=int)
    saved = 0
    for les in lessons_on(d, stream_id, teacher_id):
        k = _key(les)
        status = (request.form.get(f"st_{k}") or "").strip().upper()
        where = (d.isoformat(), les["stream_id"], les["period_id"], les["subject_id"])
        if status not in VALID:
            db.execute("DELETE FROM lesson_attendance WHERE att_date=? AND stream_id=? AND period_id=? "
                       "AND subject_id=?", where)
            continue
        cover = request.form.get(f"cv_{k}", type=int) if status == "C" else None
        note = (request.form.get(f"note_{k}") or "").strip()[:200] or None
        db.execute(
            "INSERT INTO lesson_attendance (att_date, stream_id, period_id, subject_id, teacher_id, status, "
            "cover_teacher_id, note, stream_label, subject_name, slot_label) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(att_date, stream_id, period_id, subject_id) DO UPDATE SET "
            "teacher_id=excluded.teacher_id, status=excluded.status, cover_teacher_id=excluded.cover_teacher_id, "
            "note=excluded.note, stream_label=excluded.stream_label, subject_name=excluded.subject_name, "
            "slot_label=excluded.slot_label",
            where + (les["teacher_id"], status, cover, note,
                     f"{les['grade_name']} {les['stream_name']}".strip(), les["subject_name"],
                     f"P{les['period_number']} {les['start_time']}-{les['end_time']}"))
        saved += 1
    flash(f"Lesson attendance saved for {d.strftime('%A %d %B %Y')}: {saved} lesson(s) recorded.", "success")
    keep = {k: v for k, v in (("stream_id", stream_id), ("teacher_id", teacher_id)) if v}
    return redirect(url_for("lesson_attendance.index", date=d.isoformat(), **keep))


@lesson_attendance_bp.route("/report")
def report():
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    if start > end:
        start, end = end, start
    rows = summary(start, end)
    return render_template("lesson_attendance/report.html", rows=rows, start=start, end=end,
                           any_recorded=any(r["recorded"] or r["cover"] for r in rows))


@lesson_attendance_bp.route("/teacher/<int:teacher_id>")
def teacher_history(teacher_id):
    t = db.fetch_one("SELECT id, name FROM teachers WHERE id=?", (teacher_id,))
    if not t:
        return redirect(url_for("lesson_attendance.report"))
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    rows = db.fetch_all(
        "SELECT la.att_date, la.status, la.note, la.stream_label, la.subject_name, la.slot_label, "
        "la.teacher_id, la.cover_teacher_id, pt.name AS planned_name, ct.name AS cover_name "
        "FROM lesson_attendance la LEFT JOIN teachers pt ON pt.id = la.teacher_id "
        "LEFT JOIN teachers ct ON ct.id = la.cover_teacher_id "
        "WHERE (la.teacher_id = ? OR la.cover_teacher_id = ?) AND la.att_date >= ? AND la.att_date <= ? "
        "ORDER BY la.att_date DESC, la.slot_label", (teacher_id, teacher_id, start.isoformat(), end.isoformat()))
    return render_template("lesson_attendance/teacher.html", teacher=t, rows=rows, start=start, end=end,
                           labels=dict(STATUSES))


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def _range():
    today = date.today()
    end = _parse_date(request.args.get("to"), today)
    start = _parse_date(request.args.get("from"), today.replace(day=1))
    return (end, start) if start > end else (start, end)


def _table(start, end):
    head = ["Teacher", "Attended", "Late", "Missed", "Covered by other", "Covered for others",
            "Lessons recorded", "Lesson attendance %"]
    rows = [[r["name"], r["A"], r["L"], r["M"], r["C"], r["cover"], r["recorded"],
             "" if r["pct"] is None else f"{r['pct']}%"] for r in summary(start, end)]
    return head, rows


@lesson_attendance_bp.route("/export.csv")
def export_csv():
    start, end = _range()
    head, rows = _table(start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename=Lesson_attendance_{start}_{end}.csv"})


@lesson_attendance_bp.route("/export.pdf")
def export_pdf():
    start, end = _range()
    head, rows = _table(start, end)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=9, leading=11)
    title = (f"{db.get_school_settings()['name']} — Lesson attendance, "
             f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}")
    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    story = branding.logo_story() + [Paragraph(escape(title), styles["Title"]), Spacer(1, 8)]
    data = [[Paragraph(f"<b>{escape(h)}</b>", small) for h in head]] + \
           [[Paragraph(escape(str(c)), small) for c in r] for r in rows]
    if len(data) == 1:
        story.append(Paragraph("No teachers on record.", styles["BodyText"]))
    else:
        first = 60 * mm
        rest = (page[0] - 24 * mm - first) / (len(head) - 1)
        t = Table(data, repeatRows=1, colWidths=[first] + [rest] * (len(head) - 1))
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3dcc9")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e3dcc9")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbf8f0")])]))
        story.append(t)
    doc.build(story)
    return Response(buf.getvalue(), mimetype="application/pdf", headers={
        "Content-Disposition": f"attachment;filename=Lesson_attendance_{start}_{end}.pdf"})
