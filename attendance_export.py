"""
attendance_export.py - download attendance as CSV or PDF.

    /attendance/export/              the page with all the download options
    daily register   one day, one class or the whole school     (CSV / PDF)
    period summary   totals and % per student over a date range (CSV / PDF)
    register grid    one class, a row per student and a column per day (CSV / PDF)

It reads the same records as the Attendance pages (attendance.py), so the numbers always match.
"""
import csv
import io
from datetime import date, datetime
from xml.sax.saxutils import escape

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db
from analytics import _csv_safe, _safe_filename
from attendance import STATUS_LABELS, _class_by_key, _class_students, _classes, _report

attexport_bp = Blueprint("attexport", __name__, url_prefix="/attendance/export")

MAX_GRID_DAYS = 31


# ------------------------------------------------------------------ helpers
def _day(name, default):
    try:
        return datetime.strptime(request.values.get(name, ""), "%Y-%m-%d").date()
    except ValueError:
        return default


def _school():
    try:
        return db.get_school_settings()["name"] or "School"
    except Exception:
        return "School"


def _brand():
    try:
        import school_theme
        return colors.HexColor(school_theme.load()["primary"])
    except Exception:
        return colors.HexColor("#1f2937")


def _styles():
    ss = getSampleStyleSheet()
    return {"title": ParagraphStyle("t", parent=ss["Title"], fontSize=15, leading=18, spaceAfter=2),
            "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=9.5, textColor=colors.HexColor("#555555"), alignment=1),
            "h": ParagraphStyle("h", parent=ss["Normal"], fontSize=11, leading=14, fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=3),
            "cell": ParagraphStyle("c", parent=ss["Normal"], fontSize=8.5, leading=10.5),
            "small": ParagraphStyle("sm", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#666666"))}


def _selected_classes():
    """The class chosen on the page, or every class when none is chosen."""
    key = request.values.get("class", "")
    if key:
        c = _class_by_key(key)
        return [c] if c else []
    return _classes()


def _csv_response(rows, filename):
    out = io.StringIO()
    w = csv.writer(out)
    for r in rows:
        w.writerow([_csv_safe(x) if isinstance(x, str) else x for x in r])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename={filename}"})


def _pdf_response(story, title, filename, page=A4):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm, title=title)
    doc.build(story)
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment;filename={_safe_filename(filename, 'pdf')}"})


def _head(ss, subtitle):
    return branding.logo_story() + [Paragraph(escape(_school()), ss["title"]), Paragraph(escape(subtitle), ss["sub"]), Spacer(1, 8)]


def _table(data, widths, brand, right_from=None):
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), brand), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#aaaaaa")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6fa")])]
    if right_from is not None:
        style.append(("ALIGN", (right_from, 0), (-1, -1), "CENTER"))
    t.setStyle(TableStyle(style))
    return t


# ------------------------------------------------------------------ the page
@attexport_bp.route("/")
def index():
    today = date.today()
    return render_template("attexport/index.html", classes=_classes(), today=today.isoformat(),
                           month_start=today.replace(day=1).isoformat(), max_days=MAX_GRID_DAYS)


# ------------------------------------------------------------------ daily register
def _daily(day):
    marks = {r["student_id"]: r for r in db.fetch_all("SELECT student_id, status, note FROM attendance WHERE att_date = ?", (day.isoformat(),))}
    out = []
    for c in _selected_classes():
        rows, counts = [], {k: 0 for k in STATUS_LABELS}
        for s in _class_students(c):
            m = marks.get(s["id"])
            if m:
                counts[m["status"]] = counts.get(m["status"], 0) + 1
            rows.append((s["admission_no"] or "", s["name"], STATUS_LABELS.get(m["status"], m["status"]) if m else "Not marked",
                         (m["note"] or "") if m else ""))
        out.append({"label": c["label"], "rows": rows, "counts": counts})
    return out


@attexport_bp.route("/register.csv")
def register_csv():
    day = _day("date", date.today())
    data = _daily(day)
    if not data:
        flash("Choose a class that has students.", "error")
        return redirect(url_for("attexport.index"))
    rows = [["date", "class", "admission_no", "name", "status", "note"]]
    for c in data:
        rows += [[day.isoformat(), c["label"], a, n, s, note] for a, n, s, note in c["rows"]]
    return _csv_response(rows, f"attendance_register_{day.isoformat()}.csv")


@attexport_bp.route("/register.pdf")
def register_pdf():
    day = _day("date", date.today())
    data = _daily(day)
    if not data:
        flash("Choose a class that has students.", "error")
        return redirect(url_for("attexport.index"))
    ss, brand, story = _styles(), _brand(), []
    for n, c in enumerate(data):
        if n:
            story.append(PageBreak())
        story += _head(ss, f"Attendance register · {day.strftime('%A %d %B %Y')}")
        story.append(Paragraph(escape(c["label"]), ss["h"]))
        table = [["#", "Adm. no.", "Name", "Status", "Note"]] + [[i, a, Paragraph(escape(nm), ss["cell"]), s, Paragraph(escape(note), ss["cell"])]
                                                                for i, (a, nm, s, note) in enumerate(c["rows"], 1)]
        story.append(_table(table, [10 * mm, 24 * mm, 70 * mm, 28 * mm, 50 * mm], brand))
        k = c["counts"]
        story += [Spacer(1, 6), Paragraph(f"Present {k.get('P', 0)} · Late {k.get('L', 0)} · Absent {k.get('A', 0)} · Excused {k.get('E', 0)}", ss["small"]),
                  Spacer(1, 16), Paragraph("Class teacher: ______________________ &nbsp;&nbsp;&nbsp; Date: ______________", ss["cell"])]
    return _pdf_response(story, "Attendance register", f"Attendance register {day.isoformat()}")


# ------------------------------------------------------------------ period summary
def _summary(start, end):
    return [{"label": c["label"], "rows": _report(c, start, end)} for c in _selected_classes()]


def _range():
    today = date.today()
    start, end = _day("from", today.replace(day=1)), _day("to", today)
    return (start, end) if start <= end else (end, start)


@attexport_bp.route("/summary.csv")
def summary_csv():
    start, end = _range()
    data = _summary(start, end)
    if not data:
        flash("Choose a class that has students.", "error")
        return redirect(url_for("attexport.index"))
    rows = [["class", "admission_no", "name", "present", "late", "absent", "excused", "days_marked", "attendance_pct"]]
    for c in data:
        rows += [[c["label"], r["admission_no"] or "", r["name"], r["P"], r["L"], r["A"], r["E"], r["marked"],
                  "" if r["pct"] is None else r["pct"]] for r in c["rows"]]
    return _csv_response(rows, f"attendance_summary_{start}_{end}.csv")


@attexport_bp.route("/summary.pdf")
def summary_pdf():
    start, end = _range()
    data = _summary(start, end)
    if not data:
        flash("Choose a class that has students.", "error")
        return redirect(url_for("attexport.index"))
    ss, brand, story = _styles(), _brand(), _head(_styles(), f"Attendance summary · {start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}")
    for n, c in enumerate(data):
        if n:
            story.append(PageBreak())
            story += _head(ss, f"Attendance summary · {start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}")
        story.append(Paragraph(escape(c["label"]), ss["h"]))
        table = [["Adm. no.", "Name", "Present", "Late", "Absent", "Excused", "Days", "%"]]
        for r in c["rows"]:
            table.append([r["admission_no"] or "", Paragraph(escape(r["name"]), ss["cell"]), r["P"], r["L"], r["A"], r["E"], r["marked"],
                          "" if r["pct"] is None else f"{r['pct']}"])
        story.append(_table(table, [24 * mm, 66 * mm, 17 * mm, 14 * mm, 16 * mm, 17 * mm, 14 * mm, 14 * mm], brand, right_from=2))
    return _pdf_response(story, "Attendance summary", f"Attendance summary {start}_{end}")


# ------------------------------------------------------------------ register grid
def _grid(cls, start, end):
    studs = _class_students(cls)
    ids = [s["id"] for s in studs]
    if not ids:
        return [], [], {}
    marks = db.fetch_all("SELECT student_id, att_date, status FROM attendance WHERE att_date >= ? AND att_date <= ? AND student_id IN (%s)"
                         % ",".join("?" * len(ids)), tuple([start.isoformat(), end.isoformat()] + ids))
    days = sorted({m["att_date"] for m in marks})
    cell = {(m["student_id"], m["att_date"]): m["status"] for m in marks}
    return studs, days, cell


def _grid_input():
    cls = _class_by_key(request.values.get("class", ""))
    start, end = _range()
    if not cls:
        flash("Choose one class for the register grid.", "error")
        return None
    studs, days, cell = _grid(cls, start, end)
    if not days:
        flash("No attendance was marked for that class in those dates.", "error")
        return None
    if len(days) > MAX_GRID_DAYS:
        flash(f"That range has {len(days)} marked days. Choose at most {MAX_GRID_DAYS} (about a month) at a time.", "error")
        return None
    return cls, start, end, studs, days, cell


@attexport_bp.route("/grid.csv")
def grid_csv():
    got = _grid_input()
    if not got:
        return redirect(url_for("attexport.index"))
    cls, start, end, studs, days, cell = got
    rows = [["admission_no", "name"] + days + ["present", "late", "absent", "excused"]]
    for s in studs:
        marks = [cell.get((s["id"], d), "") for d in days]
        rows.append([s["admission_no"] or "", s["name"]] + marks + [marks.count(k) for k in "PLAE"])
    return _csv_response(rows, f"attendance_grid_{cls['label'].replace(' ', '_')}_{start}_{end}.csv")


@attexport_bp.route("/grid.pdf")
def grid_pdf():
    got = _grid_input()
    if not got:
        return redirect(url_for("attexport.index"))
    cls, start, end, studs, days, cell = got
    ss, brand = _styles(), _brand()
    story = _head(ss, f"Attendance register · {cls['label']} · {start.strftime('%d %b')} to {end.strftime('%d %b %Y')}")
    head = ["#", "Name"] + [datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m") for d in days] + ["P", "L", "A", "E"]
    table = [head]
    for i, s in enumerate(studs, 1):
        marks = [cell.get((s["id"], d), "") for d in days]
        table.append([i, Paragraph(escape(s["name"]), ss["cell"])] + marks + [marks.count(k) for k in "PLAE"])
    width = landscape(A4)[0] - 28 * mm
    day_w = max(min((width - 8 * mm - 52 * mm - 4 * 8 * mm) / len(days), 11 * mm), 6.5 * mm)
    story.append(_table(table, [8 * mm, 52 * mm] + [day_w] * len(days) + [8 * mm] * 4, brand, right_from=2))
    story += [Spacer(1, 6), Paragraph("P present · L late (counts as present) · A absent · E excused", ss["small"])]
    return _pdf_response(story, "Attendance register", f"Attendance grid {cls['label']} {start}_{end}", page=landscape(A4))
