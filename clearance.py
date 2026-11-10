"""
clearance.py - automatic student clearance forms.

A student is cleared when EVERY department has cleared them:
  * Accounts  (automatic): the fee balance is paid (or within the allowed amount)
  * Library   (automatic): no book still held and no unpaid fine (only if the Library module is on)
  * Other departments (signed off here): Boarding, Laboratory, Games, Class teacher, Principal ...
    The administrator chooses these departments in Clearance > Settings.

Pages: a class list showing who is cleared and who is not and why, a page per student where
departments sign off, a printable PDF form per student (or one page per student for a whole
class), and a CSV of the status. Everything is worked out live from the fees and library records,
so a form printed today is always accurate.
"""
import csv
import io
from datetime import date
from xml.sax.saxutils import escape

from flask import (Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db
from analytics import _csv_safe, _safe_filename

clearance_bp = Blueprint("clearance", __name__, url_prefix="/clearance")

_no_id = getattr(db, "TABLES_WITHOUT_ID", None)
if _no_id is not None:
    _no_id.add("clr_settings")

PURPOSES = ["End of term", "End of year", "Leaving the school", "Transfer to another school", "Graduation"]
MAX_FORMS = 400
DEFAULT_DEPARTMENTS = ["Boarding / Dormitory", "Laboratory", "Games & Sports", "Class teacher", "Principal"]

_DDL = """
CREATE TABLE IF NOT EXISTS clr_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS clr_departments (
    id {PK}, name TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS clr_signoffs (
    id {PK},
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    dept_id INTEGER NOT NULL REFERENCES clr_departments(id) ON DELETE CASCADE,
    signed_by TEXT, signed_on TEXT, note TEXT,
    UNIQUE (student_id, dept_id)
);
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
    if not _get("seeded"):                       # first run: start with the usual departments
        for i, name in enumerate(DEFAULT_DEPARTMENTS, 1):
            db.execute("INSERT INTO clr_departments (name, sort_order) VALUES (?, ?)", (name, i))
        _put("seeded", "1")


@clearance_bp.before_request
def _before():
    ensure_schema()


def _get(key, default=""):
    row = db.fetch_one("SELECT value FROM clr_settings WHERE key = ?", (key,))
    return row["value"] if row and row["value"] is not None else default


def _put(key, value):
    db.execute("INSERT INTO clr_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (key, str(value)))


def _school():
    try:
        return db.get_school_settings()["name"] or "School"
    except Exception:
        return "School"


def _who():
    return (g.user["full_name"] or g.user["username"]) if g.user else ""


def _kes(v):
    return f"{float(v or 0):,.0f}"


def _library_on():
    return "library" in current_app.blueprints


def _fee_allow():
    try:
        return max(float(_get("fee_allow", "0") or 0), 0.0)
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ who / what
def _class_options():
    rows = db.fetch_all(
        "SELECT st.grade_id AS gid, st.stream_id AS sid, g.name AS gname, sm.name AS smname "
        "FROM students st LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "GROUP BY st.grade_id, st.stream_id, g.name, sm.name ORDER BY g.name, sm.name")
    opts, seen = [], set()
    for r in rows:
        if r["gid"] is not None and r["gid"] not in seen:
            seen.add(r["gid"])
            opts.append(("grade:%s" % r["gid"], "All of %s" % r["gname"], "A whole grade"))
        if r["sid"]:
            opts.append(("class:%s:%s" % (r["gid"] or 0, r["sid"]), r["smname"] or r["gname"] or "Class", "One class"))
    return opts


def _where(audience, status, q):
    where, params = ["1=1"], []
    kind, _, rest = (audience or "").partition(":")
    if kind == "grade" and rest.isdigit():
        where.append("st.grade_id = ?"); params.append(int(rest))
    elif kind == "class" and rest.split(":")[-1].isdigit():
        where.append("st.stream_id = ?"); params.append(int(rest.split(":")[-1]))
    if status and status != "all":
        where.append("st.status = ?"); params.append(status)
    if q:
        where.append("(LOWER(st.name) LIKE ? OR LOWER(st.admission_no) LIKE ?)"); params += ["%" + q.lower() + "%"] * 2
    return " AND ".join(where), tuple(params)


_BAL = ("(COALESCE((SELECT SUM(l.amount) FROM invoice_lines l JOIN invoices i ON l.invoice_id = i.id WHERE i.student_id = st.id), 0)"
        " - COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.student_id = st.id AND p.voided = 0), 0))")


def _students(audience="", status="active", q="", ids=None):
    where, params = _where(audience, status, q)
    if ids is not None:
        where += " AND st.id IN (%s)" % ",".join("?" * len(ids)) if ids else " AND 1=0"
        params = params + tuple(ids)
    return db.fetch_all(
        "SELECT st.id, st.name, st.admission_no, st.status, g.name AS gname, sm.name AS smname, " + _BAL + " AS balance "
        "FROM students st LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "WHERE " + where + " ORDER BY g.sort_order, g.id, sm.name, st.name", params)


def _departments():
    return db.fetch_all("SELECT id, name FROM clr_departments ORDER BY sort_order, id")


def _library_maps():
    held, owed = {}, {}
    if not _library_on():
        return held, owed
    try:
        for r in db.fetch_all("SELECT student_id, COUNT(*) AS n FROM lib_loans WHERE returned_on IS NULL "
                              "AND student_id IS NOT NULL GROUP BY student_id"):
            held[r["student_id"]] = r["n"]
        for r in db.fetch_all("SELECT student_id, COALESCE(SUM(fine_amount), 0) AS t FROM lib_loans "
                              "WHERE fine_status = 'unpaid' AND student_id IS NOT NULL GROUP BY student_id"):
            owed[r["student_id"]] = r["t"]
    except Exception as err:
        current_app.logger.warning("clearance: library check skipped (%s)", err)
        return {}, {}
    return held, owed


def _signoffs():
    out = {}
    for r in db.fetch_all("SELECT student_id, dept_id, signed_by, signed_on, note FROM clr_signoffs"):
        out.setdefault(r["student_id"], {})[r["dept_id"]] = r
    return out


def assess(rows, depts=None):
    """For each student row -> dict with the department items and the overall verdict."""
    depts = depts if depts is not None else _departments()
    held, owed = _library_maps()
    signs = _signoffs()
    allow = _fee_allow()
    lib = _library_on()
    out = []
    for r in rows:
        items = []
        bal = float(r["balance"] or 0)
        items.append({"key": "fees", "name": "Accounts: school fees", "auto": True, "cleared": bal <= allow + 0.004,
                      "detail": (f"Balance KES {_kes(bal)} unpaid" if bal > allow + 0.004 else
                                 ("Paid in full" if bal > -0.004 else f"Paid in full (credit KES {_kes(-bal)})"))})
        if lib:
            n, t = held.get(r["id"], 0), float(owed.get(r["id"], 0) or 0)
            bits = ([f"{n} book{'s' if n != 1 else ''} not returned"] if n else []) + ([f"fine KES {_kes(t)} unpaid"] if t > 0 else [])
            items.append({"key": "library", "name": "Library", "auto": True, "cleared": not bits,
                          "detail": ", ".join(bits) if bits else "No books held, no fines"})
        mine = signs.get(r["id"], {})
        for d in depts:
            s = mine.get(d["id"])
            items.append({"key": "d%s" % d["id"], "dept_id": d["id"], "name": d["name"], "auto": False,
                          "cleared": bool(s), "detail": (f"Signed {s['signed_on']}" + (f" · {s['note']}" if s["note"] else "")) if s else "Awaiting sign-off",
                          "by": s["signed_by"] if s else "", "on": s["signed_on"] if s else ""})
        out.append({"student": r, "class": r["smname"] or r["gname"] or "", "items": items,
                    "cleared": all(i["cleared"] for i in items),
                    "pending": [i["name"] for i in items if not i["cleared"]]})
    return out


# ------------------------------------------------------------------ pages
def _filters():
    return {"audience": request.values.get("audience", ""), "status": request.values.get("status", "active"),
            "q": request.values.get("q", "").strip(), "purpose": request.values.get("purpose", PURPOSES[0]),
            "only": request.values.get("only", "")}


@clearance_bp.route("/")
def index():
    f = _filters()
    depts = _departments()
    people = assess(_students(f["audience"], f["status"], f["q"]), depts)
    total = len(people)
    if f["only"] == "pending":
        people = [p for p in people if not p["cleared"]]
    elif f["only"] == "cleared":
        people = [p for p in people if p["cleared"]]
    cleared = sum(1 for p in people if p["cleared"])
    return render_template("clearance/index.html", f=f, people=people[:500], shown=len(people), total=total, cleared=cleared,
                           options=_class_options(), purposes=PURPOSES, depts=depts, library_on=_library_on(),
                           is_admin=bool(g.user and g.user["role"] == "admin"), capped=len(people) > 500)


@clearance_bp.route("/student/<int:student_id>")
def student(student_id):
    rows = _students(ids=[student_id], status="all")
    if not rows:
        flash("Student not found.", "error")
        return redirect(url_for("clearance.index"))
    return render_template("clearance/student.html", p=assess(rows)[0], purposes=PURPOSES,
                           purpose=request.args.get("purpose", PURPOSES[0]))


@clearance_bp.route("/student/<int:student_id>/sign", methods=["POST"])
def sign(student_id):
    dept_id = request.form.get("dept_id", type=int)
    dept = db.fetch_one("SELECT id, name FROM clr_departments WHERE id = ?", (dept_id,)) if dept_id else None
    if not dept or not _students(ids=[student_id], status="all"):
        flash("That department or student was not found.", "error")
    elif request.form.get("action") == "unsign":
        db.execute("DELETE FROM clr_signoffs WHERE student_id = ? AND dept_id = ?", (student_id, dept["id"]))
        flash(f"{dept['name']} sign-off removed.", "success")
    else:
        note = (request.form.get("note") or "").strip()[:120]
        db.execute("INSERT INTO clr_signoffs (student_id, dept_id, signed_by, signed_on, note) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT (student_id, dept_id) DO UPDATE SET signed_by = excluded.signed_by, "
                   "signed_on = excluded.signed_on, note = excluded.note",
                   (student_id, dept["id"], _who(), date.today().isoformat(), note or None))
        flash(f"Cleared by {dept['name']}.", "success")
    return redirect(request.form.get("next") or url_for("clearance.student", student_id=student_id))


@clearance_bp.route("/bulk-sign", methods=["POST"])
def bulk_sign():
    f = _filters()
    dept_id = request.form.get("dept_id", type=int)
    dept = db.fetch_one("SELECT id, name FROM clr_departments WHERE id = ?", (dept_id,)) if dept_id else None
    if not dept or not f["audience"]:
        flash("Choose a class or grade first, and a department to sign off.", "error")
    else:
        done = 0
        have = {r["student_id"] for r in db.fetch_all("SELECT student_id FROM clr_signoffs WHERE dept_id = ?", (dept["id"],))}
        for r in _students(f["audience"], f["status"], f["q"]):
            if r["id"] not in have:
                db.execute("INSERT INTO clr_signoffs (student_id, dept_id, signed_by, signed_on) VALUES (?, ?, ?, ?)",
                           (r["id"], dept["id"], _who(), date.today().isoformat()))
                done += 1
        flash(f"{dept['name']} cleared {done} student{'s' if done != 1 else ''}.", "success")
    return redirect(url_for("clearance.index", audience=f["audience"], status=f["status"], purpose=f["purpose"]))


# ------------------------------------------------------------------ downloads
def _brand():
    try:
        import school_theme
        return colors.HexColor(school_theme.load()["primary"])
    except Exception:
        return colors.HexColor("#1f2937")


def _styles():
    ss = getSampleStyleSheet()
    return {"title": ParagraphStyle("t", parent=ss["Title"], fontSize=17, leading=20, spaceAfter=2),
            "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=10, textColor=colors.HexColor("#555555"), alignment=1),
            "body": ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=12.5),
            "cell": ParagraphStyle("c", parent=ss["Normal"], fontSize=9, leading=11.5),
            "small": ParagraphStyle("sm", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#666666"))}


def _form_story(p, purpose, ss):
    s, brand = p["student"], _brand()
    story = branding.logo_story() + [Paragraph(escape(_school()), ss["title"]),
                                     Paragraph("STUDENT CLEARANCE FORM", ss["sub"]), Spacer(1, 3),
                                     Paragraph(escape(purpose), ss["sub"]), Spacer(1, 10)]
    info = [[Paragraph("<b>Name</b>", ss["body"]), Paragraph(escape(s["name"]), ss["body"]),
             Paragraph("<b>Admission no.</b>", ss["body"]), Paragraph(escape(s["admission_no"] or ""), ss["body"])],
            [Paragraph("<b>Class</b>", ss["body"]), Paragraph(escape(p["class"]), ss["body"]),
             Paragraph("<b>Date</b>", ss["body"]), Paragraph(date.today().strftime("%d %B %Y"), ss["body"])]]
    t = Table(info, colWidths=[28 * mm, 66 * mm, 32 * mm, 50 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, brand), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [t, Spacer(1, 12)]
    data = [["Department", "Status", "Details", "Signature & date"]]
    for i in p["items"]:
        sig = (f"{i['by']}, {i['on']}" if i.get("by") else "") if not i["auto"] else ""
        data.append([Paragraph(escape(i["name"]), ss["cell"]),
                     Paragraph("<font color='#1f6b3b'><b>CLEARED</b></font>" if i["cleared"] else "<font color='#a3372c'><b>NOT CLEARED</b></font>", ss["cell"]),
                     Paragraph(escape(i["detail"]), ss["cell"]), Paragraph(escape(sig), ss["cell"])])
    t = Table(data, colWidths=[44 * mm, 31 * mm, 52 * mm, 49 * mm], repeatRows=1, rowHeights=[8 * mm] + [13 * mm] * (len(data) - 1))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), brand), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, 0), 9), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [t, Spacer(1, 14)]
    if p["cleared"]:
        story.append(Paragraph("<font color='#1f6b3b'><b>RESULT: CLEARED. All departments have cleared this student.</b></font>", ss["body"]))
    else:
        story.append(Paragraph("<font color='#a3372c'><b>RESULT: NOT YET CLEARED.</b></font> Outstanding: " + escape("; ".join(p["pending"])) + ".", ss["body"]))
    story += [Spacer(1, 26)]
    sig = Table([["Parent / guardian: ______________________", "Date: ______________"],
                 ["", ""], ["Principal: _________________________", "School stamp"]], colWidths=[100 * mm, 70 * mm],
                rowHeights=[8 * mm, 6 * mm, 8 * mm])
    sig.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9.5)]))
    story += [sig, Spacer(1, 8), Paragraph("Fees and library are checked automatically from the school's records on the date printed.", ss["small"])]
    return story


def _pdf(people, purpose, filename):
    buf, ss = io.BytesIO(), _styles()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title="Student clearance")
    story = []
    for n, p in enumerate(people):
        if n:
            story.append(PageBreak())
        story += _form_story(p, purpose, ss)
    doc.build(story)
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment;filename={_safe_filename(filename, 'pdf')}"})


@clearance_bp.route("/student/<int:student_id>/form.pdf")
def form_pdf(student_id):
    rows = _students(ids=[student_id], status="all")
    if not rows:
        return "Student not found.", 404
    p = assess(rows)[0]
    return _pdf([p], request.args.get("purpose", PURPOSES[0]), f"Clearance {p['student']['name']}")


@clearance_bp.route("/forms.pdf")
def forms_pdf():
    f = _filters()
    people = assess(_students(f["audience"], f["status"], f["q"]))
    if f["only"] == "pending":
        people = [p for p in people if not p["cleared"]]
    elif f["only"] == "cleared":
        people = [p for p in people if p["cleared"]]
    if not people:
        flash("There are no students to print for that selection.", "error")
        return redirect(url_for("clearance.index", **{k: v for k, v in f.items() if v}))
    if len(people) > MAX_FORMS:
        flash(f"That is {len(people)} forms. Print at most {MAX_FORMS} at once: choose a smaller group.", "error")
        return redirect(url_for("clearance.index", **{k: v for k, v in f.items() if v}))
    return _pdf(people, f["purpose"], "Clearance forms")


@clearance_bp.route("/status.csv")
def status_csv():
    f = _filters()
    depts = _departments()
    people = assess(_students(f["audience"], f["status"], f["q"]), depts)
    if f["only"] == "pending":
        people = [p for p in people if not p["cleared"]]
    elif f["only"] == "cleared":
        people = [p for p in people if p["cleared"]]
    names = [i["name"] for i in people[0]["items"]] if people else []
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", "class", "status"] + names + ["outstanding"])
    for p in people:
        s = p["student"]
        w.writerow([_csv_safe(x) for x in [s["admission_no"], s["name"], p["class"], "CLEARED" if p["cleared"] else "NOT CLEARED"]
                    + [("Cleared" if i["cleared"] else i["detail"]) for i in p["items"]] + ["; ".join(p["pending"])]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=clearance_status_{date.today().isoformat()}.csv"})


# ------------------------------------------------------------------ settings (administrator only)
@clearance_bp.route("/settings")
def settings():
    return render_template("clearance/settings.html", depts=_departments(), fee_allow=_get("fee_allow", "0"), library_on=_library_on())


@clearance_bp.route("/settings/save", methods=["POST"])
def settings_save():
    try:
        _put("fee_allow", max(float(request.form.get("fee_allow", "0") or 0), 0))
        flash("Settings saved.", "success")
    except ValueError:
        flash("Type the allowed fee balance as a number, for example 0 or 500.", "error")
    return redirect(url_for("clearance.settings"))


@clearance_bp.route("/settings/dept/add", methods=["POST"])
def dept_add():
    name = (request.form.get("name") or "").strip()[:60]
    if not name:
        flash("Type the department's name.", "error")
    else:
        top = db.fetch_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM clr_departments")["m"]
        db.execute("INSERT INTO clr_departments (name, sort_order) VALUES (?, ?)", (name, top + 1))
        flash(f"Added {name}.", "success")
    return redirect(url_for("clearance.settings"))


@clearance_bp.route("/settings/dept/<int:dept_id>/delete", methods=["POST"])
def dept_delete(dept_id):
    db.execute("DELETE FROM clr_signoffs WHERE dept_id = ?", (dept_id,))
    db.execute("DELETE FROM clr_departments WHERE id = ?", (dept_id,))
    flash("Department removed.", "success")
    return redirect(url_for("clearance.settings"))
