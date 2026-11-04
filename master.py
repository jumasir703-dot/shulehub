"""
master.py — Master Timetable: the whole school's timetable on one grid.

    /master/             on-screen master grid (+ print)
    /master/export.csv   the same grid as CSV
    /master/export.pdf   the same grid as a landscape PDF, one page per day

Two layouts, same data (the `timetable_entries` the builder already fills in):

  * By class    one row per stream, one column per period.  Each cell shows
                subject + teacher (+ room).
  * By teacher  one row per teacher, one column per period.  Each cell shows
                subject + class (+ room).  Blank cells are free periods, and
                every row ends with Lessons / Free counts for that day.

Pick one day, or "Whole week" for one grid per day.

Clashes are flagged so they can be fixed before the timetable goes up on the
wall: a teacher in two different classes in the same period, or a room used
by two different classes in the same period. The list covers the day(s) on
screen. Optional-subject groups (several subjects in one class at the same
time) are NOT clashes and are left alone.

Access: same as View / Export (the "timetable_view" service) — see the
("master.", "timetable_view") line in permissions.py.
"""

import csv
import io
import re
from collections import defaultdict
from xml.sax.saxutils import escape

from flask import Blueprint, Response, render_template, request
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db
from analytics import _csv_safe

master_bp = Blueprint("master", __name__, url_prefix="/master")

LAYOUTS = {"class": "By class", "teacher": "By teacher"}
WEEK = "all"
MAX_CLASHES_SHOWN = 60


# ---------------------------------------------------------------- data
def _columns():
    """Lesson periods in order, with a break marker after the period it follows."""
    periods = db.fetch_all(
        "SELECT DISTINCT period_number, start_time, end_time FROM periods "
        "WHERE is_break=0 ORDER BY period_number"
    )
    break_after = {b["after_period"]: b for b in db.get_break_rows()}
    cols = []
    for p in periods:
        cols.append({"type": "lesson", "period_number": p["period_number"],
                     "start_time": p["start_time"], "end_time": p["end_time"]})
        b = break_after.get(p["period_number"])
        if b:
            cols.append({"type": "break", "label": b["label"],
                         "start_time": b["start_time"], "end_time": b["end_time"]})
    return cols


def _grades():
    return db.fetch_all("SELECT id, name FROM grades ORDER BY sort_order, id")


def _clean_choice(layout, day, grade, days, grades):
    layout = layout if layout in LAYOUTS else "class"
    day = day if (day == WEEK or day in days) else WEEK
    valid_grades = {g["id"] for g in grades}
    grade = grade if (layout == "class" and grade in valid_grades) else None
    return layout, day, grade


def build_master(layout="class", day=WEEK, grade=None):
    """Everything the page, the CSV and the PDF need, built once.

    Returns a dict:
      title, layout, day, grade, days (all active days), shown_days,
      columns (lesson + break columns), lesson_cols (lessons only),
      tables  [{"day", "rows": [{"id","label","sub","cells":{period_no:[piece]},
                                 "lessons","free"}]}],
      clashes [str], has_entries (bool)
    A piece is {"subject","other","room","clash"}; "other" is the teacher in the
    class layout and the class in the teacher layout.
    """
    days = db.get_active_days()
    grades = _grades()
    layout, day, grade = _clean_choice(layout, day, grade, days, grades)
    shown_days = days if day == WEEK else [day]
    columns = _columns()
    lesson_cols = [c for c in columns if c["type"] == "lesson"]

    # ---- one query for every lesson in the school
    entries = db.fetch_all(
        """SELECT te.stream_id, te.teacher_id, te.room_id,
                  p.day, p.period_number,
                  sub.name AS subject_name,
                  st.name AS stream_name, st.grade_id,
                  t.name AS teacher_name, rm.name AS room_name
           FROM timetable_entries te
           JOIN periods p ON te.period_id = p.id
           JOIN subjects sub ON te.subject_id = sub.id
           JOIN streams st ON te.stream_id = st.id
           LEFT JOIN teachers t ON te.teacher_id = t.id
           LEFT JOIN rooms rm ON te.room_id = rm.id
           ORDER BY sub.name"""
    )

    # ---- clashes (computed over the WHOLE school, whatever is on screen)
    teacher_at = defaultdict(set)   # (teacher_id, day, period) -> {stream ids}
    room_at = defaultdict(set)      # (room_id, day, period)    -> {stream ids}
    stream_names = {}
    for e in entries:
        slot = (e["day"], e["period_number"])
        stream_names[e["stream_id"]] = e["stream_name"]
        if e["teacher_id"]:
            teacher_at[(e["teacher_id"],) + slot].add(e["stream_id"])
        if e["room_id"]:
            room_at[(e["room_id"],) + slot].add(e["stream_id"])
    teacher_clash = {k for k, v in teacher_at.items() if len(v) > 1}
    room_clash = {k for k, v in room_at.items() if len(v) > 1}

    teacher_name = {e["teacher_id"]: e["teacher_name"] for e in entries if e["teacher_id"]}
    room_name = {e["room_id"]: e["room_name"] for e in entries if e["room_id"]}
    day_order = {d: i for i, d in enumerate(days)}

    # Only the clashes on the day(s) being shown are listed, so a one-day
    # printout never mentions a problem that isn't on the page.
    clashes = []
    for (tid, d, pn) in sorted(teacher_clash, key=lambda k: (day_order.get(k[1], 99), k[2], k[0])):
        if d not in shown_days:
            continue
        names = ", ".join(sorted(stream_names[s] for s in teacher_at[(tid, d, pn)]))
        clashes.append(f"{teacher_name.get(tid, 'A teacher')} is in {len(teacher_at[(tid, d, pn)])} "
                       f"classes at once on {d}, P{pn}: {names}")
    for (rid, d, pn) in sorted(room_clash, key=lambda k: (day_order.get(k[1], 99), k[2], k[0])):
        if d not in shown_days:
            continue
        names = ", ".join(sorted(stream_names[s] for s in room_at[(rid, d, pn)]))
        clashes.append(f"Room {room_name.get(rid, '?')} is used by {len(room_at[(rid, d, pn)])} "
                       f"classes at once on {d}, P{pn}: {names}")

    # ---- the rows that make up the grid
    if layout == "class":
        sql = ("SELECT s.id, s.name, g.name AS grade_name FROM streams s "
               "JOIN grades g ON s.grade_id = g.id ")
        params = ()
        if grade:
            sql += "WHERE s.grade_id=? "
            params = (grade,)
        sql += "ORDER BY g.sort_order, g.id, s.name"
        row_defs = [{"id": r["id"], "label": r["name"], "sub": r["grade_name"]}
                    for r in db.fetch_all(sql, params)]
    else:
        row_defs = [{"id": r["id"], "label": r["name"], "sub": ""}
                    for r in db.fetch_all("SELECT id, name FROM teachers ORDER BY name")]

    wanted_rows = {r["id"] for r in row_defs}
    cells = defaultdict(list)       # (row_id, day, period_number) -> [piece]
    for e in entries:
        slot = (e["day"], e["period_number"])
        if layout == "class":
            row_id, other = e["stream_id"], e["teacher_name"] or "—"
            clash = bool(e["teacher_id"]) and ((e["teacher_id"],) + slot) in teacher_clash
        else:
            row_id, other = e["teacher_id"], e["stream_name"]
            clash = bool(e["teacher_id"]) and ((e["teacher_id"],) + slot) in teacher_clash
        if e["room_id"] and ((e["room_id"],) + slot) in room_clash:
            clash = True
        if row_id not in wanted_rows:
            continue
        cells[(row_id,) + slot].append({
            "subject": e["subject_name"], "other": other,
            "room": e["room_name"] or "", "clash": clash,
        })

    tables = []
    for d in shown_days:
        rows = []
        for r in row_defs:
            row_cells = {c["period_number"]: cells.get((r["id"], d, c["period_number"]), [])
                         for c in lesson_cols}
            busy = sum(1 for v in row_cells.values() if v)
            rows.append({**r, "cells": row_cells, "lessons": busy,
                         "free": len(lesson_cols) - busy})
        tables.append({"day": d, "rows": rows})

    school = db.get_school_settings()["name"]
    scope = "Whole week" if day == WEEK else day
    extra = ""
    if grade:
        extra = f" · {next((g['name'] for g in grades if g['id'] == grade), '')}"
    title = f"{school} — Master Timetable ({LAYOUTS[layout].lower()}) · {scope}{extra}"

    return {
        "title": title, "layout": layout, "day": day, "grade": grade,
        "days": days, "shown_days": shown_days, "grades": grades,
        "columns": columns, "lesson_cols": lesson_cols,
        "tables": tables, "clashes": clashes,
        "has_entries": bool(entries),
        # lets other timetables (remedial) reuse the same grid, CSV and PDF code
        "col_prefix": "P", "count_label": "Lessons", "kind": "master",
    }


def piece_text(p):
    """One lesson as plain text: 'Maths (Mr Otieno, Lab 1)'."""
    extra = ", ".join(x for x in (p["other"], p["room"]) if x)
    return f"{p['subject']} ({extra})" if extra else p["subject"]


def _args():
    return (request.args.get("layout", "class"),
            request.args.get("day", WEEK),
            request.args.get("grade", type=int))


def _safe_filename(title, extension):
    ascii_only = title.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    return f"{safe or 'master_timetable'}.{extension}"


# ---------------------------------------------------------------- page
@master_bp.route("/")
def index():
    layout, day, grade = _args()
    data = build_master(layout, day, grade)
    return render_template("master/index.html", m=data, layouts=LAYOUTS, week=WEEK,
                           shown_clashes=data["clashes"][:MAX_CLASHES_SHOWN])


# ---------------------------------------------------------------- CSV
@master_bp.route("/export.csv")
def export_csv():
    return render_csv(build_master(*_args()))


def render_csv(m):
    """CSV download for any grid dict shaped like build_master()'s result."""
    out = io.StringIO()
    w = csv.writer(out)
    first = "Class" if m["layout"] == "class" else "Teacher"
    w.writerow([first, "Day"]
               + [f"{m['col_prefix']}{c['period_number']} {c['start_time']}-{c['end_time']}"
                  for c in m["lesson_cols"]]
               + [m["count_label"], "Free"])
    for t in m["tables"]:
        for r in t["rows"]:
            w.writerow(
                [_csv_safe(r["label"]), t["day"]]
                + [_csv_safe(" / ".join(piece_text(p) for p in r["cells"][c["period_number"]]))
                   for c in m["lesson_cols"]]
                + [r["lessons"], r["free"]]
            )
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={_safe_filename(m['title'], 'csv')}"},
    )


# ---------------------------------------------------------------- PDF
@master_bp.route("/export.pdf")
def export_pdf():
    return render_pdf(build_master(*_args()))


def render_pdf(m):
    """Landscape A4 PDF (one page per day) for any grid dict shaped like
    build_master()'s result."""
    page = landscape(A4)
    margin_x, margin_y = 10 * mm, 10 * mm
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin_x, rightMargin=margin_x,
                            topMargin=margin_y, bottomMargin=margin_y)
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("mt_cell", parent=styles["Normal"], fontSize=6.6, leading=7.8,
                                alignment=1)
    label_style = ParagraphStyle("mt_label", parent=styles["Normal"], fontSize=7.6, leading=9,
                                 fontName="Helvetica-Bold")
    head_style = ParagraphStyle("mt_head", parent=styles["Normal"], fontSize=7, leading=8.4,
                                alignment=1, textColor=colors.white, fontName="Helvetica-Bold")
    brk_style = ParagraphStyle("mt_brk", parent=styles["Normal"], fontSize=6.2, leading=7.4,
                               alignment=1, textColor=colors.HexColor("#103524"),
                               fontName="Helvetica-Bold")

    logo_found = branding.get_logo()   # one lookup; a fresh image object is built per page
    story = []
    first_label = "Class" if m["layout"] == "class" else "Teacher"

    for index_, t in enumerate(m["tables"]):
        if index_:
            story.append(PageBreak())
        story += branding.logo_story(found=logo_found)
        story.append(Paragraph(escape(m["title"]) if m["day"] != WEEK
                               else escape(m["title"]) + f" — {escape(t['day'])}", styles["Title"]))
        story.append(Spacer(1, 4))

        head = [Paragraph(first_label, head_style)]
        for c in m["columns"]:
            if c["type"] == "break":
                head.append(Paragraph(escape(c["label"]), head_style))
            else:
                head.append(Paragraph(f"{m['col_prefix']}{c['period_number']}<br/>{escape(str(c['start_time']))}"
                                      f"–{escape(str(c['end_time']))}", head_style))
        head += [Paragraph(escape(m["count_label"]), head_style), Paragraph("Free", head_style)]
        data = [head]

        clash_cells = []
        for r_i, r in enumerate(t["rows"], start=1):
            label = escape(r["label"]) + (f"<br/><font size='6' color='#6f6a5b'>{escape(r['sub'])}</font>"
                                           if r["sub"] else "")
            row = [Paragraph(label, label_style)]
            for c_i, c in enumerate(m["columns"], start=1):
                if c["type"] == "break":
                    row.append("")
                    continue
                pieces = r["cells"][c["period_number"]]
                if not pieces:
                    row.append(Paragraph("", cell_style))
                    continue
                if any(p["clash"] for p in pieces):
                    clash_cells.append((c_i, r_i))
                row.append(Paragraph("<br/>".join(
                    f"<b>{escape(p['subject'])}</b><br/>{escape(', '.join(x for x in (p['other'], p['room']) if x))}"
                    for p in pieces), cell_style))
            row += [str(r["lessons"]), str(r["free"])]
            data.append(row)

        n_cols = len(data[0])
        usable = page[0] - 2 * margin_x
        label_w = 30 * mm
        count_w = 12 * mm
        brk_w = 9 * mm
        n_lessons = len(m["lesson_cols"])
        n_breaks = n_cols - 1 - n_lessons - 2
        lesson_w = (usable - label_w - 2 * count_w - n_breaks * brk_w) / max(n_lessons, 1)
        widths = [label_w]
        for c in m["columns"]:
            widths.append(brk_w if c["type"] == "break" else lesson_w)
        widths += [count_w, count_w]

        tbl = Table(data, colWidths=widths, repeatRows=1)
        cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103524")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfc6ad")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (-2, 1), (-1, -1), 7),
            ("ALIGN", (-2, 1), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbf8f0")]),
        ]
        for c_i, c in enumerate(m["columns"], start=1):
            if c["type"] == "break":
                cmds.append(("BACKGROUND", (c_i, 1), (c_i, -1), colors.HexColor("#f4e6c6")))
        for (c_i, r_i) in clash_cells:
            cmds.append(("BACKGROUND", (c_i, r_i), (c_i, r_i), colors.HexColor("#f8dcd6")))
        tbl.setStyle(TableStyle(cmds))
        if t["rows"]:
            story.append(tbl)
        else:
            story.append(Paragraph("Nothing to show for this selection.", styles["Normal"]))

        if index_ == len(m["tables"]) - 1 and m["clashes"]:
            story.append(Spacer(1, 6))
            story.append(Paragraph("<b>Clashes to fix</b>", styles["Normal"]))
            for line in m["clashes"][:MAX_CLASHES_SHOWN]:
                story.append(Paragraph("• " + escape(line), cell_style.clone("mt_clash", alignment=0)))

    if not story:
        story.append(Paragraph("Nothing to show.", styles["Normal"]))
    doc.build(story)
    buf.seek(0)
    return Response(
        buf.getvalue(), mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment;filename={_safe_filename(m['title'], 'pdf')}"},
    )
