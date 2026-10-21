"""
records.py — Student records module.

    /students/                       register (search, filter by grade/stream/status)
    /students/new                    admit a student (+ up to two guardians)
    /students/<id>                   profile: details, guardians, siblings, attendance
    /students/import                 CSV import (creates new, completes existing)
    /students/export.csv             CSV export of the current filter
    /students/attendance             take a class register for a date
    /students/attendance/report      attendance % per student, absentees, registers not taken

The `students` table is shared with the exam module (analytics.py): a student
added here appears there and vice versa. `students.active` is kept in step with
`students.status` (anything other than 'active' => active = 0), because the exam
code only looks at `active`.

Promotion between grades is deliberately NOT here yet: exam results are located
through a student's *current* grade, so moving students up would empty the
merit lists of earlier exams. It needs exam rosters to be remembered per exam first.
"""

import csv
import io
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, Response, flash, g, redirect, render_template, request, url_for,
)

import database as db

records_bp = Blueprint("records", __name__, url_prefix="/students")

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
STATUSES = {
    "active": "Active",
    "transferred": "Transferred out",
    "withdrawn": "Withdrawn",
    "completed": "Completed / alumni",
}
ATT_CODES = {"P": "Present", "A": "Absent", "L": "Late", "E": "Excused"}
BOARDING = {"day": "Day scholar", "boarding": "Boarder"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def school_today():
    """Today's date in the school's time zone (default Nairobi). A server on
    UTC would otherwise be a day behind until 03:00 local time."""
    try:
        from zoneinfo import ZoneInfo
        import os
        return datetime.now(ZoneInfo(os.environ.get("SCHOOL_TZ", "Africa/Nairobi"))).date()
    except Exception:
        return date.today()


def parse_date(raw):
    """'2014-03-09' or '09/03/2014' (day first) -> '2014-03-09'; else None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def normalize_phone(raw):
    """Kenyan-style numbers -> +2547XXXXXXXX. Anything unrecognised is kept
    as typed (digits and a leading +) so nothing is silently lost."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if not s:
        return ""
    if s.startswith("+"):
        return s
    if s.startswith("254") and len(s) == 12:
        return "+" + s
    if s.startswith("0") and len(s) == 10:
        return "+254" + s[1:]
    if len(s) == 9 and s[0] in "71":
        return "+254" + s
    return s


def _clean(v):
    return (v or "").strip()


def _gender(v):
    v = _clean(v).upper()[:1]
    return v if v in ("M", "F") else None


def _boarding(v):
    return "boarding" if _clean(v).lower() in ("boarding", "boarder", "b", "yes", "y") else "day"


@contextmanager
def _tx():
    """One connection/transaction for multi-statement writes."""
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _q(cur, query, params=()):
    cur.execute(db._adapt(query), params)
    return cur


def _find_guardian(cur, phone):
    if not phone:
        return None
    row = _q(cur, "SELECT id FROM guardians WHERE phone=?", (phone,)).fetchone()
    return row["id"] if row else None


def _link_guardian(cur, student_id, name, phone, relationship="", email="", alt_phone="", address=""):
    """Attach a guardian to a student. A guardian already on file with the same
    phone number (e.g. a sibling's parent) is reused, not duplicated."""
    name, relationship = _clean(name), _clean(relationship)
    phone, email = normalize_phone(phone), _clean(email)
    if not name and not phone:
        return None
    gid = _find_guardian(cur, phone)
    if gid is None:
        gid = db._insert_and_get_id(
            cur, "INSERT INTO guardians (name, phone, alt_phone, email, address) VALUES (?, ?, ?, ?, ?)",
            (name or phone, phone or None, normalize_phone(alt_phone) or None, email or None,
             _clean(address) or None))
    has_primary = _q(cur, "SELECT 1 AS x FROM student_guardians WHERE student_id=? AND is_primary=1",
                     (student_id,)).fetchone()
    _q(cur, "INSERT INTO student_guardians (student_id, guardian_id, relationship, is_primary) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (student_id, guardian_id) DO NOTHING",
       (student_id, gid, relationship or None, 0 if has_primary else 1))
    return gid


def set_student_status(cur, student_id, status, when=None):
    """Keep `status` and the legacy `active` flag in step."""
    if status not in STATUSES:
        status = "active"
    _q(cur, "UPDATE students SET status=?, active=?, status_date=? WHERE id=?",
       (status, 1 if status == "active" else 0, None if status == "active" else (when or school_today().isoformat()),
        student_id))


def _grades():
    return db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")


def _all_streams():
    return db.fetch_all(
        "SELECT s.id, s.name, s.grade_id FROM streams s JOIN grades g ON s.grade_id=g.id "
        "ORDER BY g.sort_order, s.name")


def _stream_ok(stream_id, grade_id):
    if not stream_id:
        return None
    ok = db.fetch_one("SELECT id FROM streams WHERE id=? AND grade_id=?", (stream_id, grade_id))
    return stream_id if ok else None


_LIST_SQL = (
    "SELECT st.id, st.admission_no, st.name, st.gender, st.status, st.grade_id, st.stream_id, "
    "st.date_of_birth, st.upi, st.boarding, g.name AS grade_name, s.name AS stream_name, "
    "(SELECT gu.name FROM student_guardians sg JOIN guardians gu ON gu.id=sg.guardian_id "
    " WHERE sg.student_id=st.id ORDER BY sg.is_primary DESC, gu.id LIMIT 1) AS guardian_name, "
    "(SELECT gu.phone FROM student_guardians sg JOIN guardians gu ON gu.id=sg.guardian_id "
    " WHERE sg.student_id=st.id ORDER BY sg.is_primary DESC, gu.id LIMIT 1) AS guardian_phone "
    "FROM students st JOIN grades g ON st.grade_id=g.id LEFT JOIN streams s ON st.stream_id=s.id"
)


def _filtered_students():
    grade_id = request.args.get("grade_id", type=int)
    stream_id = request.args.get("stream_id", type=int)
    status = request.args.get("status", "active")
    q = request.args.get("q", "").strip().lower()
    where, params = [], []
    if grade_id:
        where.append("st.grade_id=?")
        params.append(grade_id)
    if stream_id:
        where.append("st.stream_id=?")
        params.append(stream_id)
    if status in STATUSES:
        where.append("st.status=?")
        params.append(status)
    if q:
        where.append("(LOWER(st.name) LIKE ? OR LOWER(st.admission_no) LIKE ? OR LOWER(COALESCE(st.upi,'')) LIKE ?)")
        params += [f"%{q}%"] * 3
    sql = _LIST_SQL + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY g.sort_order, s.name, st.name"
    return db.fetch_all(sql, tuple(params)), dict(grade_id=grade_id, stream_id=stream_id, status=status, q=q)


# --------------------------------------------------------------------------
# Register
# --------------------------------------------------------------------------
@records_bp.route("/")
def students_page():
    rows, f = _filtered_students()
    counts = {r["status"]: r["c"] for r in db.fetch_all(
        "SELECT COALESCE(status,'active') AS status, COUNT(*) AS c FROM students GROUP BY COALESCE(status,'active')")}
    return render_template(
        "records/students.html", students=rows[:500], total=len(rows), grades=_grades(),
        streams=_all_streams(), statuses=STATUSES, counts=counts, **f)


@records_bp.route("/export.csv")
def students_export():
    rows, _ = _filtered_students()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["admission_no", "name", "gender", "grade", "stream", "status", "date_of_birth",
                "upi", "boarding", "guardian_name", "guardian_phone"])
    for r in rows:
        w.writerow([r["admission_no"], r["name"], r["gender"] or "", r["grade_name"], r["stream_name"] or "",
                    r["status"], r["date_of_birth"] or "", r["upi"] or "", r["boarding"] or "",
                    r["guardian_name"] or "", r["guardian_phone"] or ""])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=students.csv"})


# --------------------------------------------------------------------------
# Admit a student
# --------------------------------------------------------------------------
@records_bp.route("/new", methods=["GET", "POST"])
def student_new():
    grades, streams = _grades(), _all_streams()
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        adm = _clean(form.get("admission_no"))
        name = _clean(form.get("name"))
        grade_id = form.get("grade_id", type=int)
        error = None
        if not adm or not name or not grade_id:
            error = "Admission number, name and grade are required."
        elif not db.fetch_one("SELECT id FROM grades WHERE id=?", (grade_id,)):
            error = "Choose a grade."
        elif db.fetch_one("SELECT id FROM students WHERE LOWER(admission_no)=?", (adm.lower(),)):
            error = f"Admission number {adm} already exists."
        dob = parse_date(form.get("date_of_birth"))
        adm_date = parse_date(form.get("admission_date")) or school_today().isoformat()
        if not error and _clean(form.get("date_of_birth")) and not dob:
            error = "Date of birth isn't a valid date."
        if error:
            flash(error, "error")
        else:
            stream_id = _stream_ok(form.get("stream_id", type=int), grade_id)
            with _tx() as cur:
                sid = db._insert_and_get_id(
                    cur,
                    "INSERT INTO students (admission_no, name, gender, grade_id, stream_id, date_of_birth, "
                    "admission_date, upi, residence, boarding, notes, status, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1)",
                    (adm, name, _gender(form.get("gender")), grade_id, stream_id, dob, adm_date,
                     _clean(form.get("upi")) or None, _clean(form.get("residence")) or None,
                     _boarding(form.get("boarding")), _clean(form.get("notes")) or None))
                for n in ("1", "2"):
                    _link_guardian(cur, sid, form.get(f"g{n}_name"), form.get(f"g{n}_phone"),
                                   form.get(f"g{n}_rel"), form.get(f"g{n}_email"))
            flash(f"{name} admitted.", "success")
            return redirect(url_for("records.student_detail", student_id=sid))
    last = db.fetch_one("SELECT admission_no FROM students ORDER BY id DESC LIMIT 1")
    return render_template(
        "records/student_new.html", grades=grades, streams=streams, f=form,
        last_adm=last["admission_no"] if last else None, today=school_today().isoformat())


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------
def _get_student(student_id):
    return db.fetch_one(
        "SELECT st.*, g.name AS grade_name, s.name AS stream_name FROM students st "
        "JOIN grades g ON st.grade_id=g.id LEFT JOIN streams s ON st.stream_id=s.id WHERE st.id=?",
        (student_id,))


def _missing():
    flash("Student not found.", "error")
    return redirect(url_for("records.students_page"))


@records_bp.route("/<int:student_id>")
def student_detail(student_id):
    st = _get_student(student_id)
    if not st:
        return _missing()
    guardians = db.fetch_all(
        "SELECT gu.*, sg.relationship, sg.is_primary FROM student_guardians sg "
        "JOIN guardians gu ON gu.id=sg.guardian_id WHERE sg.student_id=? "
        "ORDER BY sg.is_primary DESC, gu.id", (student_id,))
    siblings = db.fetch_all(
        "SELECT DISTINCT s2.id, s2.name, s2.admission_no, g.name AS grade_name FROM student_guardians a "
        "JOIN student_guardians b ON a.guardian_id=b.guardian_id AND b.student_id<>a.student_id "
        "JOIN students s2 ON s2.id=b.student_id JOIN grades g ON g.id=s2.grade_id "
        "WHERE a.student_id=? AND s2.active=1 ORDER BY s2.name", (student_id,))
    since = (school_today() - timedelta(days=30)).isoformat()
    att = db.fetch_one(
        "SELECT COUNT(*) AS days, "
        "COALESCE(SUM(CASE WHEN status='P' THEN 1 ELSE 0 END),0) AS p, "
        "COALESCE(SUM(CASE WHEN status='L' THEN 1 ELSE 0 END),0) AS l, "
        "COALESCE(SUM(CASE WHEN status='A' THEN 1 ELSE 0 END),0) AS a, "
        "COALESCE(SUM(CASE WHEN status='E' THEN 1 ELSE 0 END),0) AS e "
        "FROM attendance WHERE student_id=? AND att_date>=?", (student_id, since))
    recent_absences = db.fetch_all(
        "SELECT att_date, status, note FROM attendance WHERE student_id=? AND status IN ('A','E') "
        "ORDER BY att_date DESC LIMIT 8", (student_id,))
    has_marks = db.fetch_one("SELECT 1 AS x FROM marks WHERE student_id=? LIMIT 1", (student_id,)) is not None
    streams = [s for s in _all_streams() if s["grade_id"] == st["grade_id"]]
    return render_template(
        "records/student.html", st=st, guardians=guardians, siblings=siblings, att=att,
        recent_absences=recent_absences, has_marks=has_marks, streams=streams, grades=_grades(),
        statuses=STATUSES, att_codes=ATT_CODES, today=school_today().isoformat())


@records_bp.route("/<int:student_id>/update", methods=["POST"])
def student_update(student_id):
    st = _get_student(student_id)
    if not st:
        return _missing()
    f = request.form
    name = _clean(f.get("name"))
    adm = _clean(f.get("admission_no"))
    back = redirect(url_for("records.student_detail", student_id=student_id))
    if not name or not adm:
        flash("A student needs a name and an admission number.", "error")
        return back
    clash = db.fetch_one("SELECT id FROM students WHERE LOWER(admission_no)=? AND id<>?", (adm.lower(), student_id))
    if clash:
        flash(f"Admission number {adm} belongs to another student.", "error")
        return back
    dob = parse_date(f.get("date_of_birth"))
    if _clean(f.get("date_of_birth")) and not dob:
        flash("Date of birth isn't a valid date.", "error")
        return back
    adm_date = parse_date(f.get("admission_date")) if _clean(f.get("admission_date")) else None

    grade_id = f.get("grade_id", type=int) or st["grade_id"]
    if grade_id != st["grade_id"]:
        has_marks = db.fetch_one("SELECT 1 AS x FROM marks WHERE student_id=? LIMIT 1", (student_id,))
        if has_marks or not db.fetch_one("SELECT id FROM grades WHERE id=?", (grade_id,)):
            flash("The grade can't be changed once a student has exam marks (it would pull them out of "
                  "their exams). Fix the grade before marks are entered, or contact the administrator.", "error")
            return back
    stream_id = _stream_ok(f.get("stream_id", type=int), grade_id)
    db.execute(
        "UPDATE students SET admission_no=?, name=?, gender=?, grade_id=?, stream_id=?, date_of_birth=?, "
        "admission_date=?, upi=?, residence=?, boarding=?, notes=? WHERE id=?",
        (adm, name, _gender(f.get("gender")), grade_id, stream_id, dob, adm_date,
         _clean(f.get("upi")) or None, _clean(f.get("residence")) or None, _boarding(f.get("boarding")),
         _clean(f.get("notes")) or None, student_id))
    flash("Student updated.", "success")
    return back


@records_bp.route("/<int:student_id>/status", methods=["POST"])
def student_status(student_id):
    if not _get_student(student_id):
        return _missing()
    status = request.form.get("status", "active")
    if status not in STATUSES:
        flash("Unknown status.", "error")
    else:
        when = parse_date(request.form.get("status_date")) or school_today().isoformat()
        with _tx() as cur:
            set_student_status(cur, student_id, status, when)
        flash(f"Marked as {STATUSES[status].lower()}.", "success")
    return redirect(url_for("records.student_detail", student_id=student_id))


@records_bp.route("/<int:student_id>/delete", methods=["POST"])
def student_delete(student_id):
    st = _get_student(student_id)
    if st:
        db.execute("DELETE FROM students WHERE id=?", (student_id,))
        flash(f"{st['name']} and all their marks and attendance were deleted.", "success")
    return redirect(url_for("records.students_page"))


# --------------------------------------------------------------------------
# Guardians
# --------------------------------------------------------------------------
@records_bp.route("/<int:student_id>/guardians/add", methods=["POST"])
def guardian_add(student_id):
    if not _get_student(student_id):
        return _missing()
    f = request.form
    if not _clean(f.get("name")) and not _clean(f.get("phone")):
        flash("Enter the guardian's name or phone number.", "error")
    else:
        with _tx() as cur:
            _link_guardian(cur, student_id, f.get("name"), f.get("phone"), f.get("relationship"),
                           f.get("email"), f.get("alt_phone"), f.get("address"))
        flash("Guardian added.", "success")
    return redirect(url_for("records.student_detail", student_id=student_id))


@records_bp.route("/<int:student_id>/guardians/<int:guardian_id>/update", methods=["POST"])
def guardian_update(student_id, guardian_id):
    f = request.form
    link = db.fetch_one("SELECT 1 AS x FROM student_guardians WHERE student_id=? AND guardian_id=?",
                        (student_id, guardian_id))
    name = _clean(f.get("name"))
    if not link or not name:
        flash("Guardian not found, or the name was empty.", "error")
    else:
        statements = [
            ("UPDATE guardians SET name=?, phone=?, alt_phone=?, email=?, address=? WHERE id=?",
             (name, normalize_phone(f.get("phone")) or None, normalize_phone(f.get("alt_phone")) or None,
              _clean(f.get("email")) or None, _clean(f.get("address")) or None, guardian_id)),
            ("UPDATE student_guardians SET relationship=? WHERE student_id=? AND guardian_id=?",
             (_clean(f.get("relationship")) or None, student_id, guardian_id)),
        ]
        if f.get("is_primary"):
            statements.insert(0, ("UPDATE student_guardians SET is_primary=0 WHERE student_id=?", (student_id,)))
            statements.append(("UPDATE student_guardians SET is_primary=1 WHERE student_id=? AND guardian_id=?",
                               (student_id, guardian_id)))
        db.execute_many(statements)
        flash("Guardian updated (this also updates any brothers or sisters linked to the same guardian).", "success")
    return redirect(url_for("records.student_detail", student_id=student_id))


@records_bp.route("/<int:student_id>/guardians/<int:guardian_id>/remove", methods=["POST"])
def guardian_remove(student_id, guardian_id):
    with _tx() as cur:
        _q(cur, "DELETE FROM student_guardians WHERE student_id=? AND guardian_id=?", (student_id, guardian_id))
        left = _q(cur, "SELECT guardian_id FROM student_guardians WHERE guardian_id=? LIMIT 1", (guardian_id,)).fetchone()
        if not left:      # nobody else uses this guardian record
            _q(cur, "DELETE FROM guardians WHERE id=?", (guardian_id,))
        has_primary = _q(cur, "SELECT 1 AS x FROM student_guardians WHERE student_id=? AND is_primary=1",
                         (student_id,)).fetchone()
        if not has_primary:
            nxt = _q(cur, "SELECT guardian_id FROM student_guardians WHERE student_id=? ORDER BY guardian_id LIMIT 1",
                     (student_id,)).fetchone()
            if nxt:
                _q(cur, "UPDATE student_guardians SET is_primary=1 WHERE student_id=? AND guardian_id=?",
                   (student_id, nxt["guardian_id"]))
    flash("Guardian removed from this student.", "success")
    return redirect(url_for("records.student_detail", student_id=student_id))


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------
_ALIASES = {
    "admission_no": ("admission_no", "admission_number", "adm_no", "adm", "admno", "admission"),
    "name": ("name", "full_name", "student_name", "student"),
    "gender": ("gender", "sex"),
    "grade": ("grade", "class", "form"),
    "stream": ("stream",),
    "date_of_birth": ("date_of_birth", "dob", "birth_date", "birthdate"),
    "admission_date": ("admission_date", "date_admitted", "date_of_admission"),
    "upi": ("upi", "nemis_upi", "nemis", "nemis_no"),
    "residence": ("residence", "address", "home", "location"),
    "boarding": ("boarding", "boarder", "day_boarding"),
    "g1_name": ("guardian_name", "guardian", "parent_name", "parent", "guardian1_name"),
    "g1_phone": ("guardian_phone", "parent_phone", "phone", "guardian1_phone"),
    "g1_email": ("guardian_email", "parent_email", "email", "guardian1_email"),
    "g1_rel": ("relationship", "guardian_relationship", "guardian1_relationship"),
    "g2_name": ("guardian2_name", "guardian_2_name", "second_guardian"),
    "g2_phone": ("guardian2_phone", "guardian_2_phone"),
    "g2_rel": ("guardian2_relationship", "guardian_2_relationship"),
}


def _header_key(h):
    return re.sub(r"[^a-z0-9]+", "_", (h or "").lower()).strip("_")


def _map_row(raw):
    clean = {_header_key(k): v for k, v in raw.items() if k}
    out = {}
    for field, names in _ALIASES.items():
        for n in names:
            if n in clean and _clean(clean[n]):
                out[field] = _clean(clean[n])
                break
    return out


def _lookup_grade(value, by_name):
    v = value.lower()
    for cand in (v, f"grade {v}", f"form {v}"):
        if cand in by_name:
            return by_name[cand]
    return None


@records_bp.route("/import", methods=["POST"])
def students_import():
    back = redirect(url_for("records.students_page"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return back
    data = file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        flash("That file is over 2 MB. Split it into smaller files.", "error")
        return back
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    rows = [_map_row(r) for r in reader]
    if not rows:
        flash("The file has no rows.", "error")
        return back

    grades = {g["name"].lower(): g["id"] for g in _grades()}
    streams = {}
    for s in db.fetch_all("SELECT id, name, grade_id FROM streams"):
        streams[(s["grade_id"], s["name"].lower())] = s["id"]

    created = updated = 0
    errors = []
    try:
        with _tx() as cur:
            for i, r in enumerate(rows, start=2):       # row 1 is the header
                adm, name = r.get("admission_no"), r.get("name")
                if not adm or not name:
                    errors.append(f"Row {i}: admission_no and name are required.")
                    continue
                dob = parse_date(r.get("date_of_birth")) if r.get("date_of_birth") else None
                if r.get("date_of_birth") and not dob:
                    errors.append(f"Row {i} ({adm}): date of birth “{r['date_of_birth']}” isn't a valid date.")
                    continue
                adm_date = parse_date(r.get("admission_date")) if r.get("admission_date") else None
                existing = _q(cur, "SELECT id, grade_id FROM students WHERE LOWER(admission_no)=?",
                              (adm.lower(),)).fetchone()
                if existing:
                    sid, grade_id = existing["id"], existing["grade_id"]   # grade of an existing student never changes here
                    sets, params = ["name=?"], [name]
                    for col, val in (("date_of_birth", dob), ("admission_date", adm_date),
                                     ("upi", r.get("upi")), ("residence", r.get("residence"))):
                        if val:
                            sets.append(f"{col}=?")
                            params.append(val)
                    if _gender(r.get("gender")):
                        sets.append("gender=?")
                        params.append(_gender(r.get("gender")))
                    if r.get("boarding"):
                        sets.append("boarding=?")
                        params.append(_boarding(r.get("boarding")))
                    if r.get("stream"):
                        sid_stream = streams.get((grade_id, r["stream"].lower()))
                        if sid_stream:
                            sets.append("stream_id=?")
                            params.append(sid_stream)
                    _q(cur, f"UPDATE students SET {', '.join(sets)} WHERE id=?", tuple(params + [sid]))
                    updated += 1
                else:
                    grade_id = _lookup_grade(r.get("grade", ""), grades) if r.get("grade") else None
                    if not grade_id:
                        errors.append(f"Row {i} ({adm}): grade “{r.get('grade', '')}” not found — it must match "
                                      f"a name on Grades & Streams.")
                        continue
                    stream_id = streams.get((grade_id, r["stream"].lower())) if r.get("stream") else None
                    if r.get("stream") and not stream_id:
                        errors.append(f"Row {i} ({adm}): stream “{r['stream']}” doesn't exist in that grade.")
                        continue
                    sid = db._insert_and_get_id(
                        cur,
                        "INSERT INTO students (admission_no, name, gender, grade_id, stream_id, date_of_birth, "
                        "admission_date, upi, residence, boarding, status, active) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1)",
                        (adm, name, _gender(r.get("gender")), grade_id, stream_id, dob,
                         adm_date or school_today().isoformat(), r.get("upi"), r.get("residence"),
                         _boarding(r.get("boarding"))))
                    created += 1
                for n in ("1", "2"):
                    if r.get(f"g{n}_name") or r.get(f"g{n}_phone"):
                        _link_guardian(cur, sid, r.get(f"g{n}_name"), r.get(f"g{n}_phone"),
                                       r.get(f"g{n}_rel"), r.get(f"g{n}_email"))
    except Exception as exc:       # nothing from the file is kept if the database rejects a row
        flash(f"Import stopped and nothing was saved: {exc}", "error")
        return back

    flash(f"Import finished: {created} added, {updated} updated"
          + (f", {len(errors)} row(s) skipped." if errors else "."), "success" if not errors else "error")
    if errors:
        shown = " ".join(errors[:8]) + (f" … and {len(errors) - 8} more." if len(errors) > 8 else "")
        flash(shown, "error")
    return back


@records_bp.route("/template.csv")
def students_template():
    sample = (
        "admission_no,name,gender,grade,stream,date_of_birth,admission_date,upi,residence,boarding,"
        "guardian_name,guardian_phone,guardian_email,relationship,guardian2_name,guardian2_phone,guardian2_relationship\n"
        "1001,Achieng Otieno,F,Grade 7,East,2013-05-14,2024-01-08,,Nakuru,day,"
        "Jane Otieno,0712345678,jane@example.com,Mother,Peter Otieno,0722333444,Father\n"
    )
    return Response("\ufeff" + sample, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=students_template.csv"})


# --------------------------------------------------------------------------
# Attendance
# --------------------------------------------------------------------------
def _classes():
    """Every (grade, stream) that has active students, as picker options."""
    rows = db.fetch_all(
        "SELECT st.grade_id, st.stream_id, g.name AS grade_name, s.name AS stream_name, COUNT(*) AS n "
        "FROM students st JOIN grades g ON g.id=st.grade_id LEFT JOIN streams s ON s.id=st.stream_id "
        "WHERE st.active=1 GROUP BY st.grade_id, st.stream_id, g.name, s.name, g.sort_order "
        "ORDER BY g.sort_order, s.name")
    out = []
    for r in rows:
        out.append({
            "key": f"{r['grade_id']}:{r['stream_id'] or 0}",
            "grade_id": r["grade_id"], "stream_id": r["stream_id"] or 0,
            "label": f"{r['grade_name']}" + (f" {r['stream_name']}" if r["stream_name"] else ""),
            "n": r["n"],
        })
    return out


def _parse_class(key, classes):
    for c in classes:
        if c["key"] == key:
            return c
    return classes[0] if classes else None


def _class_students(c):
    if c["stream_id"]:
        return db.fetch_all(
            "SELECT id, admission_no, name FROM students WHERE active=1 AND grade_id=? AND stream_id=? ORDER BY name",
            (c["grade_id"], c["stream_id"]))
    return db.fetch_all(
        "SELECT id, admission_no, name FROM students WHERE active=1 AND grade_id=? AND stream_id IS NULL ORDER BY name",
        (c["grade_id"],))


def _att_date(raw):
    d = parse_date(raw) or school_today().isoformat()
    return min(d, school_today().isoformat())          # no registers for the future


@records_bp.route("/attendance")
def attendance_page():
    classes = _classes()
    c = _parse_class(request.args.get("class", ""), classes)
    day = _att_date(request.args.get("date"))
    students, existing = [], {}
    if c:
        students = _class_students(c)
        for r in db.fetch_all(
                "SELECT student_id, status, note FROM attendance WHERE att_date=? AND student_id IN "
                "(SELECT id FROM students WHERE active=1 AND grade_id=?)", (day, c["grade_id"])):
            existing[r["student_id"]] = r
    weekday = datetime.strptime(day, "%Y-%m-%d").strftime("%A")
    return render_template(
        "records/attendance.html", classes=classes, c=c, day=day, students=students, existing=existing,
        taken=bool(existing) and any(s["id"] in existing for s in students),
        off_day=weekday not in db.get_active_days(), weekday=weekday,
        today=school_today().isoformat(), codes=ATT_CODES)


@records_bp.route("/attendance/save", methods=["POST"])
def attendance_save():
    classes = _classes()
    c = _parse_class(request.form.get("class", ""), classes)
    day = parse_date(request.form.get("date"))
    if not c or not day or day > school_today().isoformat():
        flash("Pick a class and a date that is not in the future.", "error")
        return redirect(url_for("records.attendance_page"))
    marker = g.user["id"] if g.get("user") and g.user["id"] else None
    statements, counts = [], {k: 0 for k in ATT_CODES}
    for s in _class_students(c):
        code = request.form.get(f"s_{s['id']}", "P")
        if code not in ATT_CODES:
            code = "P"
        note = _clean(request.form.get(f"n_{s['id']}"))[:200] or None
        counts[code] += 1
        statements.append((
            "INSERT INTO attendance (student_id, att_date, status, note, marked_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (student_id, att_date) DO UPDATE SET status=excluded.status, note=excluded.note, "
            "marked_by=excluded.marked_by",
            (s["id"], day, code, note, marker)))
    if statements:
        db.execute_many(statements)
    flash(f"Register saved for {c['label']}, {day}: {counts['P']} present, {counts['L']} late, "
          f"{counts['A']} absent, {counts['E']} excused.", "success")
    return redirect(url_for("records.attendance_page", **{"class": c["key"], "date": day}))


def _report_data():
    classes = _classes()
    key = request.args.get("class", "")
    c = next((x for x in classes if x["key"] == key), None)     # None = whole school
    today = school_today()
    start = parse_date(request.args.get("from")) or (today - timedelta(days=29)).isoformat()
    end = parse_date(request.args.get("to")) or today.isoformat()
    if start > end:
        start, end = end, start
    where, params = ["st.active=1"], [start, end]
    if c:
        where.append("st.grade_id=?")
        params.append(c["grade_id"])
        if c["stream_id"]:
            where.append("st.stream_id=?")
            params.append(c["stream_id"])
        else:
            where.append("st.stream_id IS NULL")
    rows = db.fetch_all(
        "SELECT st.id, st.admission_no, st.name, g.name AS grade_name, s.name AS stream_name, "
        "COUNT(a.id) AS days, "
        "COALESCE(SUM(CASE WHEN a.status='P' THEN 1 ELSE 0 END),0) AS p, "
        "COALESCE(SUM(CASE WHEN a.status='L' THEN 1 ELSE 0 END),0) AS l, "
        "COALESCE(SUM(CASE WHEN a.status='A' THEN 1 ELSE 0 END),0) AS a, "
        "COALESCE(SUM(CASE WHEN a.status='E' THEN 1 ELSE 0 END),0) AS e "
        "FROM students st JOIN grades g ON g.id=st.grade_id LEFT JOIN streams s ON s.id=st.stream_id "
        "LEFT JOIN attendance a ON a.student_id=st.id AND a.att_date>=? AND a.att_date<=? "
        "WHERE " + " AND ".join(where) +
        " GROUP BY st.id, st.admission_no, st.name, g.name, g.sort_order, s.name "
        "ORDER BY g.sort_order, s.name, st.name", tuple(params))
    table = []
    for r in rows:
        d = dict(r)
        d["pct"] = round((d["p"] + d["l"]) * 100.0 / d["days"], 1) if d["days"] else None
        table.append(d)
    return classes, c, start, end, table


@records_bp.route("/attendance/report")
def attendance_report():
    classes, c, start, end, table = _report_data()
    day = _att_date(request.args.get("day"))
    absent = db.fetch_all(
        "SELECT st.id, st.name, a.status, a.note, g.name AS grade_name, s.name AS stream_name, "
        "(SELECT gu.phone FROM student_guardians sg JOIN guardians gu ON gu.id=sg.guardian_id "
        " WHERE sg.student_id=st.id ORDER BY sg.is_primary DESC, gu.id LIMIT 1) AS guardian_phone "
        "FROM attendance a JOIN students st ON st.id=a.student_id JOIN grades g ON g.id=st.grade_id "
        "LEFT JOIN streams s ON s.id=st.stream_id WHERE a.att_date=? AND a.status IN ('A','E') AND st.active=1 "
        "ORDER BY g.sort_order, s.name, st.name", (day,))
    marked = {(r["grade_id"], r["stream_id"] or 0) for r in db.fetch_all(
        "SELECT DISTINCT st.grade_id, st.stream_id FROM attendance a JOIN students st ON st.id=a.student_id "
        "WHERE a.att_date=? AND st.active=1", (day,))}
    not_taken = [x for x in classes if (x["grade_id"], x["stream_id"]) not in marked]
    day_counts = {r["status"]: r["c"] for r in db.fetch_all(
        "SELECT a.status, COUNT(*) AS c FROM attendance a JOIN students st ON st.id=a.student_id "
        "WHERE a.att_date=? AND st.active=1 GROUP BY a.status", (day,))}
    low = [t for t in table if t["pct"] is not None and t["pct"] < 80]
    return render_template(
        "records/attendance_report.html", classes=classes, c=c, start=start, end=end, table=table,
        day=day, absent=absent, not_taken=not_taken, day_counts=day_counts, low_count=len(low),
        today=school_today().isoformat(), codes=ATT_CODES)


@records_bp.route("/attendance/report.csv")
def attendance_report_csv():
    _, c, start, end, table = _report_data()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["admission_no", "name", "grade", "stream", "days_recorded", "present", "late", "absent",
                "excused", "attendance_pct"])
    for t in table:
        w.writerow([t["admission_no"], t["name"], t["grade_name"], t["stream_name"] or "", t["days"], t["p"],
                    t["l"], t["a"], t["e"], "" if t["pct"] is None else t["pct"]])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=attendance_{start}_to_{end}.csv"})
