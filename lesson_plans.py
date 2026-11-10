"""
lesson_plans.py - online lesson plans, marked by the Director of Studies (DOS).

Teachers write a lesson plan, save it as a draft and submit it. The DOS (anyone whose role has
"Mark lesson plans") sees every submitted plan, gives a score and a comment, and either approves it
or returns it for revision. The teacher edits and resubmits. Every marking is kept in the plan's
history. Plans download as a PDF (one plan, or all plans in the current list) and the list as a CSV.

Teachers only ever see their own plans; markers see everyone's.
Statuses: draft -> submitted -> approved | revise (returned) -> submitted ...
"""
import csv
import io
from datetime import date, datetime
from xml.sax.saxutils import escape

from flask import (Blueprint, Response, flash, g, redirect, render_template, request, url_for)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db
from analytics import _csv_safe, _safe_filename
from auth import has_service

lp_bp = Blueprint("lp", __name__, url_prefix="/lesson-plans")

MAX_SCORE = 10
MAX_PDF_PLANS = 200
STATUS = {"draft": "Draft", "submitted": "Awaiting marking", "approved": "Approved", "revise": "Returned for revision"}
SECTIONS = [
    ("objectives", "Learning objectives", "By the end of the lesson, learners should be able to ..."),
    ("prior_knowledge", "Prior knowledge", "What learners already know that this lesson builds on."),
    ("introduction", "Introduction", "How the lesson starts (review, hook, questions)."),
    ("development", "Lesson development and activities", "Step by step: teacher activities and learner activities."),
    ("conclusion", "Conclusion", "Summary, key points, checking understanding."),
    ("resources", "Resources and learning materials", "Textbooks, charts, equipment ..."),
    ("assessment", "Assessment", "How you will check that learning happened."),
    ("homework", "Homework or extension", ""),
    ("reflection", "Teacher's reflection (after the lesson)", "What worked, what to change next time."),
]
REQUIRED_TO_SUBMIT = ("objectives", "development")

_DDL = """
CREATE TABLE IF NOT EXISTS lp_plans (
    id {PK},
    owner TEXT NOT NULL,
    teacher_name TEXT,
    subject TEXT,
    class_label TEXT,
    grade_id INTEGER,
    stream_id INTEGER,
    plan_date TEXT,
    term INTEGER,
    topic TEXT NOT NULL,
    subtopic TEXT,
    objectives TEXT, prior_knowledge TEXT, introduction TEXT, development TEXT, conclusion TEXT,
    resources TEXT, assessment TEXT, homework TEXT, reflection TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    submitted_at TEXT,
    score INTEGER,
    max_score INTEGER NOT NULL DEFAULT 10,
    reviewed_by TEXT, reviewed_at TEXT, feedback TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS lp_reviews (
    id {PK},
    plan_id INTEGER NOT NULL REFERENCES lp_plans(id) ON DELETE CASCADE,
    reviewer TEXT, reviewed_at TEXT, score INTEGER, max_score INTEGER, outcome TEXT, comment TEXT
);
CREATE INDEX IF NOT EXISTS ix_lp_plans_owner ON lp_plans(owner, status);
CREATE INDEX IF NOT EXISTS ix_lp_plans_date ON lp_plans(plan_date);
"""
_ready = False


def ensure_schema():
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
    _ready = True


@lp_bp.before_request
def _before():
    ensure_schema()


# ------------------------------------------------------------------ helpers
def _me():
    return g.user["username"]


def _my_name():
    return g.user["full_name"] or g.user["username"]


def _can_mark():
    return has_service(g.user, "lesson_plans_mark")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _school():
    try:
        return db.get_school_settings()["name"] or "School"
    except Exception:
        return "School"


def _plan(plan_id):
    """The plan, or None when it does not exist or the person may not see it."""
    p = db.fetch_one("SELECT * FROM lp_plans WHERE id = ?", (plan_id,))
    if not p or p["owner"] == _me():
        return p
    # someone else's plan: only markers, and never an unfinished draft
    return p if _can_mark() and p["status"] != "draft" else None


def _editable(p):
    return p["owner"] == _me() and p["status"] in ("draft", "revise")


def _class_options():
    rows = db.fetch_all(
        "SELECT st.grade_id AS gid, st.stream_id AS sid, g.name AS gname, sm.name AS smname "
        "FROM students st LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "WHERE st.active = 1 GROUP BY st.grade_id, st.stream_id, g.name, sm.name ORDER BY g.name, sm.name")
    return [{"value": f"{r['gid'] or 0}:{r['sid'] or 0}", "label": r["smname"] or r["gname"] or "Class",
             "gid": r["gid"], "sid": r["sid"]} for r in rows if r["gname"] or r["smname"]]


def _subjects():
    try:
        return [r["name"] for r in db.fetch_all("SELECT name FROM subjects ORDER BY name")]
    except Exception:
        return []


def _filters():
    a = request.args
    return {"status": a.get("status", ""), "teacher": a.get("teacher", ""), "q": a.get("q", "").strip(),
            "start": a.get("from", ""), "end": a.get("to", "")}


def _query(f, limit=1000):
    where, params = ["1=1"], []
    if not _can_mark():
        where.append("owner = ?"); params.append(_me())
    else:
        where.append("(status <> 'draft' OR owner = ?)"); params.append(_me())
        if f["teacher"]:
            where.append("owner = ?"); params.append(f["teacher"])
    if f["status"] in STATUS:
        where.append("status = ?"); params.append(f["status"])
    if f["q"]:
        where.append("(LOWER(topic) LIKE ? OR LOWER(COALESCE(subject,'')) LIKE ? OR LOWER(COALESCE(class_label,'')) LIKE ?)")
        params += ["%" + f["q"].lower() + "%"] * 3
    if f["start"]:
        where.append("plan_date >= ?"); params.append(f["start"])
    if f["end"]:
        where.append("plan_date <= ?"); params.append(f["end"])
    return db.fetch_all("SELECT * FROM lp_plans WHERE " + " AND ".join(where) +
                        " ORDER BY plan_date DESC, id DESC LIMIT " + str(int(limit)), tuple(params))


# ------------------------------------------------------------------ pages
@lp_bp.route("/")
def index():
    f = _filters()
    plans = _query(f, 500)
    counts = {k: 0 for k in STATUS}
    if _can_mark():
        for r in db.fetch_all("SELECT status, COUNT(*) AS n FROM lp_plans WHERE status <> 'draft' OR owner = ? GROUP BY status", (_me(),)):
            counts[r["status"]] = r["n"]
    else:
        for r in db.fetch_all("SELECT status, COUNT(*) AS n FROM lp_plans WHERE owner = ? GROUP BY status", (_me(),)):
            counts[r["status"]] = r["n"]
    teachers = db.fetch_all("SELECT owner, MAX(teacher_name) AS name FROM lp_plans WHERE status <> 'draft' OR owner = ? "
                            "GROUP BY owner ORDER BY name", (_me(),)) if _can_mark() else []
    return render_template("lessonplans/index.html", plans=plans, f=f, counts=counts, status=STATUS, teachers=teachers,
                           can_mark=_can_mark(), me=_me())


@lp_bp.route("/new")
def new():
    blank = {k: "" for k, _, _ in SECTIONS}
    blank.update({"id": None, "topic": "", "subtopic": "", "subject": "", "class_label": "", "term": "",
                  "plan_date": date.today().isoformat(), "status": "draft", "grade_id": None, "stream_id": None})
    return render_template("lessonplans/form.html", p=blank, sections=SECTIONS, classes=_class_options(), subjects=_subjects())


@lp_bp.route("/<int:plan_id>/edit")
def edit(plan_id):
    p = _plan(plan_id)
    if not p or not _editable(p):
        flash("That plan can't be edited now. Only your own drafts and returned plans can be changed.", "error")
        return redirect(url_for("lp.view", plan_id=plan_id) if p else url_for("lp.index"))
    return render_template("lessonplans/form.html", p=p, sections=SECTIONS, classes=_class_options(), subjects=_subjects())


@lp_bp.route("/save", methods=["POST"])
def save():
    f = request.form
    plan_id = f.get("id", type=int)
    existing = None
    if plan_id:
        existing = _plan(plan_id)
        if not existing or not _editable(existing):
            flash("That plan can't be edited now.", "error")
            return redirect(url_for("lp.index"))
    topic = (f.get("topic") or "").strip()[:200]
    try:
        plan_date = datetime.strptime(f.get("plan_date", ""), "%Y-%m-%d").date().isoformat()
    except ValueError:
        plan_date = ""
    texts = {k: (f.get(k) or "").strip()[:6000] for k, _, _ in SECTIONS}
    chosen = next((c for c in _class_options() if c["value"] == f.get("class_pick")), None)
    class_label = (f.get("class_other") or "").strip()[:60] or (chosen["label"] if chosen else "")
    submit = f.get("action") == "submit"
    problems = []
    if not topic:
        problems.append("Give the lesson a topic.")
    if not plan_date:
        problems.append("Choose the lesson date.")
    if submit:
        problems += [f"Fill in “{label}” before submitting." for k, label, _ in SECTIONS if k in REQUIRED_TO_SUBMIT and not texts[k]]
        if not class_label:
            problems.append("Choose the class.")
    if problems:
        for m in problems:
            flash(m, "error")
        draft = dict(existing) if existing else {"id": None, "status": "draft"}
        draft.update(texts)
        draft.update({"id": plan_id, "topic": topic, "subtopic": f.get("subtopic", ""), "subject": f.get("subject", ""),
                      "class_label": class_label, "term": f.get("term", ""), "plan_date": plan_date or f.get("plan_date", ""),
                      "grade_id": None, "stream_id": None})
        return render_template("lessonplans/form.html", p=draft, sections=SECTIONS, classes=_class_options(), subjects=_subjects())
    term = f.get("term", type=int) if f.get("term", type=int) in (1, 2, 3) else None
    cols = dict(topic=topic, subtopic=(f.get("subtopic") or "").strip()[:200], subject=(f.get("subject") or "").strip()[:80],
                class_label=class_label, grade_id=chosen["gid"] if chosen else None, stream_id=chosen["sid"] if chosen else None,
                plan_date=plan_date, term=term, updated_at=_now(), **texts)
    if existing:
        sets = ", ".join(f"{k} = ?" for k in cols)
        db.execute(f"UPDATE lp_plans SET {sets} WHERE id = ?", tuple(cols.values()) + (plan_id,))
    else:
        cols.update(owner=_me(), teacher_name=_my_name(), status="draft", created_at=_now(), max_score=MAX_SCORE)
        plan_id = db.execute(f"INSERT INTO lp_plans ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
    if submit:
        db.execute("UPDATE lp_plans SET status = 'submitted', submitted_at = ?, score = NULL WHERE id = ?", (_now(), plan_id))
        flash("Submitted. The DOS will mark it.", "success")
        return redirect(url_for("lp.view", plan_id=plan_id))
    flash("Draft saved.", "success")
    return redirect(url_for("lp.edit", plan_id=plan_id))


@lp_bp.route("/<int:plan_id>")
def view(plan_id):
    p = _plan(plan_id)
    if not p:
        flash("That lesson plan was not found.", "error")
        return redirect(url_for("lp.index"))
    reviews = db.fetch_all("SELECT * FROM lp_reviews WHERE plan_id = ? ORDER BY id DESC", (plan_id,))
    return render_template("lessonplans/view.html", p=p, sections=SECTIONS, reviews=reviews, status=STATUS,
                           can_mark=_can_mark(), editable=_editable(p), mine=p["owner"] == _me(), max_score=p["max_score"] or MAX_SCORE)


@lp_bp.route("/<int:plan_id>/submit", methods=["POST"])
def submit(plan_id):
    p = _plan(plan_id)
    if not p or not _editable(p):
        flash("That plan can't be submitted.", "error")
    elif any(not (p[k] or "").strip() for k in REQUIRED_TO_SUBMIT) or not p["class_label"]:
        flash("Open the plan and fill in the objectives, the development and the class first.", "error")
        return redirect(url_for("lp.edit", plan_id=plan_id))
    else:
        db.execute("UPDATE lp_plans SET status = 'submitted', submitted_at = ?, score = NULL WHERE id = ?", (_now(), plan_id))
        flash("Submitted. The DOS will mark it.", "success")
    return redirect(url_for("lp.view", plan_id=plan_id))


@lp_bp.route("/<int:plan_id>/delete", methods=["POST"])
def delete(plan_id):
    p = _plan(plan_id)
    if not p or p["owner"] != _me() or p["status"] != "draft":
        flash("Only your own drafts can be deleted.", "error")
    else:
        db.execute("DELETE FROM lp_plans WHERE id = ?", (plan_id,))
        flash("Draft deleted.", "success")
    return redirect(url_for("lp.index"))


@lp_bp.route("/<int:plan_id>/copy", methods=["POST"])
def copy(plan_id):
    p = _plan(plan_id)
    if not p:
        flash("That lesson plan was not found.", "error")
        return redirect(url_for("lp.index"))
    cols = ["subject", "class_label", "grade_id", "stream_id", "term", "topic", "subtopic"] + [k for k, _, _ in SECTIONS if k != "reflection"]
    vals = [p[c] for c in cols]
    new_id = db.execute(
        f"INSERT INTO lp_plans (owner, teacher_name, plan_date, status, max_score, created_at, updated_at, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, 'draft', ?, ?, ?, {', '.join('?' * len(cols))})",
        tuple([_me(), _my_name(), date.today().isoformat(), MAX_SCORE, _now(), _now()] + vals))
    flash("Copied as a new draft. Change the date and details, then submit.", "success")
    return redirect(url_for("lp.edit", plan_id=new_id))


@lp_bp.route("/<int:plan_id>/review", methods=["POST"])
def review(plan_id):
    p = _plan(plan_id)
    if not _can_mark():                       # permissions.py already blocks this; checked again here on purpose
        flash("Only the DOS can mark lesson plans.", "error")
        return redirect(url_for("lp.view", plan_id=plan_id) if p else url_for("lp.index"))
    if not p or p["status"] == "draft":
        flash("Only submitted plans can be marked.", "error")
        return redirect(url_for("lp.index"))
    outcome = request.form.get("outcome")
    comment = (request.form.get("comment") or "").strip()[:2000]
    raw = (request.form.get("score") or "").strip()
    cap = p["max_score"] or MAX_SCORE
    score = None
    if raw:
        try:
            score = int(float(raw))
            if not 0 <= score <= cap:
                raise ValueError
        except ValueError:
            flash(f"The score must be a whole number from 0 to {cap}.", "error")
            return redirect(url_for("lp.view", plan_id=plan_id))
    if outcome not in ("approved", "revise"):
        flash("Choose Approve or Return for revision.", "error")
    elif outcome == "revise" and not comment:
        flash("Say what the teacher should change when returning a plan.", "error")
    else:
        db.execute("UPDATE lp_plans SET status = ?, score = ?, reviewed_by = ?, reviewed_at = ?, feedback = ? WHERE id = ?",
                   (outcome, score, _my_name(), _now(), comment or None, plan_id))
        db.execute("INSERT INTO lp_reviews (plan_id, reviewer, reviewed_at, score, max_score, outcome, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (plan_id, _my_name(), _now(), score, cap, outcome, comment or None))
        flash("Approved." if outcome == "approved" else "Returned to the teacher for revision.", "success")
    return redirect(url_for("lp.view", plan_id=plan_id))


# ------------------------------------------------------------------ downloads
def _styles():
    ss = getSampleStyleSheet()
    return {"title": ParagraphStyle("t", parent=ss["Title"], fontSize=16, leading=19, spaceAfter=2),
            "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=10, textColor=colors.HexColor("#555555"), alignment=1),
            "h": ParagraphStyle("h", parent=ss["Normal"], fontSize=10.5, leading=13, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=2),
            "body": ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=12.5),
            "small": ParagraphStyle("sm", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#666666"))}


def _brand():
    try:
        import school_theme
        return colors.HexColor(school_theme.load()["primary"])
    except Exception:
        return colors.HexColor("#1f2937")


def _para(text, style):
    return Paragraph(escape(text or "—").replace("\n", "<br/>"), style)


def _plan_story(p, ss):
    brand = _brand()
    story = branding.logo_story() + [Paragraph(escape(_school()), ss["title"]), Paragraph("LESSON PLAN", ss["sub"]), Spacer(1, 8)]
    rows = [["Teacher", p["teacher_name"] or p["owner"], "Date", p["plan_date"] or ""],
            ["Subject", p["subject"] or "", "Class", p["class_label"] or ""],
            ["Topic", p["topic"], "Term", ("Term %s" % p["term"]) if p["term"] else ""]]
    if p["subtopic"]:
        rows.append(["Sub-topic", p["subtopic"], "", ""])
    data = [[Paragraph(f"<b>{escape(a)}</b>", ss["body"]), _para(b, ss["body"]), Paragraph(f"<b>{escape(c)}</b>", ss["body"]), _para(d, ss["body"])]
            for a, b, c, d in rows]
    t = Table(data, colWidths=[24 * mm, 66 * mm, 22 * mm, 62 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, brand), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(t)
    for key, label, _ in SECTIONS:
        if (p[key] or "").strip():
            story += [Paragraph(escape(label), ss["h"]), _para(p[key], ss["body"])]
    story.append(Spacer(1, 12))
    status = STATUS.get(p["status"], p["status"])
    review = [[Paragraph("<b>Status</b>", ss["body"]), Paragraph(escape(status), ss["body"])]]
    if p["reviewed_at"] and p["status"] in ("approved", "revise"):
        if p["score"] is not None:
            review.append([Paragraph("<b>Score</b>", ss["body"]), Paragraph(f"{p['score']} / {p['max_score'] or MAX_SCORE}", ss["body"])])
        review.append([Paragraph("<b>Marked by</b>", ss["body"]), Paragraph(escape(f"{p['reviewed_by'] or ''}, {p['reviewed_at']}"), ss["body"])])
        if p["feedback"]:
            review.append([Paragraph("<b>DOS comment</b>", ss["body"]), _para(p["feedback"], ss["body"])])
    t = Table(review, colWidths=[30 * mm, 144 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, brand), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6"))]))
    story += [t, Spacer(1, 18), Paragraph("Teacher: ______________________ &nbsp;&nbsp;&nbsp; DOS: ______________________", ss["body"])]
    return story


def _pdf(plans, filename):
    buf, ss = io.BytesIO(), _styles()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm, title="Lesson plans")
    story = []
    for n, p in enumerate(plans):
        if n:
            story.append(PageBreak())
        story += _plan_story(p, ss)
    doc.build(story)
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment;filename={_safe_filename(filename, 'pdf')}"})


@lp_bp.route("/<int:plan_id>/pdf")
def pdf(plan_id):
    p = _plan(plan_id)
    if not p:
        return "Lesson plan not found.", 404
    return _pdf([p], f"Lesson plan {p['topic']}")


@lp_bp.route("/all.pdf")
def all_pdf():
    f = _filters()
    plans = _query(f, MAX_PDF_PLANS + 1)
    if not plans:
        flash("There are no lesson plans to download for that selection.", "error")
        return redirect(url_for("lp.index"))
    if len(plans) > MAX_PDF_PLANS:
        flash(f"That is more than {MAX_PDF_PLANS} plans. Narrow the dates or the teacher, then download again.", "error")
        return redirect(url_for("lp.index", **{k: v for k, v in request.args.items()}))
    return _pdf(plans, "Lesson plans")


@lp_bp.route("/list.csv")
def list_csv():
    rows = _query(_filters(), 5000)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "teacher", "subject", "class", "term", "topic", "status", "submitted", "score", "out_of", "marked_by", "marked_on", "dos_comment"])
    for p in rows:
        w.writerow([_csv_safe(x) for x in (p["plan_date"], p["teacher_name"] or p["owner"], p["subject"] or "", p["class_label"] or "",
                                           p["term"] or "", p["topic"], STATUS.get(p["status"], p["status"]), p["submitted_at"] or "",
                                           "" if p["score"] is None else p["score"], p["max_score"] or MAX_SCORE,
                                           p["reviewed_by"] or "", p["reviewed_at"] or "", p["feedback"] or "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=lesson_plans_{date.today().isoformat()}.csv"})
