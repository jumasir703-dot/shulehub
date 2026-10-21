"""
exam_flow.py — the rules behind "teachers enter their own marks, an administrator
compiles and publishes". No Flask code, so it can be tested on its own.

Vocabulary
----------
sheet        One subject for one class (stream) in one exam, e.g.
             "Mathematics · Grade 7 East · Mid-Term". This is the unit a teacher
             fills in and submits. Its identity is (exam_id, subject_id, stream_key).
stream_key   The stream's id as text ("12"), or "none" for students with no stream.
allocation   "Teacher T teaches subject S in stream X" (stream 0 = every stream of
             S's grade). Set by the administrator on the Subject Allocations page,
             usually by copying the timetable. Only allocated teachers can enter
             that sheet's marks.
submitted    The teacher is finished; the sheet is locked until an administrator
             reopens it.
published    The administrator released the exam's results. Every sheet of the
             exam is then locked for teachers and teachers can see the results.

Compiling needs no step of its own: totals, means, grades and positions are
recalculated from the marks table by exam_analysis.build_results() every time
a page is opened, so the compiled results are always current.
"""

from datetime import datetime

import database as db
import exam_analysis as ea

NOT_STARTED, IN_PROGRESS, SUBMITTED = "not_started", "in_progress", "submitted"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- publication
def is_published(exam_id):
    return db.fetch_one("SELECT 1 AS x FROM exam_publications WHERE exam_id=?", (exam_id,)) is not None


def publication(exam_id):
    return db.fetch_one(
        "SELECT p.published_at, u.full_name AS published_by_name FROM exam_publications p "
        "LEFT JOIN users u ON u.id = p.published_by WHERE p.exam_id=?", (exam_id,))


def publish(exam_id, user_id):
    db.execute(
        "INSERT INTO exam_publications (exam_id, published_at, published_by) VALUES (?, ?, ?) "
        "ON CONFLICT (exam_id) DO UPDATE SET published_at = excluded.published_at, "
        "published_by = excluded.published_by", (exam_id, now(), user_id))


def unpublish(exam_id):
    db.execute("DELETE FROM exam_publications WHERE exam_id=?", (exam_id,))


def published_exam_ids():
    return {r["exam_id"] for r in db.fetch_all("SELECT exam_id FROM exam_publications")}


# ---------------------------------------------------------------- streams
def stream_key(stream_id):
    return "none" if stream_id is None else str(stream_id)


def exam_streams(exam):
    """Streams of the exam's grade that have active students (plus 'No stream')."""
    streams = db.fetch_all(
        "SELECT id, name, pathway_id FROM streams WHERE grade_id=? ORDER BY name", (exam["grade_id"],))
    counts = {r["stream_id"]: r["c"] for r in db.fetch_all(
        "SELECT stream_id, COUNT(*) AS c FROM students WHERE grade_id=? AND active=1 GROUP BY stream_id",
        (exam["grade_id"],))}
    out = []
    for s in streams:
        if counts.get(s["id"]):
            out.append({"key": str(s["id"]), "id": s["id"], "name": s["name"],
                        "pathway_id": s["pathway_id"], "n": counts[s["id"]]})
    if counts.get(None):
        out.append({"key": "none", "id": None, "name": "No stream", "pathway_id": None, "n": counts[None]})
    return out


# ---------------------------------------------------------------- sheets
def sheets_for_exam(exam):
    """Every (subject × stream) sheet the exam needs, with progress, status and
    the teachers allocated to it. Streams without students are skipped, and a
    pathway stream only gets core subjects plus its own pathway's subjects —
    the same rule the marks grid uses."""
    exam_id = exam["id"]
    subjects = ea.exam_subjects(exam_id)
    if not subjects:
        return []
    streams = exam_streams(exam)

    entered = {}
    for r in db.fetch_all(
            "SELECT m.subject_id, s.stream_id, COUNT(*) AS c FROM marks m "
            "JOIN students s ON s.id = m.student_id "
            "WHERE m.exam_id=? AND s.active=1 AND s.grade_id=? GROUP BY m.subject_id, s.stream_id",
            (exam_id, exam["grade_id"])):
        entered[(r["subject_id"], stream_key(r["stream_id"]))] = r["c"]

    submitted = {(r["subject_id"], r["stream_key"]): r for r in db.fetch_all(
        "SELECT ms.subject_id, ms.stream_key, ms.submitted_at, u.full_name AS submitted_by_name "
        "FROM mark_submissions ms LEFT JOIN users u ON u.id = ms.submitted_by WHERE ms.exam_id=?",
        (exam_id,))}

    ids = [s["id"] for s in subjects]
    marks_in = ",".join("?" for _ in ids)
    allocs = db.fetch_all(
        "SELECT a.teacher_id, a.subject_id, a.stream_id, t.name FROM subject_allocations a "
        f"JOIN teachers t ON t.id = a.teacher_id WHERE a.subject_id IN ({marks_in}) ORDER BY t.name",
        tuple(ids))

    sheets = []
    for st in streams:
        for sub in subjects:
            if st["pathway_id"] is not None and sub["pathway_id"] is not None \
                    and sub["pathway_id"] != st["pathway_id"]:
                continue
            key = (sub["id"], st["key"])
            who = [a for a in allocs if a["subject_id"] == sub["id"]
                   and (a["stream_id"] == 0 or str(a["stream_id"]) == st["key"])]
            sent = submitted.get(key)
            n_in = entered.get(key, 0)
            sheets.append({
                "exam_id": exam_id, "subject_id": sub["id"], "subject_name": sub["name"], "out_of": sub["out_of"],
                "stream_key": st["key"], "stream_name": st["name"], "n_students": st["n"], "n_entered": n_in,
                "status": SUBMITTED if sent else (IN_PROGRESS if n_in else NOT_STARTED),
                "submitted_at": sent["submitted_at"] if sent else None,
                "submitted_by_name": sent["submitted_by_name"] if sent else None,
                "teacher_ids": {a["teacher_id"] for a in who},
                "teachers": sorted({a["name"] for a in who}),
            })
    return sheets


def find_sheet(exam, subject_id, key):
    for s in sheets_for_exam(exam):
        if s["subject_id"] == subject_id and s["stream_key"] == key:
            return s
    return None


def progress(sheets):
    total = len(sheets)
    submitted = sum(1 for s in sheets if s["status"] == SUBMITTED)
    return {
        "total": total, "submitted": submitted,
        "started": sum(1 for s in sheets if s["status"] == IN_PROGRESS),
        "no_teacher": sum(1 for s in sheets if not s["teachers"]),
        "pct": round(100 * submitted / total) if total else 0,
        "complete": bool(total) and submitted == total,
    }


def open_exams():
    """Exams still collecting marks (not yet published), newest first."""
    rows = db.fetch_all(
        "SELECT e.id FROM exams e WHERE NOT EXISTS (SELECT 1 FROM exam_publications p WHERE p.exam_id = e.id) "
        "ORDER BY e.year DESC, e.term DESC, e.id DESC")
    return [ea.get_exam(r["id"]) for r in rows]


def teacher_sheets(teacher_id):
    """[(exam, [sheets this teacher is allocated to])] for exams still open."""
    out = []
    for exam in open_exams():
        mine = [s for s in sheets_for_exam(exam) if teacher_id in s["teacher_ids"]]
        if mine:
            out.append((exam, mine))
    return out


# ---------------------------------------------------------------- allocations
def list_allocations(teacher_id=None):
    sql = (
        "SELECT a.id, a.teacher_id, a.subject_id, a.stream_id, t.name AS teacher_name, "
        "sub.name AS subject_name, g.name AS grade_name, st.name AS stream_name "
        "FROM subject_allocations a JOIN teachers t ON t.id = a.teacher_id "
        "JOIN subjects sub ON sub.id = a.subject_id JOIN grades g ON g.id = sub.grade_id "
        "LEFT JOIN streams st ON st.id = a.stream_id")
    params = ()
    if teacher_id:
        sql += " WHERE a.teacher_id = ?"
        params = (teacher_id,)
    sql += " ORDER BY t.name, g.sort_order, g.id, sub.name, a.stream_id"
    return db.fetch_all(sql, params)


def add_allocation(teacher_id, subject_id, stream_id):
    """-> (ok, message). stream_id 0 = every stream of the subject's grade."""
    sub = db.fetch_one("SELECT grade_id FROM subjects WHERE id=?", (subject_id,))
    if not db.fetch_one("SELECT 1 AS x FROM teachers WHERE id=?", (teacher_id,)) or not sub:
        return False, "Choose a teacher and a subject."
    if stream_id:
        st = db.fetch_one("SELECT grade_id FROM streams WHERE id=?", (stream_id,))
        if not st or st["grade_id"] != sub["grade_id"]:
            return False, "That stream isn't in the same grade as the subject."
    if db.fetch_one("SELECT 1 AS x FROM subject_allocations WHERE teacher_id=? AND subject_id=? AND stream_id=?",
                    (teacher_id, subject_id, stream_id or 0)):
        return False, "That allocation already exists."
    db.execute("INSERT INTO subject_allocations (teacher_id, subject_id, stream_id) VALUES (?, ?, ?)",
               (teacher_id, subject_id, stream_id or 0))
    return True, "Allocation added."


def fill_from_timetable():
    """Create an allocation for every teacher/subject/stream already in the
    timetable, skipping ones that exist (or are covered by an 'all streams' one)."""
    before = db.fetch_one("SELECT COUNT(*) AS c FROM subject_allocations")["c"]
    db.execute(
        "INSERT INTO subject_allocations (teacher_id, subject_id, stream_id) "
        "SELECT DISTINCT te.teacher_id, te.subject_id, te.stream_id FROM timetable_entries te "
        "WHERE te.teacher_id IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM subject_allocations a WHERE a.teacher_id = te.teacher_id "
        "  AND a.subject_id = te.subject_id AND (a.stream_id = te.stream_id OR a.stream_id = 0))")
    return db.fetch_one("SELECT COUNT(*) AS c FROM subject_allocations")["c"] - before
