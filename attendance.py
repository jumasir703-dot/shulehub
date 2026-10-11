"""
attendance.py — School Management: daily class attendance.

    /attendance/                 today (or any date) at a glance: which classes are marked, who is absent
    /attendance/mark             the register for one class on one date
    /attendance/report           per-student totals and % for a class over a date range (+ CSV)

A "class" is a stream. Students in a grade that has no stream (or who
haven't been given one) form a class called "<Grade> (no stream)".
Statuses: P present, A absent, L late (counts as present), E excused
(counts as absent, but is shown separately).
"""

import csv
import io
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, Response, flash, g, redirect, render_template, request, url_for,
)

import database as db
from analytics import _csv_safe

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")

STATUS_LABELS = {"P": "Present", "A": "Absent", "L": "Late", "E": "Excused"}
LOW_ATTENDANCE_PCT = 80.0


# ---------------------------------------------------------------- classes
def _classes():
    """All classes that currently have at least one active student."""
    out = []
    for s in db.fetch_all(
            "SELECT s.id, s.name, s.grade_id, g.name AS grade_name FROM streams s "
            "JOIN grades g ON s.grade_id = g.id ORDER BY g.sort_order, s.name"):
        out.append({"key": f"s{s['id']}", "label": s["name"], "grade_id": s["grade_id"],
                    "stream_id": s["id"], "grade_name": s["grade_name"]})
    for gr in db.fetch_all("SELECT id, name FROM grades ORDER BY sort_order, id"):
        has_streams = db.fetch_one("SELECT COUNT(*) AS c FROM streams WHERE grade_id=?", (gr["id"],))["c"] > 0
        loose = db.fetch_one(
            "SELECT COUNT(*) AS c FROM students WHERE grade_id=? AND stream_id IS NULL AND status='active'",
            (gr["id"],))["c"]
        if loose:
            out.append({"key": f"g{gr['id']}", "label": gr["name"] + (" (no stream)" if has_streams else ""),
                        "grade_id": gr["id"], "stream_id": None, "grade_name": gr["name"]})
    # keep grade order, streams first within a grade
    order = {r["id"]: i for i, r in enumerate(db.fetch_all("SELECT id FROM grades ORDER BY sort_order, id"))}
    out.sort(key=lambda c: (order.get(c["grade_id"], 99), c["stream_id"] is None, c["label"]))
    return [c for c in out if _class_students(c)]


def _class_by_key(key):
    return next((c for c in _classes() if c["key"] == key), None)


def _class_students(c):
    if c["stream_id"]:
        return db.fetch_all(
            "SELECT id, admission_no, name FROM students WHERE stream_id=? AND status='active' ORDER BY name",
            (c["stream_id"],))
    return db.fetch_all(
        "SELECT id, admission_no, name FROM students WHERE grade_id=? AND stream_id IS NULL AND status='active' "
        "ORDER BY name", (c["grade_id"],))


def _date_arg(name, default):
    raw = request.values.get(name, "")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return default


def _who():
    return g.user["username"] if getattr(g, "user", None) else None


# ---------------------------------------------------------------- overview
@attendance_bp.route("/")
def index():
    day = _date_arg("date", date.today())
    if day > date.today():
        day = date.today()
    iso = day.isoformat()
    marked = {r["sid"]: r["status"] for r in db.fetch_all(
        "SELECT a.student_id AS sid, a.status FROM attendance a WHERE a.att_date=?", (iso,))}
    classes = []
    for c in _classes():
        studs = _class_students(c)
        counts = {k: 0 for k in STATUS_LABELS}
        n_marked = 0
        for s in studs:
            st = marked.get(s["id"])
            if st:
                counts[st] += 1
                n_marked += 1
        classes.append({**c, "total": len(studs), "marked": n_marked, "counts": counts,
                        "done": n_marked == len(studs) and len(studs) > 0})
    absent = db.fetch_all(
        "SELECT st.id, st.name, st.admission_no, a.status, a.note, g.name AS grade_name, s.name AS stream_name, "
        "(SELECT gu.name FROM guardians gu WHERE gu.student_id = st.id ORDER BY gu.is_primary DESC, gu.id LIMIT 1) AS guardian, "
        "(SELECT gu.phone FROM guardians gu WHERE gu.student_id = st.id ORDER BY gu.is_primary DESC, gu.id LIMIT 1) AS phone "
        "FROM attendance a JOIN students st ON a.student_id = st.id JOIN grades g ON st.grade_id = g.id "
        "LEFT JOIN streams s ON st.stream_id = s.id WHERE a.att_date=? AND a.status IN ('A','E') "
        "ORDER BY g.sort_order, s.name, st.name", (iso,))
    total_marked = sum(c["marked"] for c in classes)
    total_present = sum(c["counts"]["P"] + c["counts"]["L"] for c in classes)
    weekday = day.strftime("%A")
    return render_template(
        "attendance/index.html", today=date.today().isoformat(), day=iso, weekday=weekday, classes=classes, absent=absent,
        school_day=weekday in db.get_active_days(), is_today=day == date.today(),
        pct=round(100 * total_present / total_marked, 1) if total_marked else None,
        prev_day=(day - timedelta(days=1)).isoformat(),
        next_day=(day + timedelta(days=1)).isoformat() if day < date.today() else None)


# ---------------------------------------------------------------- mark
@attendance_bp.route("/mark")
def mark():
    classes = _classes()
    cls = _class_by_key(request.args.get("class", "")) if request.args.get("class") else None
    day = _date_arg("date", date.today())
    if day > date.today():
        day = date.today()
    if not cls:
        return render_template("attendance/mark.html", classes=classes, cls=None, day=day.isoformat(), students=[], today=date.today().isoformat())
    studs = _class_students(cls)
    existing = {r["student_id"]: r for r in db.fetch_all(
        "SELECT student_id, status, note FROM attendance WHERE att_date=? AND student_id IN "
        "(SELECT id FROM students WHERE " + ("stream_id=?" if cls["stream_id"] else "grade_id=? AND stream_id IS NULL") + ")",
        (day.isoformat(), cls["stream_id"] or cls["grade_id"]))}
    rows = [{"id": s["id"], "admission_no": s["admission_no"], "name": s["name"],
             "status": existing[s["id"]]["status"] if s["id"] in existing else "P",
             "note": existing[s["id"]]["note"] if s["id"] in existing else "",
             "saved": s["id"] in existing} for s in studs]
    return render_template(
        "attendance/mark.html", today=date.today().isoformat(), classes=classes, cls=cls, day=day.isoformat(), students=rows,
        already=any(r["saved"] for r in rows), school_day=day.strftime("%A") in db.get_active_days(),
        labels=STATUS_LABELS)


@attendance_bp.route("/mark/save", methods=["POST"])
def mark_save():
    cls = _class_by_key(request.form.get("class", ""))
    day = _date_arg("date", None)
    if not cls or not day or day > date.today():
        flash("Choose a class and a date that isn't in the future.", "error")
        return redirect(url_for("attendance.mark"))
    stmts, tally = [], {k: 0 for k in STATUS_LABELS}
    for s in _class_students(cls):
        status = request.form.get(f"st_{s['id']}", "P")
        if status not in STATUS_LABELS:
            status = "P"
        note = (request.form.get(f"note_{s['id']}", "").strip() or None)
        if note:
            note = note[:120]
        stmts.append((
            "INSERT INTO attendance (student_id, att_date, status, note, marked_by) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (student_id, att_date) DO UPDATE SET status = excluded.status, "
            "note = excluded.note, marked_by = excluded.marked_by",
            (s["id"], day.isoformat(), status, note, _who())))
        tally[status] += 1
    if stmts:
        db.execute_many(stmts)
    flash(f"{cls['label']} — {day.strftime('%a %d %b')}: {tally['P']} present, {tally['L']} late, "
          f"{tally['A']} absent, {tally['E']} excused.", "success")
    return redirect(url_for("attendance.index", date=day.isoformat()))


# ---------------------------------------------------------------- report
def _report(cls, start, end):
    rows = []
    studs = _class_students(cls)
    data = {}
    for r in db.fetch_all(
            "SELECT student_id, status, COUNT(*) AS c FROM attendance WHERE att_date >= ? AND att_date <= ? "
            "GROUP BY student_id, status", (start.isoformat(), end.isoformat())):
        data.setdefault(r["student_id"], {})[r["status"]] = r["c"]
    for s in studs:
        c = data.get(s["id"], {})
        marked = sum(c.values())
        present = c.get("P", 0) + c.get("L", 0)
        rows.append({"id": s["id"], "admission_no": s["admission_no"], "name": s["name"],
                     "P": c.get("P", 0), "L": c.get("L", 0), "A": c.get("A", 0), "E": c.get("E", 0),
                     "marked": marked, "pct": round(100 * present / marked, 1) if marked else None})
    return rows


@attendance_bp.route("/report")
def report():
    classes = _classes()
    cls = _class_by_key(request.args.get("class", "")) if request.args.get("class") else None
    today = date.today()
    start = _date_arg("from", today.replace(day=1))
    end = _date_arg("to", today)
    rows = _report(cls, start, end) if cls else []
    marked_days = db.fetch_one(
        "SELECT COUNT(DISTINCT att_date) AS c FROM attendance WHERE att_date >= ? AND att_date <= ?",
        (start.isoformat(), end.isoformat()))["c"] if cls else 0
    return render_template(
        "attendance/report.html", classes=classes, cls=cls, start=start.isoformat(), end=end.isoformat(),
        rows=rows, low=LOW_ATTENDANCE_PCT, marked_days=marked_days)


@attendance_bp.route("/report.csv")
def report_csv():
    cls = _class_by_key(request.args.get("class", ""))
    if not cls:
        return redirect(url_for("attendance.report"))
    today = date.today()
    start, end = _date_arg("from", today.replace(day=1)), _date_arg("to", today)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", "present", "late", "absent", "excused", "days_marked", "attendance_pct"])
    for r in _report(cls, start, end):
        w.writerow([_csv_safe(x) for x in (r["admission_no"], r["name"], r["P"], r["L"], r["A"], r["E"],
                                           r["marked"], "" if r["pct"] is None else r["pct"])])
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment;filename=attendance_{cls['label'].replace(' ', '_')}_{start}_{end}.csv"})
