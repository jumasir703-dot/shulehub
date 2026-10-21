"""
terms.py
Term-by-term timetables.

The live timetable (timetable_entries) is always the CURRENT term's. When a
new term starts, the current one is archived into timetable_history (one row
per lesson: class, subject, teacher, day, period, clock time) and, if wanted,
a fresh timetable is generated.

Rotation: when generating, the scheduler avoids giving a teacher the same
class + subject in the same day + lesson number as in the previous archived
term (a preference, relaxed only when nothing else fits). See scheduler.py.

Pages (Timetable > Terms & Rotation): see the current term, start a new term
(archives the current timetable, then regenerates a rotated one or keeps the
current lessons), switch rotation on/off, and see how many lessons of each
class still repeat last term.

Exam analysis uses the archived timetable of an exam's own term to find
"who taught this subject", so old results stay correct after the timetable
changes.
"""

from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

import database as db

terms_bp = Blueprint("terms", __name__, url_prefix="/terms")
_tables_ready = False

ROTATE_KEY = "rotate_from_last_term"


# --------------------------------------------------------------------------
def apply(conn, use_pg):
    pk = "SERIAL PRIMARY KEY" if use_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cur = conn.cursor()
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS timetable_history (
            id {pk},
            year INTEGER NOT NULL,
            term INTEGER NOT NULL,
            stream_id INTEGER NOT NULL,
            stream_name TEXT,
            grade_name TEXT,
            subject_id INTEGER NOT NULL,
            subject_name TEXT,
            teacher_id INTEGER,
            teacher_name TEXT,
            day TEXT NOT NULL,
            period_number INTEGER NOT NULL,
            start_time TEXT,
            end_time TEXT
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tt_hist_term ON timetable_history (year, term)")
    conn.commit()


def ensure_tables():
    """Create the history table on first use (also done at startup by apply())."""
    global _tables_ready
    if _tables_ready:
        return
    for pk in ("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"):
        try:
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS timetable_history (
                    id {pk}, year INTEGER NOT NULL, term INTEGER NOT NULL, stream_id INTEGER NOT NULL,
                    stream_name TEXT, grade_name TEXT, subject_id INTEGER NOT NULL, subject_name TEXT,
                    teacher_id INTEGER, teacher_name TEXT, day TEXT NOT NULL, period_number INTEGER NOT NULL,
                    start_time TEXT, end_time TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tt_hist_term ON timetable_history (year, term)")
            _tables_ready = True
            return
        except Exception:
            continue


# --------------------------------------------------------------------------
def _setting(key, default=None):
    row = db.fetch_one("SELECT value FROM generation_settings WHERE key=?", (key,))
    return row["value"] if row else default


def get_current():
    """(year, term) the live timetable belongs to."""
    y = _setting("current_year")
    t = _setting("current_term")
    return (int(y) if y else date.today().year, int(t) if t else 1)


def set_current(year, term):
    db.set_generation_setting("current_year", str(int(year)))
    db.set_generation_setting("current_term", str(int(term)))


def rotate_on():
    return _setting(ROTATE_KEY, "1") == "1"


def set_rotate(on):
    db.set_generation_setting(ROTATE_KEY, "1" if on else "0")


# --------------------------------------------------------------------------
def archive(year, term):
    """Copy the live timetable into history as (year, term), replacing any
    earlier copy of the same term. Returns the number of lessons stored."""
    ensure_tables()
    db.execute("DELETE FROM timetable_history WHERE year=? AND term=?", (year, term))
    rows = db.fetch_all(
        """SELECT te.stream_id, st.name AS stream_name, g.name AS grade_name,
                  te.subject_id, sub.name AS subject_name, te.teacher_id, t.name AS teacher_name,
                  p.day, p.period_number, p.start_time, p.end_time
           FROM timetable_entries te
           JOIN periods p ON p.id = te.period_id
           JOIN streams st ON st.id = te.stream_id
           JOIN grades g ON g.id = st.grade_id
           JOIN subjects sub ON sub.id = te.subject_id
           LEFT JOIN teachers t ON t.id = te.teacher_id""")
    for r in rows:
        db.execute(
            "INSERT INTO timetable_history (year, term, stream_id, stream_name, grade_name, subject_id, subject_name, "
            "teacher_id, teacher_name, day, period_number, start_time, end_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (year, term, r["stream_id"], r["stream_name"], r["grade_name"], r["subject_id"], r["subject_name"],
             r["teacher_id"], r["teacher_name"], r["day"], r["period_number"], r["start_time"], r["end_time"]))
    return len(rows)


def history():
    """Archived terms, newest first, with a lesson count each."""
    ensure_tables()
    return db.fetch_all(
        "SELECT year, term, COUNT(*) AS lessons, COUNT(DISTINCT stream_id) AS classes "
        "FROM timetable_history GROUP BY year, term ORDER BY year DESC, term DESC")


def previous_term():
    """(year, term) of the latest archived term before the current one, or None."""
    ensure_tables()
    y, t = get_current()
    row = db.fetch_one(
        "SELECT year, term FROM timetable_history WHERE year < ? OR (year = ? AND term < ?) "
        "ORDER BY year DESC, term DESC LIMIT 1", (y, y, t))
    return (row["year"], row["term"]) if row else None


def previous_slots():
    """{(teacher_id, stream_id, subject_id, day, period_number)} of the previous term."""
    prev = previous_term()
    if not prev:
        return set()
    rows = db.fetch_all(
        "SELECT teacher_id, stream_id, subject_id, day, period_number FROM timetable_history "
        "WHERE year=? AND term=? AND teacher_id IS NOT NULL", prev)
    return {(r["teacher_id"], r["stream_id"], r["subject_id"], r["day"], r["period_number"]) for r in rows}


def count_repeats(stream_id):
    """Lessons in the live timetable of this class that repeat last term's slot
    (same teacher + subject + day + lesson number)."""
    prev = previous_slots()
    if not prev:
        return 0
    rows = db.fetch_all(
        "SELECT te.teacher_id, te.subject_id, p.day, p.period_number FROM timetable_entries te "
        "JOIN periods p ON p.id = te.period_id WHERE te.stream_id=? AND te.teacher_id IS NOT NULL", (stream_id,))
    return sum(1 for r in rows if (r["teacher_id"], stream_id, r["subject_id"], r["day"], r["period_number"]) in prev)


def is_repeat(prev, teacher_id, stream_id, subject_id, day, period_number):
    """True if this exact lesson (teacher + class + subject + day + lesson number)
    was also in the previous term. `prev` is the set from previous_slots();
    the scheduler should load it once per run and call this for every candidate
    slot, preferring any slot where it returns False."""
    return (teacher_id, stream_id, subject_id, day, period_number) in prev


def repeat_report():
    """Every class with the number of its live lessons that repeat last term,
    worst first: [{stream_id, stream_name, grade_name, repeats, lessons}]."""
    prev = previous_slots()
    rows = db.fetch_all(
        "SELECT te.stream_id, st.name AS stream_name, g.name AS grade_name, te.teacher_id, te.subject_id, "
        "p.day, p.period_number FROM timetable_entries te "
        "JOIN periods p ON p.id = te.period_id JOIN streams st ON st.id = te.stream_id "
        "JOIN grades g ON g.id = st.grade_id")
    out = {}
    for r in rows:
        o = out.setdefault(r["stream_id"], {"stream_id": r["stream_id"], "stream_name": r["stream_name"],
                                            "grade_name": r["grade_name"], "repeats": 0, "lessons": 0})
        o["lessons"] += 1
        if r["teacher_id"] is not None and is_repeat(
                prev, r["teacher_id"], r["stream_id"], r["subject_id"], r["day"], r["period_number"]):
            o["repeats"] += 1
    return sorted(out.values(), key=lambda o: (-o["repeats"], o["grade_name"], o["stream_name"]))


def start_new_term(year, term, mode="regenerate"):
    """Archive the current term, move to (year, term). mode 'regenerate' builds a
    fresh rotated timetable; 'copy' keeps the current lessons as the starting
    point. Returns (archived_count, report_or_None)."""
    cy, ct = get_current()
    stored = archive(cy, ct)
    set_current(year, term)
    report = None
    if mode == "regenerate":
        import scheduler
        report = scheduler.generate_timetable(overwrite=True)
    return stored, report


def teacher_names_for_term(year, term):
    """(stream_id, subject_id) -> 'A / B' from the ARCHIVED timetable of a term,
    or None if that term was never archived."""
    rows = db.fetch_all(
        "SELECT DISTINCT stream_id, subject_id, teacher_name FROM timetable_history "
        "WHERE year=? AND term=? AND teacher_name IS NOT NULL ORDER BY teacher_name", (year, term))
    if not rows:
        return None
    out = {}
    for r in rows:
        out.setdefault((r["stream_id"], r["subject_id"]), []).append(r["teacher_name"])
    return {k: " / ".join(v) for k, v in out.items()}


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@terms_bp.before_request
def _make_tables():
    ensure_tables()


def _next_term(year, term):
    return (year, term + 1) if term < 3 else (year + 1, 1)


@terms_bp.route("/")
def index():
    cy, ct = get_current()
    prev = previous_term()
    ny, nt = _next_term(cy, ct)
    return render_template(
        "terms/index.html", year=cy, term=ct, prev=prev, rotate=rotate_on(), history=history(),
        report=repeat_report() if prev else [], next_year=ny, next_term=nt,
        lessons_now=db.fetch_one("SELECT COUNT(*) AS c FROM timetable_entries")["c"])


@terms_bp.route("/start", methods=["POST"])
def start():
    cy, ct = get_current()
    year, term = request.form.get("year", type=int), request.form.get("term", type=int)
    mode = request.form.get("mode", "regenerate")
    if not year or term not in (1, 2, 3) or mode not in ("regenerate", "copy"):
        flash("Enter a year and a term number from 1 to 3.", "error")
        return redirect(url_for("terms.index"))
    if (year, term) == (cy, ct):
        flash(f"That is already the current term ({year} Term {term}).", "error")
        return redirect(url_for("terms.index"))
    stored, report = start_new_term(year, term, mode)
    flash(f"{cy} Term {ct} saved ({stored} lessons). Now in {year} Term {term}.", "success")
    if report is not None:
        placed = sum(r["scheduled"] for r in report)
        flash(f"New timetable generated: {placed} lessons placed across {len(report)} class(es).", "success")
        left = [f"{r['stream']}: " + "; ".join(r["skipped"]) for r in report if r["skipped"]]
        if left:
            flash("Some lessons could not be placed — " + " | ".join(left), "error")
    return redirect(url_for("terms.index"))


@terms_bp.route("/snapshot", methods=["POST"])
def snapshot():
    cy, ct = get_current()
    stored = archive(cy, ct)
    flash(f"Saved a copy of the {cy} Term {ct} timetable ({stored} lessons).", "success")
    return redirect(url_for("terms.index"))


@terms_bp.route("/rotate", methods=["POST"])
def rotate():
    set_rotate(request.form.get("rotate") == "1")
    flash("Rotation turned " + ("on." if rotate_on() else "off."), "success")
    return redirect(url_for("terms.index"))
