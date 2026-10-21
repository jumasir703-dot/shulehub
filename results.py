"""
results.py — two sections that split exam work between teachers and the office.

  /my-marks/   (service: marks_entry)    SUBJECT TEACHERS
      Each teacher sees only the subjects and classes allocated to them, types
      or uploads marks, saves drafts, and finally submits (which locks the
      sheet). Once the administrator publishes an exam, teachers can see its
      results here too.

  /results/    (service: results_publish)  ADMINISTRATOR / PUBLISHER
      Shows which subject sheets are in, lets them allocate subjects to
      teachers, reopen a sheet, preview the automatically compiled results,
      and publish or unpublish them.

The compiling itself is automatic: exam_analysis.build_results() recalculates
totals, means, grades and positions from the marks table whenever a page is
opened, so there is nothing to "run". See workflow.py for the rules.
"""

import csv
import io

from flask import (
    Blueprint, Response, flash, g, redirect, render_template, request, url_for,
)

import database as db
import exam_analysis as ea
import workflow as wf
from analytics import (
    _ADMISSION_ALIASES, _adm_key, _csv_safe, _headings, _norm, _parse_score,
    _read_csv_upload, _safe_filename, _stream_students, _upsert_mark,
)
from auth import has_service

mymarks_bp = Blueprint("mymarks", __name__, url_prefix="/my-marks")
results_bp = Blueprint("results", __name__, url_prefix="/results")

_SCORE_ALIASES = {"score", "marks", "mark", "marksobtained", "result", "results", "points", "total"}
_NAME_ALIASES = {"name", "student", "studentname", "fullname", "learner", "learnername"}

STATUS_LABEL = {wf.NOT_STARTED: "Not started", wf.IN_PROGRESS: "In progress", wf.SUBMITTED: "Submitted"}


def _forbidden():
    return render_template("auth/forbidden.html"), 403


def _teacher_id():
    return g.user["teacher_id"]


def _sheet_url(sheet, endpoint="mymarks.sheet"):
    return url_for(endpoint, exam_id=sheet["exam_id"], subject_id=sheet["subject_id"], key=sheet["stream_key"])


# =====================================================================
# TEACHERS — /my-marks
# =====================================================================
@mymarks_bp.route("/")
def index():
    tid = _teacher_id()
    groups = wf.teacher_sheets(tid) if tid else []
    n_published = len(wf.published_exam_ids())
    return render_template(
        "mymarks/index.html", groups=groups, linked=bool(tid), n_published=n_published,
        STATUS_LABEL=STATUS_LABEL, sheet_url=_sheet_url)


@mymarks_bp.route("/published")
def published():
    exams = []
    for exam_id in sorted(wf.published_exam_ids(), reverse=True):
        exam = ea.get_exam(exam_id)
        if exam:
            exams.append((exam, wf.publication(exam_id)))
    exams.sort(key=lambda t: (t[0]["year"], t[0]["term"]), reverse=True)
    return render_template("mymarks/published.html", exams=exams)


def _load(exam_id, subject_id, key):
    """-> (exam, sheet, lock_reason) or (None, None, None) when this teacher may not touch it."""
    exam = ea.get_exam(exam_id)
    tid = _teacher_id()
    if not exam or not tid:
        return None, None, None
    sheet = wf.find_sheet(exam, subject_id, key)
    if not sheet or tid not in sheet["teacher_ids"]:
        return None, None, None
    if wf.is_published(exam_id):
        lock = "published"
    elif sheet["status"] == wf.SUBMITTED:
        lock = "submitted"
    else:
        lock = None
    return exam, sheet, lock


@mymarks_bp.route("/<int:exam_id>/<int:subject_id>/<key>")
def sheet(exam_id, subject_id, key):
    exam, sh, lock = _load(exam_id, subject_id, key)
    if not sh:
        return _forbidden()
    students = _stream_students(exam, key)
    marks = {m["student_id"]: m["score"] for m in db.fetch_all(
        "SELECT student_id, score FROM marks WHERE exam_id=? AND subject_id=?", (exam_id, subject_id))}
    return render_template(
        "mymarks/sheet.html", exam=exam, sh=sh, lock=lock, students=students, marks=marks,
        STATUS_LABEL=STATUS_LABEL)


@mymarks_bp.route("/<int:exam_id>/<int:subject_id>/<key>/save", methods=["POST"])
def sheet_save(exam_id, subject_id, key):
    exam, sh, lock = _load(exam_id, subject_id, key)
    if not sh:
        return _forbidden()
    back = _sheet_url(sh)
    if lock:
        flash("This sheet is locked" + (" because the results are published." if lock == "published"
              else " — you already submitted it. Ask the administrator to reopen it if it needs changing."), "error")
        return redirect(back)

    students = _stream_students(exam, key)
    statements, saved, blank, errors = [], 0, 0, []
    for st in students:
        field = f"m_{st['id']}"
        if field not in request.form:
            continue
        status, value = _parse_score(request.form[field], sh["out_of"])
        if status == "ok":
            statements.append(_upsert_mark(exam_id, st["id"], subject_id, value))
            saved += 1
        elif status == "blank":
            statements.append(("DELETE FROM marks WHERE exam_id=? AND student_id=? AND subject_id=?",
                               (exam_id, st["id"], subject_id)))
            blank += 1
        else:
            errors.append(f"{st['name']} (0 to {sh['out_of']})")
    if errors:
        shown = "; ".join(errors[:6]) + (f" … and {len(errors) - 6} more" if len(errors) > 6 else "")
        flash(f"Not saved — {len(errors)} invalid mark(s): {shown}", "error")
        return redirect(back)

    submit = request.form.get("action") == "submit"
    if submit and saved == 0:
        flash("Enter at least one mark before submitting.", "error")
        return redirect(back)
    db.execute_many(statements)
    if submit:
        db.execute(
            "INSERT INTO mark_submissions (exam_id, subject_id, stream_key, submitted_by, submitted_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (exam_id, subject_id, stream_key) DO UPDATE SET "
            "submitted_by = excluded.submitted_by, submitted_at = excluded.submitted_at",
            (exam_id, subject_id, key, g.user["id"], wf.now()))
        flash(f"Submitted {saved} mark(s) for {sh['subject_name']} · {sh['stream_name']}. "
              "The sheet is now locked.", "success")
        return redirect(url_for("mymarks.index"))
    flash(f"Draft saved — {saved} mark(s)" + (f", {blank} left blank." if blank else ".")
          + " Remember to submit when you are finished.", "success")
    return redirect(back)


@mymarks_bp.route("/<int:exam_id>/<int:subject_id>/<key>/import", methods=["POST"])
def sheet_import(exam_id, subject_id, key):
    exam, sh, lock = _load(exam_id, subject_id, key)
    if not sh:
        return _forbidden()
    back = _sheet_url(sh)
    if lock:
        flash("This sheet is locked, so nothing was imported.", "error")
        return redirect(back)
    reader, err = _read_csv_upload()
    if err:
        flash(err, "error")
        return redirect(back)

    adm_col = next((h for h in reader.fieldnames if h and _norm(h) in _ADMISSION_ALIASES), None)
    if not adm_col:
        flash("The file needs an “admission_no” column. Headings found: " + _headings(reader)
              + ". Download the template to get the exact layout.", "error")
        return redirect(back)
    # The marks column: the one named after the subject, else “score”/“marks”,
    # else the only column left once admission number and name are set aside.
    score_col = next((h for h in reader.fieldnames if h and _norm(h) == _norm(sh["subject_name"])), None) \
        or next((h for h in reader.fieldnames if h and _norm(h) in _SCORE_ALIASES), None)
    if not score_col:
        rest = [h for h in reader.fieldnames
                if h and h != adm_col and _norm(h) not in _NAME_ALIASES and h.strip()]
        score_col = rest[0] if len(rest) == 1 else None
    if not score_col:
        flash(f"Couldn't tell which column holds the {sh['subject_name']} marks. Name it “{sh['subject_name']}” "
              "or “Score”. Headings found: " + _headings(reader), "error")
        return redirect(back)

    mine = {_adm_key(s["admission_no"]): s["id"] for s in _stream_students(exam, key)}
    statements, saved, outside, bad = [], 0, [], 0
    for row in reader:
        adm = _adm_key(row.get(adm_col))
        if not adm:
            continue
        sid = mine.get(adm)
        if sid is None:
            outside.append(adm)
            continue
        status, value = _parse_score(row.get(score_col), sh["out_of"])
        if status == "ok":
            statements.append(_upsert_mark(exam_id, sid, subject_id, value))
            saved += 1
        elif status == "bad":
            bad += 1
    if bad:
        flash(f"Nothing imported — {bad} value(s) were not numbers from 0 to {sh['out_of']}. "
              "Fix the file and upload it again.", "error")
        return redirect(back)
    if not saved:
        flash("Nothing was imported — no marks were found for the students in " + sh["stream_name"] + ".", "error")
        return redirect(back)
    db.execute_many(statements)
    flash(f"Imported {saved} mark(s) as a draft. Check them below, then submit.", "success")
    if outside:
        flash(f"{len(set(outside))} admission number(s) are not in {sh['stream_name']} and were skipped: "
              + ", ".join(sorted(set(outside))[:8]), "error")
    return redirect(back)


@mymarks_bp.route("/<int:exam_id>/<int:subject_id>/<key>/template.csv")
def sheet_template(exam_id, subject_id, key):
    exam, sh, lock = _load(exam_id, subject_id, key)
    if not sh:
        return _forbidden()
    marks = {m["student_id"]: m["score"] for m in db.fetch_all(
        "SELECT student_id, score FROM marks WHERE exam_id=? AND subject_id=?", (exam_id, subject_id))}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", sh["subject_name"]])
    for st in _stream_students(exam, key):
        w.writerow([_csv_safe(st["admission_no"]), _csv_safe(st["name"]),
                    f"{marks[st['id']]:g}" if st["id"] in marks else ""])
    title = f"{exam['name']} {sh['subject_name']} {sh['stream_name']} marks"
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename={_safe_filename(title, 'csv')}"})


# =====================================================================
# ADMINISTRATOR / PUBLISHER — /results
# =====================================================================
def _exam_or_none(exam_id):
    exam = ea.get_exam(exam_id)
    if not exam:
        flash("That exam no longer exists.", "error")
    return exam


@results_bp.route("/")
def index():
    rows = []
    published = wf.published_exam_ids()
    for e in db.fetch_all("SELECT id FROM exams ORDER BY year DESC, term DESC, id DESC"):
        exam = ea.get_exam(e["id"])
        sheets = wf.sheets_for_exam(exam)
        rows.append({"exam": exam, "progress": wf.progress(sheets), "published": exam["id"] in published})
    return render_template("results/index.html", rows=rows)


@results_bp.route("/<int:exam_id>")
def exam_page(exam_id):
    exam = _exam_or_none(exam_id)
    if not exam:
        return redirect(url_for("results.index"))
    sheets = wf.sheets_for_exam(exam)
    by_stream = {}
    for s in sheets:
        by_stream.setdefault((s["stream_key"], s["stream_name"]), []).append(s)
    res = ea.build_results(exam_id)
    ranked = res["ranked"] if res else []
    grade_mean = round(sum(s["mean_pct"] for s in ranked) / len(ranked), 2) if ranked else None
    return render_template(
        "results/exam.html", exam=exam, by_stream=by_stream, progress=wf.progress(sheets),
        published=wf.publication(exam_id), n_ranked=len(ranked),
        n_students=len(res["students"]) if res else 0, grade_mean=grade_mean,
        STATUS_LABEL=STATUS_LABEL, can_edit_marks=has_service(g.user, "exams"))


@results_bp.route("/<int:exam_id>/publish", methods=["POST"])
def publish(exam_id):
    exam = _exam_or_none(exam_id)
    if not exam:
        return redirect(url_for("results.index"))
    sheets = wf.sheets_for_exam(exam)
    prog = wf.progress(sheets)
    if not any(s["n_entered"] for s in sheets):
        flash("There are no marks in this exam yet, so there is nothing to publish.", "error")
    elif not prog["complete"] and not request.form.get("force"):
        flash(f"{prog['total'] - prog['submitted']} of {prog['total']} mark sheets have not been submitted. "
              "Wait for the teachers, or tick “Publish anyway”.", "error")
    else:
        wf.publish(exam_id, g.user["id"])
        flash(f"Results for {exam['name']} ({exam['grade_name']}) are published. Marks are now locked "
              "and teachers can view the results.", "success")
    return redirect(url_for("results.exam_page", exam_id=exam_id))


@results_bp.route("/<int:exam_id>/unpublish", methods=["POST"])
def unpublish(exam_id):
    if _exam_or_none(exam_id):
        wf.unpublish(exam_id)
        flash("Results are no longer published. Teachers can't see them and unlocked sheets can be edited again.",
              "success")
    return redirect(url_for("results.exam_page", exam_id=exam_id))


@results_bp.route("/<int:exam_id>/reopen", methods=["POST"])
def reopen(exam_id):
    subject_id = request.form.get("subject_id", type=int)
    key = request.form.get("stream_key", "")
    if wf.is_published(exam_id):
        flash("Unpublish the results first, then reopen the sheet.", "error")
    else:
        db.execute("DELETE FROM mark_submissions WHERE exam_id=? AND subject_id=? AND stream_key=?",
                   (exam_id, subject_id, key))
        flash("Sheet reopened — the teacher can edit and submit it again.", "success")
    return redirect(url_for("results.exam_page", exam_id=exam_id))


# ------------------------------------------------------------- allocations
@results_bp.route("/allocations")
def allocations():
    teacher_id = request.args.get("teacher_id", type=int)
    teachers = db.fetch_all("SELECT id, name FROM teachers ORDER BY name")
    subjects = db.fetch_all(
        "SELECT sub.id, sub.name, g.name AS grade_name, g.id AS grade_id FROM subjects sub "
        "JOIN grades g ON g.id = sub.grade_id ORDER BY g.sort_order, g.id, sub.name")
    streams = db.fetch_all(
        "SELECT st.id, st.name, st.grade_id, g.name AS grade_name FROM streams st "
        "JOIN grades g ON g.id = st.grade_id ORDER BY g.sort_order, g.id, st.name")
    linked = {r["teacher_id"] for r in db.fetch_all("SELECT teacher_id FROM users WHERE teacher_id IS NOT NULL")}
    return render_template(
        "results/allocations.html", rows=wf.list_allocations(teacher_id), teachers=teachers,
        subjects=subjects, streams=streams, teacher_id=teacher_id, linked=linked,
        n_timetable=db.fetch_one("SELECT COUNT(*) AS c FROM timetable_entries WHERE teacher_id IS NOT NULL")["c"])


@results_bp.route("/allocations/add", methods=["POST"])
def allocation_add():
    ok, msg = wf.add_allocation(
        request.form.get("teacher_id", type=int), request.form.get("subject_id", type=int),
        request.form.get("stream_id", type=int) or 0)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("results.allocations", teacher_id=request.form.get("teacher_id", type=int)))


@results_bp.route("/allocations/<int:alloc_id>/delete", methods=["POST"])
def allocation_delete(alloc_id):
    db.execute("DELETE FROM subject_allocations WHERE id=?", (alloc_id,))
    return redirect(url_for("results.allocations", teacher_id=request.form.get("teacher_id", type=int)))


@results_bp.route("/allocations/from-timetable", methods=["POST"])
def allocation_from_timetable():
    n = wf.fill_from_timetable()
    flash(f"Added {n} allocation(s) from the timetable." if n else
          "Nothing new to add — every teacher in the timetable is already allocated.", "success" if n else "error")
    return redirect(url_for("results.allocations"))
