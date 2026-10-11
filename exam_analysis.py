"""
exam_analysis.py
The calculation engine behind the Exam Analysis module. It reads exam,
student and mark rows through database.py and produces ready-to-display
results; it has no Flask code, so every function here can be tested on its
own.

How results are computed
------------------------
* A mark is converted to a percentage of that subject's "out of", then to a
  grade band (e.g. B+ or ME1) using the grade's grading scale.
* A student's TOTAL is the sum of the raw marks for the subjects they sat,
  and their MEAN % is total / sum-of-"out of" for those same subjects. A
  blank mark means "did not sit / not taking this subject" and is simply
  left out, which is what makes optional subjects fair.
* POSITION is ranked on MEAN %, not raw total, so that a student taking an
  extra optional subject isn't automatically ahead. Equal means share a
  position ("competition" ranking: 1, 2, 2, 4).
* Overall position = among all students of the grade who sat the exam;
  stream position = among students of the same stream.
"""

from collections import OrderedDict, defaultdict

import curriculum
import database as db


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------
def get_bands(scale):
    """Grading bands for a scale, highest threshold first."""
    rows = db.fetch_all(
        "SELECT id, label, min_pct, points, remark FROM grading_bands "
        "WHERE scale=? ORDER BY min_pct DESC",
        (scale,),
    )
    bands = [dict(r) for r in rows]
    if not bands:  # defensive: should never happen after _post_init
        info = curriculum.SCALES[scale]
        bands = [
            {"id": None, "label": l, "min_pct": m, "points": p, "remark": r}
            for l, m, p, r in info["bands"]
        ]
    return bands


def band_for(pct, bands):
    """The band a percentage falls into (the lowest band catches everything below)."""
    for b in bands:
        if pct >= b["min_pct"]:
            return b
    return bands[-1]


def pass_pct_for(scale):
    return curriculum.SCALES.get(scale, {}).get("pass_pct", 50)


def _pct(score, out_of):
    # score*100/out_of (not score/out_of*100) avoids float noise at boundaries
    return round(score * 100.0 / out_of, 6)


def _assign_positions(items, key, field):
    """Competition ranking in place: highest key = 1, ties share a position."""
    ordered = sorted(items, key=key, reverse=True)
    prev, pos = None, 0
    for i, it in enumerate(ordered):
        k = round(key(it), 4)
        if k != prev:
            pos, prev = i + 1, k
        it[field] = pos


# --------------------------------------------------------------------------
# Loading + computing one exam
# --------------------------------------------------------------------------
def get_exam(exam_id):
    return db.fetch_one(
        "SELECT e.id, e.name, e.term, e.year, e.exam_date, e.grade_id, "
        "g.name AS grade_name, g.curriculum "
        "FROM exams e JOIN grades g ON e.grade_id = g.id WHERE e.id=?",
        (exam_id,),
    )


def exam_subjects(exam_id):
    return [dict(r) for r in db.fetch_all(
        "SELECT es.subject_id AS id, es.out_of, sub.name, sub.pathway_id "
        "FROM exam_subjects es JOIN subjects sub ON es.subject_id = sub.id "
        "WHERE es.exam_id=? ORDER BY sub.id",
        (exam_id,),
    )]


def build_results(exam_id):
    """Compute everything for one exam. Returns None if the exam doesn't exist.

    Returned dict keys:
      exam, scale, bands, pass_pct, subjects (list), students (list, every
      active student of the grade), ranked (students with >=1 mark, best first),
      streams (list of {id, name})
    Each student dict has: id, admission_no, name, gender, stream_id,
      stream_name, scores{subject_id: {score,out_of,pct,label,points,rank}},
      n_subjects, total, out_total, mean_pct, points, mean_points,
      grade_label, remark, position, stream_position.
    """
    exam = get_exam(exam_id)
    if not exam:
        return None

    scale = curriculum.scale_for_curriculum(exam["curriculum"])
    bands = get_bands(scale)
    subjects = exam_subjects(exam_id)
    subj_by_id = {s["id"]: s for s in subjects}

    students = [dict(r) for r in db.fetch_all(
        "SELECT st.id, st.admission_no, st.name, st.gender, st.stream_id, "
        "s.name AS stream_name "
        "FROM students st LEFT JOIN streams s ON st.stream_id = s.id "
        "WHERE st.grade_id=? AND st.active=1 ORDER BY st.name",
        (exam["grade_id"],),
    )]

    # Students who sat this exam but have since been promoted, graduated or
    # left still belong in its results: otherwise promoting a class at year
    # end would rewrite last term's merit list. Their stream is the one they
    # were in when they left this grade (kept in promotion_log).
    have = {st["id"] for st in students}
    stream_names = {r["id"]: r["name"] for r in db.fetch_all("SELECT id, name FROM streams")}
    for r in db.fetch_all(
        "SELECT DISTINCT st.id, st.admission_no, st.name, st.gender, st.grade_id, st.stream_id, "
        "(SELECT pl.from_stream_id FROM promotion_log pl WHERE pl.student_id = st.id "
        " AND pl.from_grade_id = ? ORDER BY pl.id DESC LIMIT 1) AS old_stream "
        "FROM students st JOIN marks m ON m.student_id = st.id WHERE m.exam_id = ?",
        (exam["grade_id"], exam_id),
    ):
        if r["id"] in have:
            continue
        sid = r["stream_id"] if r["grade_id"] == exam["grade_id"] else r["old_stream"]
        students.append({
            "id": r["id"], "admission_no": r["admission_no"], "name": r["name"], "gender": r["gender"],
            "stream_id": sid, "stream_name": stream_names.get(sid),
        })
    students.sort(key=lambda x: x["name"])

    mark_map = {}
    for m in db.fetch_all("SELECT student_id, subject_id, score FROM marks WHERE exam_id=?", (exam_id,)):
        if m["subject_id"] in subj_by_id:
            mark_map[(m["student_id"], m["subject_id"])] = m["score"]

    for st in students:
        scores = OrderedDict()
        for sub in subjects:
            raw = mark_map.get((st["id"], sub["id"]))
            if raw is None:
                continue
            pct = _pct(raw, sub["out_of"])
            b = band_for(pct, bands)
            scores[sub["id"]] = {
                "score": raw, "out_of": sub["out_of"], "pct": pct,
                "label": b["label"], "points": b["points"], "rank": None,
            }
        st["scores"] = scores
        st["n_subjects"] = len(scores)
        st["total"] = sum(v["score"] for v in scores.values())
        st["out_total"] = sum(v["out_of"] for v in scores.values())
        st["points"] = sum(v["points"] for v in scores.values())
        if scores:
            st["mean_pct"] = round(st["total"] * 100.0 / st["out_total"], 4)
            st["mean_points"] = round(st["points"] / len(scores), 2)
            b = band_for(st["mean_pct"], bands)
            st["grade_label"], st["remark"] = b["label"], b["remark"]
        else:
            st["mean_pct"] = st["mean_points"] = None
            st["grade_label"] = st["remark"] = None
        st["position"] = None
        st["stream_position"] = None

    ranked = [s for s in students if s["n_subjects"] > 0]
    _assign_positions(ranked, lambda s: s["mean_pct"], "position")
    by_stream = defaultdict(list)
    for s in ranked:
        by_stream[s["stream_id"]].append(s)
    for group in by_stream.values():
        _assign_positions(group, lambda s: s["mean_pct"], "stream_position")
    ranked.sort(key=lambda s: (s["position"], s["name"]))

    # subject rank for each student (across the whole grade)
    for sub in subjects:
        sitting = [s["scores"][sub["id"]] for s in students if sub["id"] in s["scores"]]
        _assign_positions(sitting, lambda v: v["pct"], "rank")

    streams = OrderedDict()
    for s in students:
        if s["stream_id"] not in streams:
            streams[s["stream_id"]] = {"id": s["stream_id"], "name": s["stream_name"] or "No stream"}

    return {
        "exam": dict(exam), "scale": scale, "bands": bands,
        "pass_pct": pass_pct_for(scale), "subjects": subjects,
        "students": students, "ranked": ranked,
        "streams": list(streams.values()),
    }


# --------------------------------------------------------------------------
# Aggregate analysis on top of build_results()
# --------------------------------------------------------------------------
def _mean(values):
    return sum(values) / len(values) if values else None


def subject_stats(res):
    """Per-subject analysis for the whole grade, ranked by mean %."""
    out = []
    for sub in res["subjects"]:
        pcts, raws, dist = [], [], defaultdict(int)
        for s in res["students"]:
            v = s["scores"].get(sub["id"])
            if v:
                pcts.append(v["pct"])
                raws.append(v["score"])
                dist[v["label"]] += 1
        if not pcts:
            out.append({"id": sub["id"], "name": sub["name"], "out_of": sub["out_of"],
                        "entries": 0, "mean_pct": None, "mean_score": None,
                        "highest": None, "lowest": None, "pass_rate": None,
                        "label": None, "dist": {}, "rank": None})
            continue
        mean_pct = _mean(pcts)
        out.append({
            "id": sub["id"], "name": sub["name"], "out_of": sub["out_of"],
            "entries": len(pcts), "mean_pct": round(mean_pct, 2),
            "mean_score": round(_mean(raws), 2),
            "highest": max(raws), "lowest": min(raws),
            "pass_rate": round(100.0 * sum(1 for p in pcts if p >= res["pass_pct"]) / len(pcts), 1),
            "label": band_for(mean_pct, res["bands"])["label"],
            "dist": dict(dist), "rank": None,
        })
    with_data = [o for o in out if o["entries"]]
    _assign_positions(with_data, lambda o: o["mean_pct"], "rank")
    return out


def stream_stats(res):
    """Per-stream comparison, ranked by mean %."""
    groups = defaultdict(list)
    for s in res["ranked"]:
        groups[s["stream_id"]].append(s)
    out = []
    for stream in res["streams"]:
        members = groups.get(stream["id"], [])
        if not members:
            continue
        mean_pct = _mean([m["mean_pct"] for m in members])
        top = min(members, key=lambda m: (m["stream_position"], m["name"]))
        out.append({
            "id": stream["id"], "name": stream["name"], "count": len(members),
            "mean_pct": round(mean_pct, 2),
            "mean_points": round(_mean([m["mean_points"] for m in members]), 2),
            "label": band_for(mean_pct, res["bands"])["label"],
            "top_name": top["name"], "top_pct": top["mean_pct"], "rank": None,
        })
    _assign_positions(out, lambda o: o["mean_pct"], "rank")
    out.sort(key=lambda o: o["rank"])
    return out


def overall_distribution(res):
    """How many students landed in each overall grade, in scale order."""
    counts = defaultdict(int)
    for s in res["ranked"]:
        counts[s["grade_label"]] += 1
    return [{"label": b["label"], "count": counts.get(b["label"], 0)} for b in res["bands"]]


def teacher_map(subject_ids):
    """(stream_id, subject_id) -> 'Teacher A / Teacher B' from the TIMETABLE.
    This is what ties exam results back to who teaches what."""
    if not subject_ids:
        return {}
    placeholders = ",".join("?" for _ in subject_ids)
    rows = db.fetch_all(
        "SELECT DISTINCT te.stream_id, te.subject_id, t.name AS teacher_name "
        "FROM timetable_entries te JOIN teachers t ON te.teacher_id = t.id "
        f"WHERE te.subject_id IN ({placeholders}) ORDER BY t.name",
        tuple(subject_ids),
    )
    m = defaultdict(list)
    for r in rows:
        m[(r["stream_id"], r["subject_id"])].append(r["teacher_name"])
    return {k: " / ".join(v) for k, v in m.items()}


def subject_by_stream(res):
    """Subject x stream grid: mean %, entries and the teacher (per the timetable)."""
    tmap = teacher_map([s["id"] for s in res["subjects"]])
    rows = []
    for sub in res["subjects"]:
        cells = []
        for stream in res["streams"]:
            pcts = [s["scores"][sub["id"]]["pct"] for s in res["students"]
                    if s["stream_id"] == stream["id"] and sub["id"] in s["scores"]]
            if not pcts:
                cells.append(None)
                continue
            mean_pct = _mean(pcts)
            cells.append({
                "entries": len(pcts), "mean_pct": round(mean_pct, 2),
                "label": band_for(mean_pct, res["bands"])["label"],
                "teacher": tmap.get((stream["id"], sub["id"])),
            })
        if any(cells):
            rows.append({"subject": sub, "cells": cells})
    return rows


def teacher_performance(res):
    """Mean % of the students each teacher taught in this exam, based on who
    the timetable assigns to each stream+subject. Teachers who don't appear
    in the timetable for these classes can't be matched and are left out."""
    tmap = teacher_map([s["id"] for s in res["subjects"]])
    sub_name = {s["id"]: s["name"] for s in res["subjects"]}
    acc = defaultdict(lambda: {"pcts": [], "subjects": set(), "streams": set()})
    stream_name = {s["id"]: s["name"] for s in res["streams"]}
    for st in res["students"]:
        for sid, v in st["scores"].items():
            names = tmap.get((st["stream_id"], sid))
            if not names:
                continue
            for tname in names.split(" / "):
                a = acc[tname]
                a["pcts"].append(v["pct"])
                a["subjects"].add(sub_name[sid])
                a["streams"].add(stream_name.get(st["stream_id"], "No stream"))
    out = []
    for tname, a in acc.items():
        mean_pct = _mean(a["pcts"])
        out.append({
            "teacher": tname, "entries": len(a["pcts"]),
            "mean_pct": round(mean_pct, 2),
            "label": band_for(mean_pct, res["bands"])["label"],
            "subjects": ", ".join(sorted(a["subjects"])),
            "streams": ", ".join(sorted(a["streams"])), "rank": None,
        })
    _assign_positions(out, lambda o: o["mean_pct"], "rank")
    out.sort(key=lambda o: o["rank"])
    return out


# --------------------------------------------------------------------------
# Comparing exams / tracking a student
# --------------------------------------------------------------------------
def compare_exams(res_a, res_b):
    """Compare two exams of the same grade (A = earlier, B = later).
    Returns subject deviations and per-student movement."""
    stats_a = {s["id"]: s for s in subject_stats(res_a)}
    stats_b = {s["id"]: s for s in subject_stats(res_b)}
    subjects = []
    for sid, sb in stats_b.items():
        sa = stats_a.get(sid)
        if not sa or sa["mean_pct"] is None or sb["mean_pct"] is None:
            continue
        subjects.append({
            "name": sb["name"], "mean_a": sa["mean_pct"], "mean_b": sb["mean_pct"],
            "deviation": round(sb["mean_pct"] - sa["mean_pct"], 2),
            "label_a": sa["label"], "label_b": sb["label"],
        })
    subjects.sort(key=lambda r: r["deviation"], reverse=True)

    by_id_a = {s["id"]: s for s in res_a["ranked"]}
    students = []
    for sb in res_b["ranked"]:
        sa = by_id_a.get(sb["id"])
        if not sa:
            continue
        students.append({
            "id": sb["id"], "name": sb["name"], "admission_no": sb["admission_no"],
            "stream_name": sb["stream_name"],
            "mean_a": sa["mean_pct"], "mean_b": sb["mean_pct"],
            "deviation": round(sb["mean_pct"] - sa["mean_pct"], 2),
            "pos_a": sa["position"], "pos_b": sb["position"],
            "pos_change": sa["position"] - sb["position"],  # + = moved up
            "label_a": sa["grade_label"], "label_b": sb["grade_label"],
        })
    students.sort(key=lambda r: r["deviation"], reverse=True)

    def mean_of(res):
        return round(_mean([s["mean_pct"] for s in res["ranked"]]), 2) if res["ranked"] else None
    return {
        "subjects": subjects, "students": students,
        "grade_mean_a": mean_of(res_a), "grade_mean_b": mean_of(res_b),
    }


def student_history(student_id):
    """A student's results across every exam set for their grade,
    oldest first, each with the change from the previous exam."""
    stu = db.fetch_one("SELECT grade_id FROM students WHERE id=?", (student_id,))
    if not stu:
        return []
    # Every exam the student has marks in, whichever grade it was sat in, so
    # their progress carries across promotions.
    exams = db.fetch_all(
        "SELECT DISTINCT e.id, e.year, e.term FROM exams e JOIN marks m ON m.exam_id = e.id "
        "WHERE m.student_id=? ORDER BY e.year, e.term, e.id", (student_id,)
    )
    history, prev = [], None
    for e in exams:
        res = build_results(e["id"])
        row = next((s for s in res["students"] if s["id"] == student_id and s["n_subjects"]), None)
        if not row:
            continue
        item = {
            "exam": res["exam"], "mean_pct": row["mean_pct"], "grade_label": row["grade_label"],
            "position": row["position"], "stream_position": row["stream_position"],
            "of": len(res["ranked"]), "total": row["total"], "out_total": row["out_total"],
            "deviation": round(row["mean_pct"] - prev, 2) if prev is not None else None,
        }
        prev = row["mean_pct"]
        history.append(item)
    return history


def previous_exam_id(exam):
    """The exam for the same grade immediately before this one, if any."""
    rows = db.fetch_all(
        "SELECT id, year, term FROM exams WHERE grade_id=? ORDER BY year, term, id",
        (exam["grade_id"],),
    )
    prev = None
    for r in rows:
        if r["id"] == exam["id"]:
            return prev
        prev = r["id"]
    return None


# --------------------------------------------------------------------------
# Tiny dependency-free chart (works offline in the desktop build)
# --------------------------------------------------------------------------
def trend_svg(values, labels=None, width=420, height=130):
    """Inline SVG line chart for a list of percentages (0-100)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    pad_l, pad_r, pad_t, pad_b = 34, 14, 14, 26
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(values)

    def xy(i, v):
        x = pad_l + (w * i / (n - 1) if n > 1 else w / 2)
        y = pad_t + h * (1 - max(0, min(100, v)) / 100.0)
        return x, y

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="Mean percentage trend" style="max-width:{width}px">']
    for g in (0, 50, 100):
        _, y = xy(0, g)
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="#e3dcc9" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" font-size="10" fill="#6f6a5b" '
                     f'text-anchor="end">{g}</text>')
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    path = " ".join(f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}" for i, v in pts)
    if len(pts) > 1:
        parts.append(f'<polyline points="{path}" fill="none" stroke="#1d5c3f" stroke-width="2.5"/>')
    for i, v in pts:
        x, y = xy(i, v)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#c8932b"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" font-size="10" fill="#103524" '
                     f'text-anchor="middle">{v:.0f}</text>')
        if labels:
            parts.append(f'<text x="{x:.1f}" y="{height - 8}" font-size="9" fill="#6f6a5b" '
                         f'text-anchor="middle">{labels[i]}</text>')
    parts.append("</svg>")
    return "".join(parts)
