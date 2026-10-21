"""
school.py — School Management: student records, guardians and promotion.

    /school/students                register (filter by grade / stream / status)
    /school/students/new            admit a student (with up to two guardians)
    /school/students/<id>           profile: details, guardians, attendance, fees, exams
    /school/students/import         CSV import (with guardian columns)
    /school/students/directory.csv  export the register with guardian contacts
    /school/promotion               end-of-year promotion / repeat / graduate

Students live in the same `students` table the exam analysis already uses,
so a student admitted here is immediately available for marks entry.
`students.active` stays 1 only while status == 'active'; every existing exam
query already filters on it, so graduated or transferred students drop out
of new class lists automatically.
"""

import csv
import io
import re
from datetime import date, datetime

from flask import (
    Blueprint, Response, flash, g, redirect, render_template, request, url_for,
)

import database as db
from auth import has_service
from analytics import _csv_safe, _find_column, _norm, _read_csv_upload
from fees import student_balance, student_fee_summary

school_bp = Blueprint("school", __name__, url_prefix="/school")

STATUSES = {
    "active": "Active", "graduated": "Graduated",
    "transferred": "Transferred out", "withdrawn": "Withdrawn",
}
RELATIONSHIPS = ["Mother", "Father", "Guardian", "Grandparent", "Sibling", "Aunt/Uncle", "Other"]


# ---------------------------------------------------------------- helpers
def _iso(value):
    """Return a YYYY-MM-DD string for a valid date, else None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _phone(value):
    value = re.sub(r"[^\d+]", "", value or "")
    return value or None


def _who():
    return g.user["username"] if getattr(g, "user", None) else None


def _grades():
    return db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")


def _streams():
    return db.fetch_all(
        "SELECT s.id, s.name, s.grade_id FROM streams s JOIN grades g ON s.grade_id = g.id "
        "ORDER BY g.sort_order, s.name")


def _next_admission_no():
    top = 0
    for r in db.fetch_all("SELECT admission_no FROM students"):
        m = re.fullmatch(r"\s*(\d+)\s*", r["admission_no"] or "")
        if m:
            top = max(top, int(m.group(1)))
    return str(top + 1) if top else ""


def _label(grade_name, stream_name):
    return f"{grade_name} · {stream_name}" if stream_name else (grade_name or "")


def _valid_stream(stream_id, grade_id):
    if stream_id and db.fetch_one("SELECT id FROM streams WHERE id=? AND grade_id=?", (stream_id, grade_id)):
        return stream_id
    return None


def _insert_guardian_stmt(student_id, name, relationship, phone, email, primary):
    return ("INSERT INTO guardians (student_id, name, relationship, phone, email, is_primary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (student_id, name, relationship or None, _phone(phone), (email or "").strip() or None,
             1 if primary else 0))


# ---------------------------------------------------------------- register
def _student_rows(grade_id=None, stream_id=None, status="active", q=""):
    query = (
        "SELECT st.id, st.admission_no, st.name, st.gender, st.status, st.dob, g.name AS grade_name, "
        "s.name AS stream_name, "
        "(SELECT gu.name FROM guardians gu WHERE gu.student_id = st.id "
        " ORDER BY gu.is_primary DESC, gu.id LIMIT 1) AS guardian_name, "
        "(SELECT gu.phone FROM guardians gu WHERE gu.student_id = st.id "
        " ORDER BY gu.is_primary DESC, gu.id LIMIT 1) AS guardian_phone "
        "FROM students st JOIN grades g ON st.grade_id = g.id "
        "LEFT JOIN streams s ON st.stream_id = s.id WHERE 1=1"
    )
    params = []
    if status in STATUSES:
        query += " AND st.status = ?"
        params.append(status)
    if grade_id:
        query += " AND st.grade_id = ?"
        params.append(grade_id)
    if stream_id:
        query += " AND st.stream_id = ?"
        params.append(stream_id)
    if q:
        like = f"%{q.lower()}%"
        query += (" AND (LOWER(st.name) LIKE ? OR LOWER(st.admission_no) LIKE ? OR EXISTS ("
                  "SELECT 1 FROM guardians gu WHERE gu.student_id = st.id AND "
                  "(LOWER(gu.name) LIKE ? OR gu.phone LIKE ?)))")
        params += [like, like, like, f"%{q}%"]
    query += " ORDER BY g.sort_order, s.name, st.name"
    return db.fetch_all(query, tuple(params))


@school_bp.route("/students")
def students():
    grade_id = request.args.get("grade_id", type=int)
    stream_id = request.args.get("stream_id", type=int)
    status = request.args.get("status", "active")
    q = request.args.get("q", "").strip()
    rows = _student_rows(grade_id, stream_id, status, q)
    counts = {r["status"]: r["c"] for r in db.fetch_all(
        "SELECT status, COUNT(*) AS c FROM students GROUP BY status")}
    return render_template(
        "school/students.html", students=rows[:500], total=len(rows), grades=_grades(),
        streams=_streams(), grade_id=grade_id, stream_id=stream_id, status=status, q=q,
        statuses=STATUSES, counts=counts)


@school_bp.route("/students/directory.csv")
def directory_csv():
    rows = _student_rows(
        request.args.get("grade_id", type=int), request.args.get("stream_id", type=int),
        request.args.get("status", "active"), request.args.get("q", "").strip())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", "gender", "grade", "stream", "status", "guardian", "guardian_phone"])
    for r in rows:
        w.writerow([_csv_safe(x) for x in (
            r["admission_no"], r["name"], r["gender"] or "", r["grade_name"],
            r["stream_name"] or "", r["status"], r["guardian_name"] or "", r["guardian_phone"] or "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=students.csv"})


# ---------------------------------------------------------------- admit
@school_bp.route("/students/new", methods=["GET", "POST"])
def student_new():
    if request.method == "POST":
        f = request.form
        adm = f.get("admission_no", "").strip()
        name = f.get("name", "").strip()
        grade_id = f.get("grade_id", type=int)
        gender = f.get("gender", "").strip().upper()[:1]
        dob = _iso(f.get("dob"))
        adm_date = _iso(f.get("admission_date")) or date.today().isoformat()
        error = None
        if not adm or not name or not grade_id:
            error = "Admission number, name and grade are required."
        elif f.get("dob", "").strip() and not dob:
            error = "Date of birth isn't a valid date."
        elif not db.fetch_one("SELECT id FROM grades WHERE id=?", (grade_id,)):
            error = "Choose a grade."
        if error:
            flash(error, "error")
            return render_template(
                "school/student_new.html", grades=_grades(), streams=_streams(),
                next_adm=adm, relationships=RELATIONSHIPS, form=f), 400
        stream_id = _valid_stream(f.get("stream_id", type=int), grade_id)
        try:
            sid = db.execute(
                "INSERT INTO students (admission_no, name, gender, grade_id, stream_id, dob, "
                "admission_date, upi, status, notes, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 1)",
                (adm, name, gender if gender in ("M", "F") else None, grade_id, stream_id, dob,
                 adm_date, f.get("upi", "").strip() or None, f.get("notes", "").strip() or None))
        except Exception:
            flash(f"Admission number {adm} is already in use.", "error")
            return render_template(
                "school/student_new.html", grades=_grades(), streams=_streams(),
                next_adm=adm, relationships=RELATIONSHIPS, form=f), 400
        stmts = []
        for i in (1, 2):
            gname = f.get(f"g{i}_name", "").strip()
            if gname:
                stmts.append(_insert_guardian_stmt(
                    sid, gname, f.get(f"g{i}_rel"), f.get(f"g{i}_phone"), f.get(f"g{i}_email"), i == 1))
        if stmts:
            db.execute_many(stmts)
        flash(f"Admitted {name} (Adm. {adm}).", "success")
        return redirect(url_for("school.student_detail", student_id=sid))
    return render_template(
        "school/student_new.html", grades=_grades(), streams=_streams(),
        next_adm=_next_admission_no(), relationships=RELATIONSHIPS, form={})


# ---------------------------------------------------------------- profile
def _attendance_summary(student_id):
    rows = db.fetch_all(
        "SELECT status, COUNT(*) AS c FROM attendance WHERE student_id=? AND att_date >= ? GROUP BY status",
        (student_id, date(date.today().year, 1, 1).isoformat()))
    counts = {r["status"]: r["c"] for r in rows}
    marked = sum(counts.values())
    present = counts.get("P", 0) + counts.get("L", 0)
    recent_abs = db.fetch_all(
        "SELECT att_date, status, note FROM attendance WHERE student_id=? AND status IN ('A','E') "
        "ORDER BY att_date DESC LIMIT 8", (student_id,))
    return {
        "counts": counts, "marked": marked,
        "pct": round(100 * present / marked, 1) if marked else None,
        "recent_absences": recent_abs,
    }


@school_bp.route("/students/<int:student_id>")
def student_detail(student_id):
    st = db.fetch_one(
        "SELECT st.*, g.name AS grade_name, s.name AS stream_name FROM students st "
        "JOIN grades g ON st.grade_id = g.id LEFT JOIN streams s ON st.stream_id = s.id WHERE st.id=?",
        (student_id,))
    if not st:
        flash("Student not found.", "error")
        return redirect(url_for("school.students"))
    guardians = db.fetch_all(
        "SELECT * FROM guardians WHERE student_id=? ORDER BY is_primary DESC, id", (student_id,))
    history = db.fetch_all(
        "SELECT * FROM promotion_log WHERE student_id=? ORDER BY id DESC", (student_id,))
    attendance = _attendance_summary(student_id) if has_service(g.user, "attendance") else None
    fees = student_fee_summary(student_id) if has_service(g.user, "fees") else None
    return render_template(
        "school/student.html", st=dict(st), guardians=guardians, history=history,
        attendance=attendance, fees=fees, grades=_grades(),
        streams=db.fetch_all("SELECT id, name FROM streams WHERE grade_id=? ORDER BY name", (st["grade_id"],)),
        statuses=STATUSES, relationships=RELATIONSHIPS)


@school_bp.route("/students/<int:student_id>/edit", methods=["POST"])
def student_edit(student_id):
    st = db.fetch_one("SELECT * FROM students WHERE id=?", (student_id,))
    if not st:
        return redirect(url_for("school.students"))
    f = request.form
    name = f.get("name", "").strip()
    gender = f.get("gender", "").strip().upper()[:1]
    dob = _iso(f.get("dob"))
    adm_date = _iso(f.get("admission_date"))
    if not name:
        flash("A student needs a name.", "error")
        return redirect(url_for("school.student_detail", student_id=student_id))
    if f.get("dob", "").strip() and not dob:
        flash("Date of birth isn't a valid date.", "error")
        return redirect(url_for("school.student_detail", student_id=student_id))
    grade_id = f.get("grade_id", type=int) or st["grade_id"]
    if not db.fetch_one("SELECT id FROM grades WHERE id=?", (grade_id,)):
        grade_id = st["grade_id"]
    stream_id = _valid_stream(f.get("stream_id", type=int), grade_id)
    db.execute(
        "UPDATE students SET name=?, gender=?, dob=?, admission_date=?, upi=?, notes=?, grade_id=?, stream_id=? "
        "WHERE id=?",
        (name, gender if gender in ("M", "F") else None, dob, adm_date,
         f.get("upi", "").strip() or None, f.get("notes", "").strip() or None,
         grade_id, stream_id, student_id))
    flash("Student updated.", "success")
    return redirect(url_for("school.student_detail", student_id=student_id))


@school_bp.route("/students/<int:student_id>/status", methods=["POST"])
def student_status(student_id):
    st = db.fetch_one(
        "SELECT st.id, st.name, st.grade_id, st.stream_id, g.name AS grade_name, s.name AS stream_name "
        "FROM students st JOIN grades g ON st.grade_id = g.id LEFT JOIN streams s ON st.stream_id = s.id "
        "WHERE st.id=?", (student_id,))
    new = request.form.get("status", "")
    if not st or new not in STATUSES:
        return redirect(url_for("school.students"))
    db.execute_many([
        ("UPDATE students SET status=?, active=? WHERE id=?", (new, 1 if new == "active" else 0, student_id)),
        ("INSERT INTO promotion_log (student_id, action, from_grade_id, from_stream_id, from_label, to_label, "
         "logged_on, logged_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
         (student_id, new if new != "active" else "reactivated", st["grade_id"], st["stream_id"],
          _label(st["grade_name"], st["stream_name"]), STATUSES[new], date.today().isoformat(), _who())),
    ])
    flash(f"{st['name']} marked as {STATUSES[new].lower()}.", "success")
    return redirect(url_for("school.student_detail", student_id=student_id))


# ---------------------------------------------------------------- guardians
@school_bp.route("/students/<int:student_id>/guardians/add", methods=["POST"])
def guardian_add(student_id):
    f = request.form
    name = f.get("name", "").strip()
    if not name or not db.fetch_one("SELECT id FROM students WHERE id=?", (student_id,)):
        flash("A guardian needs a name.", "error")
        return redirect(url_for("school.student_detail", student_id=student_id))
    first = db.fetch_one("SELECT COUNT(*) AS c FROM guardians WHERE student_id=?", (student_id,))["c"] == 0
    db.execute_many([_insert_guardian_stmt(
        student_id, name, f.get("relationship"), f.get("phone"), f.get("email"), first)])
    flash("Guardian added.", "success")
    return redirect(url_for("school.student_detail", student_id=student_id))


@school_bp.route("/guardians/<int:guardian_id>/delete", methods=["POST"])
def guardian_delete(guardian_id):
    gu = db.fetch_one("SELECT student_id, is_primary FROM guardians WHERE id=?", (guardian_id,))
    if gu:
        db.execute("DELETE FROM guardians WHERE id=?", (guardian_id,))
        if gu["is_primary"]:
            nxt = db.fetch_one("SELECT id FROM guardians WHERE student_id=? ORDER BY id LIMIT 1", (gu["student_id"],))
            if nxt:
                db.execute("UPDATE guardians SET is_primary=1 WHERE id=?", (nxt["id"],))
        flash("Guardian removed.", "success")
        return redirect(url_for("school.student_detail", student_id=gu["student_id"]))
    return redirect(url_for("school.students"))


@school_bp.route("/guardians/<int:guardian_id>/primary", methods=["POST"])
def guardian_primary(guardian_id):
    gu = db.fetch_one("SELECT student_id FROM guardians WHERE id=?", (guardian_id,))
    if gu:
        db.execute_many([
            ("UPDATE guardians SET is_primary=0 WHERE student_id=?", (gu["student_id"],)),
            ("UPDATE guardians SET is_primary=1 WHERE id=?", (guardian_id,)),
        ])
        return redirect(url_for("school.student_detail", student_id=gu["student_id"]))
    return redirect(url_for("school.students"))


# ---------------------------------------------------------------- import
@school_bp.route("/students/template.csv")
def students_template():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", "gender", "grade", "stream", "dob", "admission_date", "upi",
                "guardian_name", "guardian_relationship", "guardian_phone", "guardian_email"])
    w.writerow(["1001", "Amina Wanjiru", "F", "Grade 7", "Grade 7 East", "2013-03-14", "2026-01-05", "",
                "Grace Wanjiru", "Mother", "0712345678", ""])
    w.writerow(["1002", "Brian Otieno", "M", "Grade 7", "Grade 7 East", "2012-11-02", "2026-01-05", "",
                "Peter Otieno", "Father", "0722000111", ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=students_template.csv"})


@school_bp.route("/students/import", methods=["POST"])
def students_import():
    back = url_for("school.students")
    reader, err = _read_csv_upload()
    if err:
        flash(err, "error")
        return redirect(back)
    cols = {
        "adm": _find_column(reader.fieldnames, "admission_no", "adm", "admno", "admission"),
        "name": _find_column(reader.fieldnames, "name", "fullname", "studentname"),
        "grade": _find_column(reader.fieldnames, "grade", "class", "form"),
        "stream": _find_column(reader.fieldnames, "stream"),
        "gender": _find_column(reader.fieldnames, "gender", "sex"),
        "dob": _find_column(reader.fieldnames, "dob", "dateofbirth", "birthdate"),
        "adm_date": _find_column(reader.fieldnames, "admission_date", "admitted", "dateadmitted"),
        "upi": _find_column(reader.fieldnames, "upi", "nemis", "nemisupi"),
        "g_name": _find_column(reader.fieldnames, "guardian_name", "guardian", "parent", "parentname"),
        "g_rel": _find_column(reader.fieldnames, "guardian_relationship", "relationship"),
        "g_phone": _find_column(reader.fieldnames, "guardian_phone", "phone", "parentphone", "guardianphone"),
        "g_email": _find_column(reader.fieldnames, "guardian_email", "email"),
    }
    if not (cols["adm"] and cols["name"] and cols["grade"]):
        flash("The CSV needs admission_no, name and grade columns. Download the sample file to see the layout.", "error")
        return redirect(back)

    grades = {_norm(x["name"]): x["id"] for x in db.fetch_all("SELECT id, name FROM grades")}
    streams = {(s["grade_id"], _norm(s["name"])): s["id"] for s in db.fetch_all("SELECT id, name, grade_id FROM streams")}
    existing = {r["admission_no"].lower() for r in db.fetch_all("SELECT admission_no FROM students")}

    def val(row, key):
        return (row.get(cols[key]) or "").strip() if cols[key] else ""

    stmts, guardians, seen = [], {}, set()
    added = dup = 0
    bad_grade, bad_stream, bad_dob = set(), set(), 0
    for row in reader:
        adm, name = val(row, "adm"), val(row, "name")
        if not adm or not name:
            continue
        if adm.lower() in existing or adm.lower() in seen:
            dup += 1
            continue
        gid = grades.get(_norm(val(row, "grade")))
        if not gid:
            bad_grade.add(val(row, "grade") or "(blank)")
            continue
        sid = None
        if val(row, "stream"):
            sid = streams.get((gid, _norm(val(row, "stream"))))
            if sid is None:
                bad_stream.add(val(row, "stream"))
        dob = _iso(val(row, "dob"))
        if val(row, "dob") and not dob:
            bad_dob += 1
        gender = val(row, "gender").upper()[:1]
        stmts.append((
            "INSERT INTO students (admission_no, name, gender, grade_id, stream_id, dob, admission_date, upi, "
            "status, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1)",
            (adm, name, gender if gender in ("M", "F") else None, gid, sid, dob,
             _iso(val(row, "adm_date")) or date.today().isoformat(), val(row, "upi") or None)))
        if val(row, "g_name"):
            guardians[adm.lower()] = (val(row, "g_name"), val(row, "g_rel"), val(row, "g_phone"), val(row, "g_email"))
        seen.add(adm.lower())
        added += 1
    if stmts:
        db.execute_many(stmts)
        if guardians:
            ids = {r["admission_no"].lower(): r["id"] for r in db.fetch_all("SELECT id, admission_no FROM students")}
            db.execute_many([
                _insert_guardian_stmt(ids[a], n, rel, ph, em, True)
                for a, (n, rel, ph, em) in guardians.items() if a in ids])
    flash(f"Imported {added} student(s)." + (f" Skipped {dup} duplicate admission number(s)." if dup else ""),
          "success" if added else "error")
    if bad_grade:
        flash("Grade not recognised: " + ", ".join(sorted(bad_grade)) + ". Use the exact names from Grades & Streams.", "error")
    if bad_stream:
        flash("Stream not found (added without a stream): " + ", ".join(sorted(bad_stream)), "error")
    if bad_dob:
        flash(f"{bad_dob} date(s) of birth weren't understood and were left blank. Use YYYY-MM-DD.", "error")
    return redirect(back)


# ---------------------------------------------------------------- promotion
def _suggest_stream(src_stream_name, src_grade_name, target_grade_name, target_streams):
    """Best-guess the matching stream in the next grade ('Grade 7 East' -> 'Grade 8 East')."""
    guess = _norm(src_stream_name.replace(src_grade_name, target_grade_name))
    for s in target_streams:
        if _norm(s["name"]) == guess:
            return s["id"]
    suffix = _norm(src_stream_name.replace(src_grade_name, ""))
    if suffix:
        for s in target_streams:
            if _norm(s["name"]).endswith(suffix):
                return s["id"]
    return None


ACTIONS = [
    ("promote", "Promote"), ("repeat", "Repeat"), ("graduate", "Graduate"),
    ("transfer", "Transfer out"), ("withdraw", "Withdraw"),
]


@school_bp.route("/promotion", methods=["GET", "POST"])
def promote():
    grades = _grades()
    grade_id = (request.form if request.method == "POST" else request.args).get("grade_id", type=int)
    src = next((x for x in grades if x["id"] == grade_id), None)
    if not src:
        return render_template("school/promote.html", grades=grades, src=None)
    idx = [x["id"] for x in grades].index(src["id"])
    default_target = grades[idx + 1] if idx + 1 < len(grades) else None

    students_in = db.fetch_all(
        "SELECT st.id, st.admission_no, st.name, st.stream_id, s.name AS stream_name "
        "FROM students st LEFT JOIN streams s ON st.stream_id = s.id "
        "WHERE st.grade_id=? AND st.status='active' ORDER BY s.name, st.name", (src["id"],))

    if request.method == "POST":
        target_id = request.form.get("target_grade_id", type=int)
        target = next((x for x in grades if x["id"] == target_id and x["id"] != src["id"]), None)
        stmts, tally = [], {}
        today = date.today().isoformat()
        src_streams = {s["id"]: s["name"] for s in db.fetch_all("SELECT id, name FROM streams WHERE grade_id=?", (src["id"],))}
        stream_map = {}
        if target:
            valid = {s["id"] for s in db.fetch_all("SELECT id FROM streams WHERE grade_id=?", (target["id"],))}
            for sid in src_streams:
                chosen = request.form.get(f"map_{sid}", type=int)
                stream_map[sid] = chosen if chosen in valid else None
        for s in students_in:
            action = request.form.get(f"act_{s['id']}", "promote")
            if action not in dict(ACTIONS):
                action = "promote"
            if action == "promote" and not target:
                action = "graduate"
            from_label = _label(src["name"], s["stream_name"])
            base = (s["id"], action if action != "promote" else "promoted", src["id"], s["stream_id"], from_label)
            if action == "promote":
                new_stream = stream_map.get(s["stream_id"])
                stmts.append(("UPDATE students SET grade_id=?, stream_id=? WHERE id=?", (target["id"], new_stream, s["id"])))
                to_label = target["name"]
            elif action == "repeat":
                to_label = f"{src['name']} (repeat)"
            else:
                status = {"graduate": "graduated", "transfer": "transferred", "withdraw": "withdrawn"}[action]
                stmts.append(("UPDATE students SET status=?, active=0 WHERE id=?", (status, s["id"])))
                to_label = STATUSES[status]
            stmts.append((
                "INSERT INTO promotion_log (student_id, action, from_grade_id, from_stream_id, from_label, to_label, "
                "logged_on, logged_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                base + (to_label, today, _who())))
            tally[action] = tally.get(action, 0) + 1
        if stmts:
            db.execute_many(stmts)
        parts = [f"{n} {dict(ACTIONS)[a].lower()}" for a, n in tally.items()]
        flash(f"{src['name']} done: " + (", ".join(parts) or "no students") + ".", "success")
        return redirect(url_for("school.promote"))

    target_streams = db.fetch_all("SELECT id, name FROM streams WHERE grade_id=? ORDER BY name", (default_target["id"],)) if default_target else []
    src_streams = db.fetch_all("SELECT id, name FROM streams WHERE grade_id=? ORDER BY name", (src["id"],))
    suggestions = {s["id"]: (_suggest_stream(s["name"], src["name"], default_target["name"], target_streams) if default_target else None)
                   for s in src_streams}
    all_streams = _streams()
    return render_template(
        "school/promote.html", grades=grades, src=src, target=default_target, students=students_in,
        src_streams=src_streams, target_streams=target_streams, suggestions=suggestions,
        actions=ACTIONS, all_streams=all_streams)
