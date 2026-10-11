"""
analytics.py — Exam Analysis module (a Zeraki-style analytics suite inside
the timetable app).

Everything lives under /exams:
    /exams/                    exam list
    /exams/new                 create an exam for one or more grades
    /exams/<id>                overview: means, top students, grade spread, streams
    /exams/<id>/marks          mark-entry grid per stream (+ CSV import/template)
    /exams/<id>/merit          merit list (+ .csv / .pdf)
    /exams/<id>/subjects       subject, subject-by-stream and teacher analysis
    /exams/<id>/setup          choose subjects + "out of", rename, delete
    /exams/<id>/reports.pdf    report forms (one student, a stream, or the grade)
    /exams/compare             compare two exams of the same grade
    /exams/students            student register (+ CSV import)
    /exams/students/<id>       student profile with progress across exams
    /exams/grading             edit grading scales

The maths lives in exam_analysis.py; this file is routing, forms and exports.
"""

import csv
import io
import re
from datetime import date
from xml.sax.saxutils import escape

from flask import (
    Blueprint, Response, flash, redirect, render_template, request, url_for,
)
from markupsafe import Markup
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

import curriculum
import branding
import import_files
import database as db
import exam_analysis as ea

analytics_bp = Blueprint("analytics", __name__, url_prefix="/exams")

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------
@analytics_bp.app_template_filter("pct")
def _pct_filter(v, digits=1):
    return "—" if v is None else f"{v:.{digits}f}"


@analytics_bp.app_template_filter("num")
def _num_filter(v):
    """12.0 -> '12', 12.5 -> '12.5', None -> ''."""
    if v is None:
        return ""
    return f"{v:g}" if isinstance(v, float) else str(v)


@analytics_bp.app_template_filter("lvl")
def _level_class(label):
    """CSS class for a grade label, so colours mean the same on both scales."""
    if not label:
        return ""
    if label[:2] in ("EE",) or label == "A" or label == "A-":
        return "lv-top"
    if label[:2] == "ME" or label[0] == "B":
        return "lv-good"
    if label[:2] == "AE" or label[0] == "C":
        return "lv-mid"
    return "lv-low"


@analytics_bp.app_template_filter("signed")
def _signed(v):
    if v is None:
        return "—"
    return f"+{v:g}" if v > 0 else f"{v:g}"


# --------------------------------------------------------------------------
# Small shared utilities
# --------------------------------------------------------------------------
def _safe_filename(title, extension):
    ascii_only = title.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    return f"{safe or 'export'}.{extension}"


def _csv_safe(value):
    """Stop spreadsheet apps treating a text cell as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _norm(text):
    """Lower-case and strip everything but letters/digits, for tolerant header matching."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _parse_score(raw, out_of):
    """Return (status, value): ('blank', None) | ('ok', float) | ('bad', None)."""
    raw = (raw or "").strip().replace(",", ".")
    if raw == "":
        return "blank", None
    try:
        v = float(raw)
    except ValueError:
        return "bad", None
    if v < 0 or v > out_of:
        return "bad", None
    return "ok", round(v, 2)


_ADMISSION_ALIASES = {
    "admissionno", "admission", "admissionnumber", "adm", "admno", "admnumber",
    "regno", "regnumber", "registrationno", "registrationnumber", "studentno", "studentid",
}


def _read_csv_upload(field="file"):
    """Read an uploaded CSV, Excel (.xlsx) or PDF table into (rows, error).
    Copes with the ways Excel saves CSV: a BOM, UTF-16 ("Unicode Text"), ';' or
    tab separators, and title rows above the real header row. Excel and PDF
    files are converted to rows by import_files and then follow the same path."""
    f = request.files.get(field)
    if not f or not f.filename:
        return None, "Choose a CSV, Excel or PDF file first."
    data = f.read(import_files.MAX_BINARY_UPLOAD_BYTES + 1)
    kind = import_files.sniff_kind(data, f.filename)
    limit = import_files.MAX_BINARY_UPLOAD_BYTES if kind else MAX_UPLOAD_BYTES
    if len(data) > limit:
        return None, f"That file is too large (limit {limit // (1024 * 1024)} MB)."
    if kind:
        rows, err = import_files.rows_from_upload(data, kind)
        if err:
            return None, err
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        text = buf.getvalue()
    elif data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, "The file looks empty."
    delimiter = max((",", ";", "\t"), key=lines[0].count)
    # Skip title rows: the header is the first early row that names the admission column.
    header_at = 0
    for i, line in enumerate(lines[:15]):
        try:
            cells = next(csv.reader([line], delimiter=max((",", ";", "\t"), key=line.count)))
        except (StopIteration, csv.Error):
            continue
        if any(_norm(c) in _ADMISSION_ALIASES for c in cells):
            header_at, delimiter = i, max((",", ";", "\t"), key=line.count)
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_at:])), delimiter=delimiter)
    if not reader.fieldnames:
        return None, "The file looks empty."
    return reader, None


def _adm_key(value):
    """Admission numbers compared loosely: case, spaces, and Excel's '1001.0'."""
    v = (value or "").strip().lower()
    return re.sub(r"\.0+$", "", v) if re.fullmatch(r"\d+\.0+", v) else v


def _headings(reader):
    shown = [h for h in (reader.fieldnames or []) if h and h.strip()][:8]
    return ", ".join(f"“{h.strip()}”" for h in shown) or "none"


def _find_column(fieldnames, *aliases):
    wanted = {_norm(a) for a in aliases}
    for name in fieldnames:
        if _norm(name) in wanted:
            return name
    return None


def _upsert_mark(exam_id, student_id, subject_id, score):
    return (
        "INSERT INTO marks (exam_id, student_id, subject_id, score) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (exam_id, student_id, subject_id) DO UPDATE SET score = excluded.score",
        (exam_id, student_id, subject_id, score),
    )


def _grade_streams(grade_id):
    return db.fetch_all(
        "SELECT id, name, pathway_id FROM streams WHERE grade_id=? ORDER BY name", (grade_id,)
    )


def _load_or_404(exam_id):
    res = ea.build_results(exam_id)
    return res


def _exam_missing():
    flash("That exam no longer exists.", "error")
    return redirect(url_for("analytics.exams_page"))


def _exam_title(exam):
    return f"{exam['name']} — Term {exam['term']}, {exam['year']} ({exam['grade_name']})"


# --------------------------------------------------------------------------
# Exam list + create
# --------------------------------------------------------------------------
@analytics_bp.route("/")
def exams_page():
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")
    grade_id = request.args.get("grade_id", type=int)
    query = (
        "SELECT e.id, e.name, e.term, e.year, e.exam_date, g.name AS grade_name, "
        "(SELECT COUNT(*) FROM exam_subjects es WHERE es.exam_id = e.id) AS n_subjects, "
        "(SELECT COUNT(DISTINCT m.student_id) FROM marks m WHERE m.exam_id = e.id) AS n_marked, "
        "(SELECT COUNT(*) FROM students st WHERE st.grade_id = e.grade_id AND st.active = 1) AS n_students "
        "FROM exams e JOIN grades g ON e.grade_id = g.id"
    )
    params = ()
    if grade_id:
        query += " WHERE e.grade_id = ?"
        params = (grade_id,)
    query += " ORDER BY e.year DESC, e.term DESC, g.sort_order, e.name"
    exams = db.fetch_all(query, params)
    n_students = db.fetch_one("SELECT COUNT(*) AS c FROM students WHERE active=1")["c"]
    return render_template(
        "analytics/exams.html", exams=exams, grades=grades, grade_id=grade_id,
        n_students=n_students,
    )


@analytics_bp.route("/new", methods=["GET", "POST"])
def new_exam():
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        term = request.form.get("term", type=int) or 1
        year = request.form.get("year", type=int) or date.today().year
        exam_date = request.form.get("exam_date", "").strip() or None
        grade_ids = request.form.getlist("grade_ids", type=int)
        out_of = request.form.get("out_of", type=int) or 100

        if not name:
            flash("Give the exam a name, e.g. “Mid-Term 1” or “End of Term 2”.", "error")
            return redirect(url_for("analytics.new_exam"))
        if not grade_ids:
            flash("Choose at least one grade.", "error")
            return redirect(url_for("analytics.new_exam"))
        if term not in (1, 2, 3) or out_of < 1 or out_of > 1000:
            flash("Term must be 1, 2 or 3 and “out of” between 1 and 1000.", "error")
            return redirect(url_for("analytics.new_exam"))

        created, skipped = [], []
        for gid in grade_ids:
            grade = db.fetch_one("SELECT id, name FROM grades WHERE id=?", (gid,))
            if not grade:
                continue
            try:
                exam_id = db.execute(
                    "INSERT INTO exams (name, term, year, exam_date, grade_id) VALUES (?, ?, ?, ?, ?)",
                    (name, term, year, exam_date, gid),
                )
            except Exception:
                skipped.append(grade["name"])
                continue
            subs = db.fetch_all("SELECT id FROM subjects WHERE grade_id=? ORDER BY id", (gid,))
            db.execute_many([
                ("INSERT INTO exam_subjects (exam_id, subject_id, out_of) VALUES (?, ?, ?)",
                 (exam_id, s["id"], out_of)) for s in subs
            ])
            created.append(exam_id)

        if skipped:
            flash("Already exists for: " + ", ".join(skipped), "error")
        if not created:
            return redirect(url_for("analytics.new_exam"))
        flash(f"Created {len(created)} exam(s). Remove any subjects not examined under Setup.", "success")
        if len(created) == 1:
            return redirect(url_for("analytics.exam_overview", exam_id=created[0]))
        return redirect(url_for("analytics.exams_page"))

    return render_template("analytics/new_exam.html", grades=grades, this_year=date.today().year)


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------
@analytics_bp.route("/<int:exam_id>")
def exam_overview(exam_id):
    res = _load_or_404(exam_id)
    if not res:
        return _exam_missing()
    ranked = res["ranked"]
    grade_mean = round(sum(s["mean_pct"] for s in ranked) / len(ranked), 2) if ranked else None
    grade_label = ea.band_for(grade_mean, res["bands"])["label"] if ranked else None
    dist = ea.overall_distribution(res)
    return render_template(
        "analytics/overview.html", res=res, exam=res["exam"], active="overview",
        n_students=len(res["students"]), n_ranked=len(ranked),
        grade_mean=grade_mean, grade_label=grade_label,
        top=ranked[:5], dist=dist, max_count=max([d["count"] for d in dist] + [1]),
        streams=ea.stream_stats(res), subjects=ea.subject_stats(res),
    )


# --------------------------------------------------------------------------
# Setup (subjects + out-of, rename, delete)
# --------------------------------------------------------------------------
@analytics_bp.route("/<int:exam_id>/setup", methods=["GET", "POST"])
def exam_setup(exam_id):
    exam = ea.get_exam(exam_id)
    if not exam:
        return _exam_missing()

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "delete":
            db.execute("DELETE FROM exams WHERE id=?", (exam_id,))
            flash("Exam deleted along with all its marks.", "success")
            return redirect(url_for("analytics.exams_page"))

        name = request.form.get("name", "").strip()
        term = request.form.get("term", type=int) or exam["term"]
        year = request.form.get("year", type=int) or exam["year"]
        exam_date = request.form.get("exam_date", "").strip() or None
        if not name or term not in (1, 2, 3):
            flash("Enter an exam name and a term of 1, 2 or 3.", "error")
            return redirect(url_for("analytics.exam_setup", exam_id=exam_id))

        statements = []
        grade_subjects = db.fetch_all("SELECT id FROM subjects WHERE grade_id=?", (exam["grade_id"],))
        included = set(request.form.getlist("include", type=int))
        bad_out_of = False
        for s in grade_subjects:
            if s["id"] in included:
                out_of = request.form.get(f"out_of_{s['id']}", type=int)
                if not out_of or out_of < 1 or out_of > 1000:
                    bad_out_of = True
                    continue
                statements.append((
                    "INSERT INTO exam_subjects (exam_id, subject_id, out_of) VALUES (?, ?, ?) "
                    "ON CONFLICT (exam_id, subject_id) DO UPDATE SET out_of = excluded.out_of",
                    (exam_id, s["id"], out_of),
                ))
            else:
                statements.append(("DELETE FROM marks WHERE exam_id=? AND subject_id=?", (exam_id, s["id"])))
                statements.append(("DELETE FROM exam_subjects WHERE exam_id=? AND subject_id=?", (exam_id, s["id"])))
        if bad_out_of:
            flash("Every included subject needs an “out of” between 1 and 1000.", "error")
            return redirect(url_for("analytics.exam_setup", exam_id=exam_id))
        statements.append((
            "UPDATE exams SET name=?, term=?, year=?, exam_date=? WHERE id=?",
            (name, term, year, exam_date, exam_id),
        ))
        try:
            db.execute_many(statements)
        except Exception as e:
            flash(f"Could not save: {e}", "error")
            return redirect(url_for("analytics.exam_setup", exam_id=exam_id))

        over = db.fetch_one(
            "SELECT COUNT(*) AS c FROM marks m JOIN exam_subjects es "
            "ON es.exam_id = m.exam_id AND es.subject_id = m.subject_id "
            "WHERE m.exam_id=? AND m.score > es.out_of", (exam_id,),
        )["c"]
        flash("Exam setup saved.", "success")
        if over:
            flash(f"{over} existing mark(s) are now higher than the “out of” — "
                  "re-enter them on the Marks page.", "error")
        return redirect(url_for("analytics.exam_setup", exam_id=exam_id))

    subjects = db.fetch_all(
        "SELECT sub.id, sub.name, sub.category, p.name AS pathway_name, es.out_of "
        "FROM subjects sub LEFT JOIN pathways p ON sub.pathway_id = p.id "
        "LEFT JOIN exam_subjects es ON es.subject_id = sub.id AND es.exam_id = ? "
        "WHERE sub.grade_id = ? ORDER BY sub.id",
        (exam_id, exam["grade_id"]),
    )
    n_marks = db.fetch_one("SELECT COUNT(*) AS c FROM marks WHERE exam_id=?", (exam_id,))["c"]
    return render_template(
        "analytics/setup.html", exam=exam, subjects=subjects, active="setup", n_marks=n_marks,
    )


# --------------------------------------------------------------------------
# Marks: grid entry, CSV import, CSV template
# --------------------------------------------------------------------------
def _stream_choice(exam, arg):
    """Resolve ?stream_id= to (streams_for_picker, selected, stream_row_or_None).
    `selected` is an int stream id, the string 'none' (students with no stream),
    or None when the grade has no students yet."""
    streams = [dict(s) for s in _grade_streams(exam["grade_id"])]
    counts = {r["stream_id"]: r["c"] for r in db.fetch_all(
        "SELECT stream_id, COUNT(*) AS c FROM students "
        "WHERE grade_id=? AND active=1 GROUP BY stream_id", (exam["grade_id"],))}
    for s in streams:
        s["n"] = counts.get(s["id"], 0)
    unassigned = counts.get(None, 0)
    picker = [s for s in streams]
    if unassigned:
        picker.append({"id": "none", "name": "No stream", "pathway_id": None, "n": unassigned})

    valid = {str(s["id"]) for s in picker}
    selected = arg if arg in valid else None
    if selected is None:
        first = next((s for s in picker if s["n"]), None)
        selected = str(first["id"]) if first else None
    stream_row = next((s for s in picker if str(s["id"]) == selected), None)
    return picker, selected, stream_row


def _stream_students(exam, selected):
    if selected == "none":
        return db.fetch_all(
            "SELECT id, admission_no, name FROM students "
            "WHERE grade_id=? AND active=1 AND stream_id IS NULL ORDER BY name", (exam["grade_id"],))
    return db.fetch_all(
        "SELECT id, admission_no, name FROM students "
        "WHERE grade_id=? AND active=1 AND stream_id=? ORDER BY name",
        (exam["grade_id"], int(selected)))


def _applicable_subjects(exam_id, stream_row):
    """Subjects shown for a stream: everything for streams without a pathway;
    core + the stream's own pathway for pathway streams (Senior School)."""
    subs = ea.exam_subjects(exam_id)
    pw = stream_row.get("pathway_id") if stream_row else None
    if pw is None:
        return subs
    return [s for s in subs if s["pathway_id"] is None or s["pathway_id"] == pw]


@analytics_bp.route("/<int:exam_id>/marks")
def marks_page(exam_id):
    exam = ea.get_exam(exam_id)
    if not exam:
        return _exam_missing()
    picker, selected, stream_row = _stream_choice(exam, request.args.get("stream_id"))
    students, subjects, marks = [], [], {}
    if selected:
        students = _stream_students(exam, selected)
        subjects = _applicable_subjects(exam_id, stream_row)
        for m in db.fetch_all("SELECT student_id, subject_id, score FROM marks WHERE exam_id=?", (exam_id,)):
            marks[(m["student_id"], m["subject_id"])] = m["score"]
    n_exam_subjects = db.fetch_one("SELECT COUNT(*) AS c FROM exam_subjects WHERE exam_id=?", (exam_id,))["c"]
    return render_template(
        "analytics/marks.html", exam=exam, picker=picker, selected=selected,
        students=students, subjects=subjects, marks=marks, active="marks",
        n_exam_subjects=n_exam_subjects,
    )


@analytics_bp.route("/<int:exam_id>/marks/save", methods=["POST"])
def marks_save(exam_id):
    exam = ea.get_exam(exam_id)
    if not exam:
        return _exam_missing()
    selected = request.form.get("stream_id", "")
    picker, selected, stream_row = _stream_choice(exam, selected)
    if not selected:
        flash("No students to save marks for.", "error")
        return redirect(url_for("analytics.marks_page", exam_id=exam_id))

    students = _stream_students(exam, selected)
    subjects = _applicable_subjects(exam_id, stream_row)
    statements, saved, cleared, errors = [], 0, 0, []
    for st in students:
        for sub in subjects:
            key = f"m_{st['id']}_{sub['id']}"
            if key not in request.form:
                continue
            status, value = _parse_score(request.form[key], sub["out_of"])
            if status == "ok":
                statements.append(_upsert_mark(exam_id, st["id"], sub["id"], value))
                saved += 1
            elif status == "blank":
                statements.append((
                    "DELETE FROM marks WHERE exam_id=? AND student_id=? AND subject_id=?",
                    (exam_id, st["id"], sub["id"])))
                cleared += 1
            else:
                errors.append(f"{st['name']} – {sub['name']} (0 to {sub['out_of']})")
    if errors:
        # Nothing is written if anything is invalid, so a typo never leaves a
        # half-saved page behind.
        shown = "; ".join(errors[:6]) + (f" … and {len(errors) - 6} more" if len(errors) > 6 else "")
        flash(f"Not saved — {len(errors)} invalid mark(s): {shown}", "error")
    else:
        db.execute_many(statements)
        flash(f"Saved {saved} mark(s)" + (f", cleared {cleared} blank cell(s)." if cleared else "."), "success")
    return redirect(url_for("analytics.marks_page", exam_id=exam_id, stream_id=selected))


@analytics_bp.route("/<int:exam_id>/import", methods=["POST"])
def marks_import(exam_id):
    exam = ea.get_exam(exam_id)
    if not exam:
        return _exam_missing()
    back = url_for("analytics.marks_page", exam_id=exam_id, stream_id=request.form.get("stream_id", ""))
    reader, err = _read_csv_upload()
    if err:
        flash(err, "error")
        return redirect(back)

    if db.fetch_one("SELECT COUNT(*) AS c FROM students WHERE grade_id=? AND active=1", (exam["grade_id"],))["c"] == 0:
        flash(f"There are no active students in {exam['grade_name']} yet, so there is nothing to match the marks to. "
              "Admit or import the students first (School Management → Students & Guardians).", "error")
        return redirect(back)

    adm_col = next((h for h in reader.fieldnames if h and _norm(h) in _ADMISSION_ALIASES), None)
    if not adm_col:
        flash("The CSV needs an “admission_no” column. Headings found in your file: "
              f"{_headings(reader)}. Download the template above to get the exact layout.", "error")
        return redirect(back)

    subjects = ea.exam_subjects(exam_id)
    col_to_subject = {}
    for col in reader.fieldnames:
        for s in subjects:
            if _norm(col) == _norm(s["name"]):
                col_to_subject[col] = s
    if not col_to_subject:
        flash("None of the column headings match this exam's subjects. Headings found in your file: "
              f"{_headings(reader)}. Download the template to get the exact headings.", "error")
        return redirect(back)

    students = {_adm_key(r["admission_no"]): r["id"] for r in db.fetch_all(
        "SELECT id, admission_no FROM students WHERE grade_id=? AND active=1", (exam["grade_id"],))}
    statements, saved, unknown, bad = [], 0, [], 0
    for row in reader:
        adm = _adm_key(row.get(adm_col))
        if not adm:
            continue
        sid = students.get(adm)
        if sid is None:
            unknown.append(adm)
            continue
        for col, sub in col_to_subject.items():
            status, value = _parse_score(row.get(col), sub["out_of"])
            if status == "ok":
                statements.append(_upsert_mark(exam_id, sid, sub["id"], value))
                saved += 1
            elif status == "bad":
                bad += 1
    if bad:
        flash(f"Nothing imported — {bad} value(s) were not numbers between 0 and the subject's “out of”. "
              "Fix the file and upload again.", "error")
        return redirect(back)
    if saved == 0:
        msg = "Nothing was imported — no marks were found to save."
        if unknown:
            msg += (f" None of the admission numbers ({', '.join(sorted(set(unknown))[:6])}) match active students in "
                    f"{exam['grade_name']}. Check the grade of this exam, or that the students are admitted.")
        else:
            msg += " The subject cells in the file were empty."
        flash(msg, "error")
        return redirect(back)
    db.execute_many(statements)
    flash(f"Imported {saved} mark(s) across {len(col_to_subject)} subject column(s).", "success")
    if unknown:
        shown = ", ".join(sorted(set(unknown))[:8])
        flash(f"{len(set(unknown))} admission number(s) not found in {exam['grade_name']}: {shown}", "error")
    return redirect(back)


@analytics_bp.route("/<int:exam_id>/template.csv")
def marks_template(exam_id):
    exam = ea.get_exam(exam_id)
    if not exam:
        return "Exam not found", 404
    subjects = ea.exam_subjects(exam_id)
    picker, selected, stream_row = _stream_choice(exam, request.args.get("stream_id"))
    if request.args.get("stream_id") == "all" or not selected:
        students = db.fetch_all(
            "SELECT id, admission_no, name FROM students WHERE grade_id=? AND active=1 ORDER BY name",
            (exam["grade_id"],))
    else:
        students = _stream_students(exam, selected)
        subjects = _applicable_subjects(exam_id, stream_row)
    marks = {(m["student_id"], m["subject_id"]): m["score"] for m in db.fetch_all(
        "SELECT student_id, subject_id, score FROM marks WHERE exam_id=?", (exam_id,))}

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name"] + [s["name"] for s in subjects])
    for st in students:
        w.writerow([_csv_safe(st["admission_no"]), _csv_safe(st["name"])] +
                   [f"{marks[(st['id'], s['id'])]:g}" if (st["id"], s["id"]) in marks else "" for s in subjects])
    title = f"{exam['name']} {exam['grade_name']} marks template"
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename={_safe_filename(title, 'csv')}"})


# --------------------------------------------------------------------------
# Merit list
# --------------------------------------------------------------------------
def _merit_rows(res, stream_key):
    """Students shown on a merit list, best first. stream_key: None = whole grade,
    'none' = students without a stream, else an int stream id."""
    rows = res["ranked"]
    if stream_key == "none":
        rows = [s for s in rows if s["stream_id"] is None]
    elif stream_key is not None:
        rows = [s for s in rows if s["stream_id"] == stream_key]
    return rows


def _stream_key(arg):
    if arg in (None, "", "all"):
        return None
    if arg == "none":
        return "none"
    try:
        return int(arg)
    except ValueError:
        return None


@analytics_bp.route("/<int:exam_id>/merit")
def merit_list(exam_id):
    res = _load_or_404(exam_id)
    if not res:
        return _exam_missing()
    key = _stream_key(request.args.get("stream_id"))
    rows = _merit_rows(res, key)
    unmarked = [s for s in res["students"] if not s["n_subjects"]]
    # Students with fewer subjects than the most common count are flagged: a
    # half-entered result can otherwise sit at the top on a high mean.
    counts = [s["n_subjects"] for s in res["ranked"]]
    typical = max(set(counts), key=counts.count) if counts else 0
    return render_template(
        "analytics/merit.html", res=res, exam=res["exam"], rows=rows, stream_key=key,
        unmarked=unmarked, typical=typical, active="merit",
    )


def _merit_table(res, rows):
    header = (["Pos", "Str Pos", "Adm No", "Name", "Stream"] +
              [s["name"] for s in res["subjects"]] +
              ["Total", "Mean %", "Grade", "Points"])
    body = []
    for s in rows:
        cells = []
        for sub in res["subjects"]:
            v = s["scores"].get(sub["id"])
            cells.append("" if v is None else f"{v['score']:g}")
        body.append([s["position"], s["stream_position"], s["admission_no"], s["name"],
                     s["stream_name"] or "—"] + cells +
                    [f"{s['total']:g}", f"{s['mean_pct']:.1f}", s["grade_label"], s["points"]])
    return header, body


@analytics_bp.route("/<int:exam_id>/merit.csv")
def merit_csv(exam_id):
    res = _load_or_404(exam_id)
    if not res:
        return "Exam not found", 404
    rows = _merit_rows(res, _stream_key(request.args.get("stream_id")))
    header, body = _merit_table(res, rows)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    for r in body:
        w.writerow([_csv_safe(c) for c in r])
    title = f"{res['exam']['name']} {res['exam']['grade_name']} merit list"
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename={_safe_filename(title, 'csv')}"})


@analytics_bp.route("/<int:exam_id>/merit.pdf")
def merit_pdf(exam_id):
    res = _load_or_404(exam_id)
    if not res:
        return "Exam not found", 404
    key = _stream_key(request.args.get("stream_id"))
    rows = _merit_rows(res, key)
    header, body = _merit_table(res, rows)
    styles = getSampleStyleSheet()
    school = db.get_school_settings()["name"]
    scope = "All streams"
    if key == "none":
        scope = "No stream"
    elif key is not None:
        scope = next((s["name"] for s in res["streams"] if s["id"] == key), "")
    title = f"{school} — Merit List"
    sub = f"{_exam_title(res['exam'])} · {scope}"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm)
    small = ParagraphStyle("c", fontSize=6.5, leading=7.5)
    head_cells = [Paragraph(f"<b>{escape(str(h))}</b>", small) for h in header]
    data = [head_cells] + [[Paragraph(escape(str(c)), small) for c in r] for r in body]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4e6c6")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b5ad96")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#faf7ee")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    doc.build(branding.logo_story() + [Paragraph(escape(title), styles["Title"]), Paragraph(escape(sub), styles["Normal"]), Spacer(1, 8), t])
    return Response(buf.getvalue(), mimetype="application/pdf", headers={
        "Content-Disposition": f"attachment;filename={_safe_filename(title + ' ' + sub, 'pdf')}"})


# --------------------------------------------------------------------------
# Subject / stream / teacher analysis
# --------------------------------------------------------------------------
@analytics_bp.route("/<int:exam_id>/subjects")
def subject_analysis(exam_id):
    res = _load_or_404(exam_id)
    if not res:
        return _exam_missing()
    return render_template(
        "analytics/subjects.html", res=res, exam=res["exam"], active="subjects",
        stats=ea.subject_stats(res), grid=ea.subject_by_stream(res),
        teachers=ea.teacher_performance(res), streams=res["streams"],
    )


# --------------------------------------------------------------------------
# Report forms (PDF)
# --------------------------------------------------------------------------
def _report_pages(res, prev, students, school_name):
    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Title"], fontSize=17, spaceAfter=2)
    sub = ParagraphStyle("s", parent=styles["Normal"], alignment=1, textColor=colors.HexColor("#6f6a5b"))
    small = ParagraphStyle("sm", parent=styles["Normal"], fontSize=8.5, leading=11)
    exam = res["exam"]
    is_cbc = res["scale"] == "CBC8"
    subj_by_id = {s["id"]: s for s in res["subjects"]}
    stream_counts = {}
    for s in res["ranked"]:
        stream_counts[s["stream_id"]] = stream_counts.get(s["stream_id"], 0) + 1
    prev_by_id = {s["id"]: s for s in prev["ranked"]} if prev else {}

    story = []
    logo = branding.get_logo()  # one lookup for the whole batch of reports
    for i, st in enumerate(students):
        if i:
            story.append(PageBreak())
        story += branding.logo_story(found=logo)
        story.append(Paragraph(escape(school_name), h))
        story.append(Paragraph("Student Progress Report", sub))
        story.append(Spacer(1, 8))
        info = Table([
            ["Name:", st["name"], "Adm No:", st["admission_no"]],
            ["Class:", st["stream_name"] or exam["grade_name"], "Exam:", exam["name"]],
            ["Grade:", exam["grade_name"], "Term / Year:", f"Term {exam['term']}, {exam['year']}"],
        ], colWidths=[22 * mm, 68 * mm, 28 * mm, 62 * mm])
        info.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story += [info, Spacer(1, 8)]

        rows = [["Subject", "Marks", "Out of", "%", "Level" if is_cbc else "Grade", "Pts", "Position", "Remark"]]
        for sid, v in st["scores"].items():
            sitting = sum(1 for x in res["students"] if sid in x["scores"])
            band_remark = next((b["remark"] for b in res["bands"] if b["label"] == v["label"]), "")
            rows.append([subj_by_id[sid]["name"], f"{v['score']:g}", v["out_of"], f"{v['pct']:.0f}",
                         v["label"], v["points"], f"{v['rank']}/{sitting}", band_remark])
        t = Table(rows, repeatRows=1, colWidths=[52 * mm, 16 * mm, 16 * mm, 14 * mm, 16 * mm, 12 * mm, 20 * mm, 34 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103524")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b5ad96")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#faf7ee")]),
            ("ALIGN", (1, 0), (6, -1), "CENTER"),
        ]))
        story += [t, Spacer(1, 10)]

        summary = [
            ["Total marks", f"{st['total']:g} / {st['out_total']:g}", "Mean %", f"{st['mean_pct']:.1f}"],
            ["Overall grade" if not is_cbc else "Overall level", st["grade_label"], "Total points", st["points"]],
            ["Position in grade", f"{st['position']} of {len(res['ranked'])}",
             "Position in stream",
             f"{st['stream_position']} of {stream_counts.get(st['stream_id'], 0)}" if st["stream_name"] else "—"],
        ]
        p = prev_by_id.get(st["id"])
        if p:
            diff = st["mean_pct"] - p["mean_pct"]
            summary.append([f"Previous ({prev['exam']['name']})", f"{p['mean_pct']:.1f}%",
                            "Change", f"{'+' if diff > 0 else ''}{diff:.1f}"])
        s_t = Table(summary, colWidths=[40 * mm, 45 * mm, 40 * mm, 45 * mm])
        s_t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4e6c6")),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8932b")),
        ]))
        story += [s_t, Spacer(1, 8)]

        if is_cbc:
            legend = "  ·  ".join(f"{k} = {v}" for k, v in curriculum.CBC_LEVEL_NAMES.items())
            story.append(Paragraph(f"<i>{legend}</i>", small))
            story.append(Spacer(1, 6))
        story.append(Paragraph(f"Overall performance: <b>{escape(st['remark'] or '')}</b>", small))
        story.append(Spacer(1, 14))
        for label in ("Class teacher's remarks", "Principal's remarks", "Parent's signature"):
            story.append(Paragraph(f"{label}: " + "_" * 70, small))
            story.append(Spacer(1, 12))
    return story


@analytics_bp.route("/<int:exam_id>/reports.pdf")
def reports_pdf(exam_id):
    res = _load_or_404(exam_id)
    if not res:
        return "Exam not found", 404
    student_id = request.args.get("student_id", type=int)
    key = _stream_key(request.args.get("stream_id"))
    if student_id:
        students = [s for s in res["ranked"] if s["id"] == student_id]
    else:
        students = sorted(_merit_rows(res, key), key=lambda s: s["name"])
    if not students:
        return "No marked students to produce reports for.", 400

    prev_id = ea.previous_exam_id(res["exam"])
    prev = ea.build_results(prev_id) if prev_id else None
    school = db.get_school_settings()["name"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    doc.build(_report_pages(res, prev, students, school))
    label = students[0]["name"] if len(students) == 1 else f"{res['exam']['grade_name']} reports"
    fname = _safe_filename(f"{res['exam']['name']} {label}", "pdf")
    return Response(buf.getvalue(), mimetype="application/pdf", headers={
        "Content-Disposition": f"attachment;filename={fname}"})


# --------------------------------------------------------------------------
# Compare two exams
# --------------------------------------------------------------------------
@analytics_bp.route("/compare")
def compare_page():
    exams = db.fetch_all(
        "SELECT e.id, e.name, e.term, e.year, e.grade_id, g.name AS grade_name "
        "FROM exams e JOIN grades g ON e.grade_id = g.id "
        "ORDER BY g.sort_order, e.year DESC, e.term DESC, e.name")
    a, b = request.args.get("a", type=int), request.args.get("b", type=int)
    result = res_a = res_b = None
    if a and b:
        if a == b:
            flash("Choose two different exams.", "error")
        else:
            res_a, res_b = ea.build_results(a), ea.build_results(b)
            if not res_a or not res_b:
                flash("One of those exams no longer exists.", "error")
                res_a = res_b = None
            elif res_a["exam"]["grade_id"] != res_b["exam"]["grade_id"]:
                flash("You can only compare exams for the same grade.", "error")
                res_a = res_b = None
            else:
                result = ea.compare_exams(res_a, res_b)
    return render_template(
        "analytics/compare.html", exams=exams, a=a, b=b, result=result, res_a=res_a, res_b=res_b,
    )


# --------------------------------------------------------------------------
# Students
# --------------------------------------------------------------------------
@analytics_bp.route("/students")
def students_page():
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")
    streams = db.fetch_all(
        "SELECT s.id, s.name, s.grade_id FROM streams s JOIN grades g ON s.grade_id = g.id "
        "ORDER BY g.sort_order, s.name")
    grade_id = request.args.get("grade_id", type=int)
    stream_id = request.args.get("stream_id", type=int)
    q = request.args.get("q", "").strip().lower()

    query = (
        "SELECT st.id, st.admission_no, st.name, st.gender, g.name AS grade_name, "
        "s.name AS stream_name FROM students st JOIN grades g ON st.grade_id = g.id "
        "LEFT JOIN streams s ON st.stream_id = s.id WHERE st.active = 1"
    )
    params = []
    if grade_id:
        query += " AND st.grade_id = ?"
        params.append(grade_id)
    if stream_id:
        query += " AND st.stream_id = ?"
        params.append(stream_id)
    if q:
        query += " AND (LOWER(st.name) LIKE ? OR LOWER(st.admission_no) LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY g.sort_order, s.name, st.name"
    rows = db.fetch_all(query, tuple(params))
    return render_template(
        "analytics/students.html", students=rows[:500], total=len(rows), grades=grades,
        streams=streams, grade_id=grade_id, stream_id=stream_id, q=q,
    )


@analytics_bp.route("/students/add", methods=["POST"])
def student_add():
    adm = request.form.get("admission_no", "").strip()
    name = request.form.get("name", "").strip()
    gender = request.form.get("gender", "").strip().upper()[:1]
    grade_id = request.form.get("grade_id", type=int)
    stream_id = request.form.get("stream_id", type=int)
    if not adm or not name or not grade_id:
        flash("Admission number, name and grade are required.", "error")
        return redirect(url_for("analytics.students_page", grade_id=grade_id))
    if stream_id:
        ok = db.fetch_one("SELECT id FROM streams WHERE id=? AND grade_id=?", (stream_id, grade_id))
        if not ok:
            stream_id = None
    try:
        db.execute(
            "INSERT INTO students (admission_no, name, gender, grade_id, stream_id) VALUES (?, ?, ?, ?, ?)",
            (adm, name, gender if gender in ("M", "F") else None, grade_id, stream_id),
        )
        flash(f"Added {name}.", "success")
    except Exception:
        flash(f"Admission number {adm} is already in use.", "error")
    return redirect(url_for("analytics.students_page", grade_id=grade_id, stream_id=stream_id))


@analytics_bp.route("/students/import", methods=["POST"])
def students_import():
    back = url_for("analytics.students_page")
    reader, err = _read_csv_upload()
    if err:
        flash(err, "error")
        return redirect(back)
    adm_c = _find_column(reader.fieldnames, "admission_no", "adm", "admno", "admission")
    name_c = _find_column(reader.fieldnames, "name", "fullname", "studentname")
    grade_c = _find_column(reader.fieldnames, "grade", "class", "form")
    stream_c = _find_column(reader.fieldnames, "stream")
    gender_c = _find_column(reader.fieldnames, "gender", "sex")
    if not (adm_c and name_c and grade_c):
        flash("The CSV needs admission_no, name and grade columns (stream and gender are optional).", "error")
        return redirect(back)

    grades = {_norm(g["name"]): g["id"] for g in db.fetch_all("SELECT id, name FROM grades")}
    streams = {(s["grade_id"], _norm(s["name"])): s["id"] for s in db.fetch_all("SELECT id, name, grade_id FROM streams")}
    existing = {r["admission_no"].lower() for r in db.fetch_all("SELECT admission_no FROM students")}
    statements, added, dup, bad_grade, bad_stream = [], 0, 0, set(), set()
    seen = set()
    for row in reader:
        adm = (row.get(adm_c) or "").strip()
        name = (row.get(name_c) or "").strip()
        if not adm or not name:
            continue
        if adm.lower() in existing or adm.lower() in seen:
            dup += 1
            continue
        gid = grades.get(_norm(row.get(grade_c)))
        if not gid:
            bad_grade.add(row.get(grade_c) or "(blank)")
            continue
        sid = None
        if stream_c and (row.get(stream_c) or "").strip():
            sid = streams.get((gid, _norm(row[stream_c])))
            if sid is None:
                bad_stream.add(row[stream_c].strip())
        gender = ((row.get(gender_c) or "").strip().upper()[:1]) if gender_c else ""
        statements.append((
            "INSERT INTO students (admission_no, name, gender, grade_id, stream_id) VALUES (?, ?, ?, ?, ?)",
            (adm, name, gender if gender in ("M", "F") else None, gid, sid)))
        seen.add(adm.lower())
        added += 1
    if statements:
        db.execute_many(statements)
    flash(f"Imported {added} student(s)." + (f" Skipped {dup} duplicate admission number(s)." if dup else ""),
          "success" if added else "error")
    if bad_grade:
        flash("Grade not recognised: " + ", ".join(sorted(bad_grade)) + ". Use the exact grade names from Grades & Streams.", "error")
    if bad_stream:
        flash("Stream not found (students were added without a stream): " + ", ".join(sorted(bad_stream))
              + ". Add those streams under Grades & Streams, then edit each student.", "error")
    return redirect(back)


@analytics_bp.route("/students/template.csv")
def students_template():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", "gender", "grade", "stream"])
    w.writerow(["1001", "Amina Wanjiru", "F", "Grade 7", "Grade 7 East"])
    w.writerow(["1002", "Brian Otieno", "M", "Grade 7", "Grade 7 East"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=students_template.csv"})


@analytics_bp.route("/students/<int:student_id>")
def student_profile(student_id):
    st = db.fetch_one(
        "SELECT st.id, st.admission_no, st.name, st.gender, st.grade_id, st.stream_id, "
        "g.name AS grade_name, s.name AS stream_name FROM students st "
        "JOIN grades g ON st.grade_id = g.id LEFT JOIN streams s ON st.stream_id = s.id "
        "WHERE st.id=?", (student_id,))
    if not st:
        flash("Student not found.", "error")
        return redirect(url_for("analytics.students_page"))
    history = ea.student_history(student_id)
    chart = ""
    if history:
        chart = Markup(ea.trend_svg(
            [h["mean_pct"] for h in history],
            [f"T{h['exam']['term']} {h['exam']['name'][:10]}" for h in history]))
    return render_template(
        "analytics/student.html", st=st, history=history, chart=chart,
        streams=_grade_streams(st["grade_id"]),
    )


@analytics_bp.route("/students/<int:student_id>/update", methods=["POST"])
def student_update(student_id):
    st = db.fetch_one("SELECT grade_id FROM students WHERE id=?", (student_id,))
    if not st:
        return redirect(url_for("analytics.students_page"))
    name = request.form.get("name", "").strip()
    gender = request.form.get("gender", "").strip().upper()[:1]
    stream_id = request.form.get("stream_id", type=int)
    if not name:
        flash("A student needs a name.", "error")
        return redirect(url_for("analytics.student_profile", student_id=student_id))
    if stream_id and not db.fetch_one("SELECT id FROM streams WHERE id=? AND grade_id=?", (stream_id, st["grade_id"])):
        stream_id = None
    db.execute("UPDATE students SET name=?, gender=?, stream_id=? WHERE id=?",
               (name, gender if gender in ("M", "F") else None, stream_id, student_id))
    flash("Student updated.", "success")
    return redirect(url_for("analytics.student_profile", student_id=student_id))


@analytics_bp.route("/students/<int:student_id>/delete", methods=["POST"])
def student_delete(student_id):
    try:
        db.execute("DELETE FROM students WHERE id=?", (student_id,))
        flash("Student and all their marks removed.", "success")
    except Exception:
        # Fee invoices / payments are financial records and are never removed
        # along with a student. Withdrawing keeps everything and hides them.
        flash("This student has fee records, so they can't be deleted. Open their profile under "
              "School Management and set their status to Withdrawn instead.", "error")
    return redirect(url_for("analytics.students_page"))


# --------------------------------------------------------------------------
# Grading scales
# --------------------------------------------------------------------------
@analytics_bp.route("/grading")
def grading_page():
    scales = []
    for key, info in curriculum.SCALES.items():
        scales.append({"key": key, "title": info["title"], "bands": ea.get_bands(key),
                       "uses": "8-4-4 grades" if key == "KCSE" else "all CBC grades"})
    return render_template("analytics/grading.html", scales=scales)


@analytics_bp.route("/grading/save", methods=["POST"])
def grading_save():
    key = request.form.get("scale", "")
    if key not in curriculum.SCALES:
        return redirect(url_for("analytics.grading_page"))

    if request.form.get("action") == "reset":
        statements = []
        for label, min_pct, points, remark in curriculum.SCALES[key]["bands"]:
            statements.append(("UPDATE grading_bands SET min_pct=?, points=?, remark=? WHERE scale=? AND label=?",
                               (min_pct, points, remark, key, label)))
        db.execute_many(statements)
        flash("Defaults restored.", "success")
        return redirect(url_for("analytics.grading_page"))

    bands = ea.get_bands(key)  # highest first
    new_mins, statements = [], []
    for b in bands:
        raw = request.form.get(f"min_{b['id']}", "").replace(",", ".").strip()
        try:
            mn = float(raw)
        except ValueError:
            flash(f"“{b['label']}” needs a number for its minimum %.", "error")
            return redirect(url_for("analytics.grading_page"))
        new_mins.append(mn)
        statements.append((
            "UPDATE grading_bands SET min_pct=?, remark=? WHERE id=?",
            (mn, request.form.get(f"remark_{b['id']}", "").strip(), b["id"])))
    if any(m < 0 or m > 100 for m in new_mins) or new_mins[-1] != 0:
        flash("Minimums must be between 0 and 100, and the lowest band must start at 0.", "error")
        return redirect(url_for("analytics.grading_page"))
    if any(new_mins[i] <= new_mins[i + 1] for i in range(len(new_mins) - 1)):
        flash("Each band's minimum must be higher than the band below it.", "error")
        return redirect(url_for("analytics.grading_page"))
    db.execute_many(statements)
    flash("Grading scale saved. All exam results now use the new boundaries.", "success")
    return redirect(url_for("analytics.grading_page"))
