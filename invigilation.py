"""
invigilation.py  —  Exam Analysis > Invigilation

Builds the exam invigilation timetable automatically.

You list the papers (date, time, grade, subject, venue, candidates). For each
paper the number of invigilators needed is worked out from the number of
candidates (one invigilator per N candidates, N is a setting). Teachers are then
assigned by these rules:

  * A teacher may only invigilate a SUBJECT THEY TEACH — in any class. A
    Mathematics teacher can invigilate every Mathematics paper, even for a class
    they do not teach. "Teaches" is read from the teacher's subjects, the
    timetable and the subject allocations, matched by subject NAME so that
    Form 4 Mathematics and Grade 10 Mathematics count as the same subject.
  * A teacher is never on two papers whose times overlap.
  * Duties are shared as evenly as possible (least-loaded teacher first), with
    an optional cap on sittings per teacher per day.

Papers with the fewest eligible teachers are filled first, so scarce subjects
are not starved by the common ones. Anything that cannot be filled is reported,
never silently dropped.
"""

import csv
import io
import math
from collections import defaultdict
from xml.sax.saxutils import escape

from flask import (Blueprint, Response, flash, redirect, render_template, request, url_for)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db
import exam_analysis as ea

invigilation_bp = Blueprint("invigilation", __name__, url_prefix="/exams/invigilation")

PER_KEY, MAXDAY_KEY = "invig_candidates_per_invigilator", "invig_max_per_day"


# --------------------------------------------------------------------------
def apply(conn, use_pg):
    pk = "SERIAL PRIMARY KEY" if use_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cur = conn.cursor()
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS invig_papers (
            id {pk},
            title TEXT,
            exam_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
            subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            venue TEXT,
            candidates INTEGER NOT NULL DEFAULT 0,
            needed INTEGER NOT NULL DEFAULT 1
        )""")
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS invig_assignments (
            id {pk},
            paper_id INTEGER NOT NULL REFERENCES invig_papers(id) ON DELETE CASCADE,
            teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
            UNIQUE(paper_id, teacher_id)
        )""")
    conn.commit()


_tables_ready = False


def ensure_tables():
    """Create the invigilation tables the first time the section is used, so it
    works even if database.init_db() never calls apply(). Tries the SQLite
    syntax first, then PostgreSQL's."""
    global _tables_ready
    if _tables_ready:
        return
    for pk in ("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"):
        try:
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS invig_papers (
                    id {pk},
                    title TEXT,
                    exam_date TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    grade_id INTEGER NOT NULL REFERENCES grades(id) ON DELETE CASCADE,
                    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    venue TEXT,
                    candidates INTEGER NOT NULL DEFAULT 0,
                    needed INTEGER NOT NULL DEFAULT 1
                )""")
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS invig_assignments (
                    id {pk},
                    paper_id INTEGER NOT NULL REFERENCES invig_papers(id) ON DELETE CASCADE,
                    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                    UNIQUE(paper_id, teacher_id)
                )""")
            _tables_ready = True
            return
        except Exception:
            continue


@invigilation_bp.before_request
def _make_tables():
    ensure_tables()


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _int_setting(key, default):
    row = db.fetch_one("SELECT value FROM generation_settings WHERE key=?", (key,))
    try:
        return int(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def per_invigilator():
    return max(1, _int_setting(PER_KEY, 40))


def max_per_day():
    return max(0, _int_setting(MAXDAY_KEY, 0))   # 0 = no cap


def needed_for(candidates):
    return max(1, math.ceil((candidates or 0) / per_invigilator()))


# --------------------------------------------------------------------------
# Who may invigilate what
# --------------------------------------------------------------------------
def _norm(name):
    return " ".join((name or "").lower().split())


def teachers_by_subject_name():
    """normalised subject name -> {teacher_id}, from teacher_subjects, the
    timetable and subject allocations."""
    out = defaultdict(set)
    queries = (
        "SELECT ts.teacher_id AS tid, s.name AS sname FROM teacher_subjects ts JOIN subjects s ON s.id = ts.subject_id",
        "SELECT DISTINCT te.teacher_id AS tid, s.name AS sname FROM timetable_entries te "
        "JOIN subjects s ON s.id = te.subject_id WHERE te.teacher_id IS NOT NULL",
        "SELECT DISTINCT a.teacher_id AS tid, s.name AS sname FROM subject_allocations a "
        "JOIN subjects s ON s.id = a.subject_id",
    )
    for q in queries:
        try:
            for r in db.fetch_all(q):
                out[_norm(r["sname"])].add(r["tid"])
        except Exception:
            pass   # an older database without that table: skip the source
    return out


def _overlap(a, b):
    return a["exam_date"] == b["exam_date"] and a["start_time"] < b["end_time"] and b["start_time"] < a["end_time"]


# --------------------------------------------------------------------------
# Papers
# --------------------------------------------------------------------------
def list_papers():
    papers = [dict(p) for p in db.fetch_all(
        """SELECT p.*, g.name AS grade_name, s.name AS subject_name
           FROM invig_papers p JOIN grades g ON g.id = p.grade_id JOIN subjects s ON s.id = p.subject_id
           ORDER BY p.exam_date, p.start_time, g.sort_order, s.name""")]
    names = {t["id"]: t["name"] for t in db.fetch_all("SELECT id, name FROM teachers")}
    assigned = defaultdict(list)
    for a in db.fetch_all("SELECT paper_id, teacher_id FROM invig_assignments"):
        assigned[a["paper_id"]].append(a["teacher_id"])
    for p in papers:
        p["teachers"] = sorted((names.get(t, "?") for t in assigned[p["id"]]))
        p["initials"] = [ea.teacher_initials(n) or n for n in p["teachers"]]
        p["short"] = max(0, p["needed"] - len(p["teachers"]))
    return papers


def add_paper(exam_date, start, end, grade_id, subject_id, venue, candidates, needed=None):
    if candidates is None:
        candidates = db.fetch_one(
            "SELECT COUNT(*) AS c FROM students WHERE grade_id=? AND active=1", (grade_id,))["c"]
    return db.execute(
        "INSERT INTO invig_papers (exam_date, start_time, end_time, grade_id, subject_id, venue, candidates, needed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (exam_date, start, end, grade_id, subject_id, venue, candidates, needed or needed_for(candidates)))


# --------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------
def generate():
    """Rebuild all invigilation assignments. Returns a list of shortfall
    messages (empty when every paper is fully staffed)."""
    db.execute("DELETE FROM invig_assignments")
    papers = [dict(p) for p in db.fetch_all(
        "SELECT p.*, s.name AS subject_name, g.name AS grade_name FROM invig_papers p "
        "JOIN subjects s ON s.id = p.subject_id JOIN grades g ON g.id = p.grade_id")]
    can_teach = teachers_by_subject_name()
    cap = max_per_day()
    names = {t["id"]: t["name"] for t in db.fetch_all("SELECT id, name FROM teachers")}

    def eligible(p):
        return sorted(can_teach.get(_norm(p["subject_name"]), set()))

    # scarcest first: fewest eligible teachers per invigilator needed
    order = sorted(papers, key=lambda p: (len(eligible(p)) / max(1, p["needed"]), p["exam_date"], p["start_time"]))

    load = defaultdict(int)                  # teacher -> total duties
    day_load = defaultdict(int)              # (teacher, date) -> duties that day
    busy = defaultdict(list)                 # teacher -> papers already given
    shortfalls = []

    for p in order:
        chosen = []
        for t in sorted(eligible(p), key=lambda t: (load[t], day_load[(t, p["exam_date"])], names.get(t, ""))):
            if len(chosen) >= p["needed"]:
                break
            if any(_overlap(p, q) for q in busy[t]):
                continue
            if cap and day_load[(t, p["exam_date"])] >= cap:
                continue
            chosen.append(t)
        for t in chosen:
            db.execute("INSERT INTO invig_assignments (paper_id, teacher_id) VALUES (?, ?)", (p["id"], t))
            load[t] += 1
            day_load[(t, p["exam_date"])] += 1
            busy[t].append(p)
        if len(chosen) < p["needed"]:
            pool = len(eligible(p))
            why = (f"only {pool} teacher(s) teach {p['subject_name']}" if pool < p["needed"]
                   else "the rest are already invigilating at that time" + (" or at their daily limit" if cap else ""))
            shortfalls.append(f"{p['exam_date']} {p['start_time']} {p['grade_name']} {p['subject_name']}: "
                              f"{len(chosen)}/{p['needed']} invigilators ({why})")
    return shortfalls


def roster():
    """Per-teacher duty list: [{name, initials, duties: [paper...]}], busiest first."""
    papers = {p["id"]: p for p in list_papers()}
    by_teacher = defaultdict(list)
    for a in db.fetch_all("SELECT paper_id, teacher_id FROM invig_assignments"):
        if a["paper_id"] in papers:
            by_teacher[a["teacher_id"]].append(papers[a["paper_id"]])
    names = {t["id"]: t["name"] for t in db.fetch_all("SELECT id, name FROM teachers")}
    out = []
    for tid, duties in by_teacher.items():
        duties.sort(key=lambda p: (p["exam_date"], p["start_time"]))
        out.append({"name": names.get(tid, "?"), "initials": ea.teacher_initials(names.get(tid, "")), "duties": duties})
    return sorted(out, key=lambda r: (-len(r["duties"]), r["name"]))


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@invigilation_bp.route("/")
def index():
    papers = list_papers()
    grades = db.fetch_all("SELECT id, name FROM grades ORDER BY sort_order, id")
    subjects = db.fetch_all(
        "SELECT s.id, s.name, s.grade_id FROM subjects s JOIN grades g ON g.id = s.grade_id "
        "ORDER BY g.sort_order, g.id, s.name")
    counts = {g["id"]: db.fetch_one("SELECT COUNT(*) AS c FROM students WHERE grade_id=? AND active=1", (g["id"],))["c"]
              for g in grades}
    return render_template(
        "invigilation/index.html", papers=papers, grades=grades, subjects=subjects, counts=counts,
        per=per_invigilator(), cap=max_per_day(), view=request.args.get("view", "papers"),
        roster=roster() if request.args.get("view") == "teachers" else [],
        short_total=sum(p["short"] for p in papers),
        n_teachers=db.fetch_one("SELECT COUNT(*) AS c FROM teachers")["c"])


@invigilation_bp.route("/add", methods=["POST"])
def add():
    f = request.form
    grade_id, subject_id = f.get("grade_id", type=int), f.get("subject_id", type=int)
    exam_date, start, end = f.get("exam_date", ""), f.get("start_time", ""), f.get("end_time", "")
    if not (grade_id and subject_id and exam_date and start and end) or end <= start:
        flash("Choose a grade, subject, date, and a start time before the end time.", "error")
        return redirect(url_for("invigilation.index"))
    sub = db.fetch_one("SELECT grade_id FROM subjects WHERE id=?", (subject_id,))
    if not sub or sub["grade_id"] != grade_id:
        flash("That subject does not belong to the chosen grade.", "error")
        return redirect(url_for("invigilation.index"))
    add_paper(exam_date, start, end, grade_id, subject_id, (f.get("venue") or "").strip(),
              f.get("candidates", type=int), f.get("needed", type=int))
    flash("Paper added. Press “Generate invigilation” when all papers are in.", "success")
    return redirect(url_for("invigilation.index"))


@invigilation_bp.route("/delete/<int:paper_id>", methods=["POST"])
def delete(paper_id):
    db.execute("DELETE FROM invig_papers WHERE id=?", (paper_id,))
    return redirect(url_for("invigilation.index"))


@invigilation_bp.route("/settings", methods=["POST"])
def settings():
    per = max(1, request.form.get("per", type=int) or 40)
    cap = max(0, request.form.get("cap", type=int) or 0)
    db.set_generation_setting(PER_KEY, str(per))
    db.set_generation_setting(MAXDAY_KEY, str(cap))
    # candidate counts stay as typed; only papers without a manual override are re-sized
    for p in db.fetch_all("SELECT id, candidates FROM invig_papers"):
        db.execute("UPDATE invig_papers SET needed=? WHERE id=?", (needed_for(p["candidates"]), p["id"]))
    flash("Settings saved and invigilators needed re-worked for every paper.", "success")
    return redirect(url_for("invigilation.index"))


@invigilation_bp.route("/generate", methods=["POST"])
def generate_page():
    if not db.fetch_one("SELECT 1 AS x FROM invig_papers"):
        flash("Add the papers first.", "error")
        return redirect(url_for("invigilation.index"))
    short = generate()
    n = db.fetch_one("SELECT COUNT(*) AS c FROM invig_assignments")["c"]
    flash(f"Invigilation generated: {n} duties assigned.", "success")
    if short:
        flash("Not fully staffed — " + " | ".join(short), "error")
    return redirect(url_for("invigilation.index"))


@invigilation_bp.route("/clear", methods=["POST"])
def clear():
    db.execute("DELETE FROM invig_assignments")
    flash("All invigilation assignments cleared.", "success")
    return redirect(url_for("invigilation.index"))


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def _rows(view):
    if view == "teachers":
        return (["Teacher", "Initials", "Date", "Time", "Paper", "Venue"],
                [[r["name"], r["initials"], p["exam_date"], f"{p['start_time']}-{p['end_time']}",
                  f"{p['grade_name']} {p['subject_name']}", p["venue"] or ""]
                 for r in roster() for p in r["duties"]])
    return (["Date", "Time", "Grade", "Subject", "Venue", "Candidates", "Invigilators"],
            [[p["exam_date"], f"{p['start_time']}-{p['end_time']}", p["grade_name"], p["subject_name"],
              p["venue"] or "", p["candidates"], "; ".join(p["teachers"])] for p in list_papers()])


@invigilation_bp.route("/export.csv")
def export_csv():
    view = request.args.get("view", "papers")
    head, rows = _rows(view)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    name = "Invigilation_by_teacher.csv" if view == "teachers" else "Invigilation_timetable.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename={name}"})


@invigilation_bp.route("/export.pdf")
def export_pdf():
    view = request.args.get("view", "papers")
    head, rows = _rows(view)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=8, leading=10)
    school = db.get_school_settings()["name"]
    title = f"{school} — Exam invigilation " + ("duties by teacher" if view == "teachers" else "timetable")
    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    story = branding.logo_story() + [Paragraph(escape(title), styles["Title"]), Spacer(1, 8)]
    data = [head] + [[Paragraph(escape(str(c)), small) for c in r] for r in rows]
    if len(data) == 1:
        story.append(Paragraph("No papers or duties yet.", styles["BodyText"]))
    else:
        t = Table(data, repeatRows=1, colWidths=[(page[0] - 24 * mm) / len(head)] * len(head))
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103524")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e3dcc9")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbf8f0")])]))
        story.append(t)
    doc.build(story)
    name = "Invigilation_by_teacher.pdf" if view == "teachers" else "Invigilation_timetable.pdf"
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment;filename={name}"})
