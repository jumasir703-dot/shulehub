"""
charts.py - bar and line charts for ShuleHub statistics.

Dependency-free inline SVG (works offline in the desktop build, prints well, no
JavaScript). Every chart carries hover tooltips (native <title>), a legend when
there is more than one series, and a "Show as table" view of the same numbers.

Templates call the chart_* functions below (registered as Jinja globals by
init_app). A chart can never break a page: any error returns an empty string.
"""
from datetime import date, timedelta
from html import escape

from markupsafe import Markup

import database as db

# validated categorical pair (colour-blind safe); series 1 is always green
GREEN = "#238c5a"
GOLD = "#c8932b"
SERIES = (GREEN, GOLD)
INK = "var(--ink,#1b2b22)"
MUTED = "var(--muted,#6f6a5b)"
GRID = "var(--line,#e3dcc9)"
SURFACE = "var(--paper,#fffdf8)"

_STYLE = ("<style>.grid-even>.card{min-width:0;overflow-x:auto}.ch{margin:6px 0 2px}.ch svg{display:block;width:100%;max-width:620px;height:auto;overflow:visible}"
          ".ch .m{transition:opacity .12s}.ch .m:hover{opacity:.78}"
          ".ch-legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:.82rem;color:" + INK + ";margin:8px 0 0}"
          ".ch-legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}"
          ".ch details{margin-top:8px;font-size:.84rem}.ch summary{cursor:pointer;color:" + MUTED + "}"
          ".ch details table{margin-top:6px}</style>")


# ------------------------------------------------------------------ helpers
def _nice_max(v):
    if v <= 0:
        return 1
    for step in (1, 2, 2.5, 5, 10):
        for p in range(-2, 12):
            top = step * 10 ** p
            if top >= v:
                return top
    return v


def _ticks(top, n=4):
    return [top * i / n for i in range(n + 1)]


def _fmt(v, unit):
    if v is None:
        return "-"
    if unit == "%":
        return f"{v:.0f}%" if abs(v - round(v)) < 0.05 else f"{v:.1f}%"
    if unit == "KES":
        a = abs(v)
        if a >= 1_000_000:
            return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
        if a >= 1_000:
            return f"{v / 1_000:.0f}K" if a >= 10_000 else f"{v / 1_000:.1f}K".replace(".0K", "K")
        return f"{v:.0f}"
    return f"{v:g}"


def _full(v, unit):
    if unit == "KES":
        return f"KES {v:,.0f}"
    return _fmt(v, unit)


def _short(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _legend(series):
    if len(series) < 2:
        return ""
    return ('<div class="ch-legend">' + "".join(
        f'<span><i style="background:{SERIES[i % 2]}"></i>{escape(s["name"])}</span>' for i, s in enumerate(series))
        + "</div>")


def _table(categories, series, unit, head="Item"):
    rows = "".join(
        "<tr><td>" + escape(str(c)) + "</td>" + "".join(
            f'<td class="num">{escape(_full(s["values"][i], unit)) if s["values"][i] is not None else "-"}</td>'
            for s in series) + "</tr>" for i, c in enumerate(categories))
    heads = "".join(f'<th class="num">{escape(s["name"])}</th>' for s in series)
    return (f'<details><summary>Show as table</summary><div class="table-scroll"><table>'
            f"<thead><tr><th>{escape(head)}</th>{heads}</tr></thead><tbody>{rows}</tbody></table></div></details>")


def _wrap(svg, series, categories, unit, head):
    return Markup(f'<div class="ch">{_STYLE}{svg}{_legend(series)}{_table(categories, series, unit, head)}</div>')


def _top_round_bar(x, y, w, h, r=4):
    """A bar anchored on the baseline with rounded data-end (top) corners."""
    r = max(0, min(r, w / 2, h))
    return (f"M{x:.1f},{y + h:.1f} L{x:.1f},{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"L{x + w - r:.1f},{y:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} L{x + w:.1f},{y + h:.1f} Z")


# ------------------------------------------------------------------ vertical bars
def vbar(categories, series, unit="", y_max=None, ref=None, ref_label="", head="Item", aria=""):
    """Grouped vertical bars. series = [{"name": str, "values": [..]}] (1 or 2)."""
    n, k = len(categories), len(series)
    if not n or not any(v for s in series for v in s["values"] if v):
        return ""
    W, H = 520, 250
    pl, pr, pt = 46, 12, 22
    rotate = n > 6 or max(len(str(c)) for c in categories) > 11 and n > 4
    pb = 62 if rotate else 34
    pw, ph = W - pl - pr, H - pt - pb
    top = y_max or _nice_max(max(v for s in series for v in s["values"] if v is not None))
    y = lambda v: pt + ph * (1 - v / top)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(aria or "Bar chart")}">']
    for t in _ticks(top):
        out.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{pl - 6}" y="{y(t) + 4:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">{escape(_fmt(t, unit))}</text>')
    group = pw / n
    bw = min(44, group * 0.72 / k)
    show_vals = n * k <= 8 or (k == 1 and n <= 10)
    for i, cat in enumerate(categories):
        gx = pl + group * i + (group - (bw * k + 2 * (k - 1))) / 2
        for j, s in enumerate(series):
            v = s["values"][i]
            if v is None:
                continue
            h = max(0.0, ph * v / top)
            x = gx + j * (bw + 2)
            tip = f'{cat} - {s["name"]}: {_full(v, unit)}' if k > 1 else f"{cat}: {_full(v, unit)}"
            if h > 0:
                out.append(f'<path class="m" d="{_top_round_bar(x, y(v), bw, h)}" fill="{SERIES[j % 2]}"><title>{escape(tip)}</title></path>')
            if show_vals:
                out.append(f'<text x="{x + bw / 2:.1f}" y="{y(v) - 5:.1f}" font-size="12" font-weight="600" fill="{INK}" text-anchor="middle">{escape(_fmt(v, unit))}</text>')
        lx = pl + group * i + group / 2
        label = _short(cat, 14 if rotate else 12)
        if rotate:
            out.append(f'<text transform="translate({lx:.1f},{H - pb + 14}) rotate(-35)" font-size="12" fill="{INK}" text-anchor="end">{escape(label)}<title>{escape(str(cat))}</title></text>')
        else:
            out.append(f'<text x="{lx:.1f}" y="{H - pb + 18}" font-size="12.5" fill="{INK}" text-anchor="middle">{escape(label)}<title>{escape(str(cat))}</title></text>')
    out.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="{MUTED}" stroke-width="1"/>')
    if ref is not None and 0 <= ref <= top:
        out.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y(ref):.1f}" y2="{y(ref):.1f}" stroke="{INK}" stroke-width="1.5" stroke-dasharray="5 4"/>')
        if ref_label:
            out.append(f'<text x="{W - pr}" y="{y(ref) - 4:.1f}" font-size="11.5" fill="{INK}" text-anchor="end">{escape(ref_label)}</text>')
    out.append("</svg>")
    return _wrap("".join(out), series, categories, unit, head)


# ------------------------------------------------------------------ horizontal bars
def hbar(labels, values, unit="", max_value=None, ref=None, ref_label="", name="Value", head="Item", aria=""):
    """One horizontal bar per item, the value written at the bar's end."""
    items = [(l, v) for l, v in zip(labels, values) if v is not None]
    if not items:
        return ""
    row, W = 28, 520
    pl, pr, pt = 130, 50, 6
    pb = 24 if ref is not None else 8
    H = pt + row * len(items) + pb
    top = max_value or _nice_max(max(v for _, v in items))
    pw = W - pl - pr
    x = lambda v: pl + pw * v / top
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(aria or "Bar chart")}">']
    for i, (lab, v) in enumerate(items):
        yy = pt + row * i
        w = max(0.0, pw * v / top)
        out.append(f'<text x="{pl - 8}" y="{yy + row / 2 + 4:.1f}" font-size="13" fill="{INK}" text-anchor="end">{escape(_short(lab, 19))}<title>{escape(str(lab))}</title></text>')
        if w > 0:
            r = min(4, w)
            out.append(f'<path class="m" d="M{pl},{yy + 4} L{pl + w - r:.1f},{yy + 4} Q{pl + w:.1f},{yy + 4} {pl + w:.1f},{yy + 4 + r:.1f} '
                       f'L{pl + w:.1f},{yy + row - 4 - r:.1f} Q{pl + w:.1f},{yy + row - 4} {pl + w - r:.1f},{yy + row - 4} L{pl},{yy + row - 4} Z" '
                       f'fill="{GREEN}"><title>{escape(f"{lab}: {_full(v, unit)}")}</title></path>')
        out.append(f'<text x="{pl + w + 6:.1f}" y="{yy + row / 2 + 4:.1f}" font-size="12.5" font-weight="600" fill="{INK}">{escape(_fmt(v, unit))}</text>')
    out.append(f'<line x1="{pl}" x2="{pl}" y1="{pt}" y2="{H - pb}" stroke="{MUTED}" stroke-width="1"/>')
    if ref is not None and 0 <= ref <= top:
        out.append(f'<line x1="{x(ref):.1f}" x2="{x(ref):.1f}" y1="{pt}" y2="{H - pb + 4}" stroke="{INK}" stroke-width="1.5" stroke-dasharray="5 4"/>')
        if ref_label:
            out.append(f'<text x="{x(ref):.1f}" y="{H - 6}" font-size="11.5" fill="{INK}" text-anchor="middle">{escape(ref_label)}</text>')
    out.append("</svg>")
    return _wrap("".join(out), [{"name": name, "values": [v for _, v in items]}], [l for l, _ in items], unit, head)


# ------------------------------------------------------------------ lines
def line(categories, series, unit="", y_min=0, y_max=None, ref=None, ref_label="", head="Item", aria=""):
    """Line chart (1-2 series); None values leave a gap."""
    n = len(categories)
    if n < 2 or not any(v is not None for s in series for v in s["values"]):
        return ""
    W, H = 520, 240
    pl, pr, pt, pb = 46, 18, 20, 36
    pw, ph = W - pl - pr, H - pt - pb
    vals = [v for s in series for v in s["values"] if v is not None]
    top = y_max or _nice_max(max(vals))
    span = (top - y_min) or 1
    px = lambda i: pl + pw * i / (n - 1)
    py = lambda v: pt + ph * (1 - (v - y_min) / span)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(aria or "Line chart")}">']
    for k in range(5):
        t = y_min + span * k / 4
        out.append(f'<line x1="{pl}" x2="{W - pr}" y1="{py(t):.1f}" y2="{py(t):.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{pl - 6}" y="{py(t) + 4:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">{escape(_fmt(t, unit))}</text>')
    if ref is not None and y_min <= ref <= top:
        out.append(f'<line x1="{pl}" x2="{W - pr}" y1="{py(ref):.1f}" y2="{py(ref):.1f}" stroke="{INK}" stroke-width="1.5" stroke-dasharray="5 4"/>')
        if ref_label:
            out.append(f'<text x="{W - pr}" y="{py(ref) - 4:.1f}" font-size="11.5" fill="{INK}" text-anchor="end">{escape(ref_label)}</text>')
    step = max(1, -(-n // 8))
    for i, c in enumerate(categories):
        if i % step == 0 or i == n - 1:
            anchor = "start" if i == 0 and n > 2 else ("end" if i == n - 1 and n > 2 else "middle")
            out.append(f'<text x="{px(i):.1f}" y="{H - 12}" font-size="12" fill="{INK}" text-anchor="{anchor}">{escape(_short(c, 22))}<title>{escape(str(c))}</title></text>')
    label_all = n <= 8
    for j, s in enumerate(series):
        col = SERIES[j % 2]
        run = []
        for i, v in enumerate(s["values"]):
            if v is None:
                if len(run) > 1:
                    out.append(f'<polyline points="{" ".join(run)}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
                run = []
                continue
            run.append(f"{px(i):.1f},{py(v):.1f}")
        if len(run) > 1:
            out.append(f'<polyline points="{" ".join(run)}" fill="none" stroke="{col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
        last = max((i for i, v in enumerate(s["values"]) if v is not None), default=None)
        for i, v in enumerate(s["values"]):
            if v is None:
                continue
            tip = f'{categories[i]} - {s["name"]}: {_full(v, unit)}' if len(series) > 1 else f"{categories[i]}: {_full(v, unit)}"
            out.append(f'<circle class="m" cx="{px(i):.1f}" cy="{py(v):.1f}" r="4.5" fill="{col}" stroke="{SURFACE}" stroke-width="2"><title>{escape(tip)}</title></circle>')
            if (label_all or i == last) and len(series) == 1:
                anc = "start" if i == 0 else ("end" if i == n - 1 else "middle")
                out.append(f'<text x="{px(i) + (4 if i == 0 else (-4 if i == n - 1 else 0)):.1f}" y="{py(v) - 11:.1f}" font-size="12" font-weight="600" fill="{INK}" text-anchor="{anc}">{escape(_fmt(v, unit))}</text>')
            elif i == last:
                out.append(f'<text x="{px(i) - 8:.1f}" y="{py(v) - 10:.1f}" font-size="12" font-weight="600" fill="{INK}" text-anchor="end">{escape(_fmt(v, unit))}</text>')
    out.append("</svg>")
    return _wrap("".join(out), series, categories, unit, head)


def _floor10(v):
    """Lowest axis value for a percentage line: 10 below the data, never above 80."""
    return max(0, min(80, int((v - 10) // 10) * 10))


# ------------------------------------------------------------------ safe wrapper
def _safe(fn):
    def run(*a, **k):
        try:
            return fn(*a, **k) or ""
        except Exception:      # a chart must never take a page down
            return ""
    run.__name__ = fn.__name__
    return run


# ------------------------------------------------------------------ exam results
@_safe
def chart_streams(streams):
    streams = [s for s in streams if s.get("mean_pct") is not None]
    if len(streams) < 2:
        return ""
    return vbar([s["name"] for s in streams], [{"name": "Mean %", "values": [s["mean_pct"] for s in streams]}],
                unit="%", y_max=100, head="Stream", aria="Mean percentage by stream")


@_safe
def chart_subjects(subjects, pass_pct=None):
    rows = [s for s in subjects if s.get("entries") and s.get("mean_pct") is not None]
    rows.sort(key=lambda s: -s["mean_pct"])
    if not rows:
        return ""
    return hbar([s["name"] for s in rows], [s["mean_pct"] for s in rows], unit="%", max_value=100,
                ref=pass_pct, ref_label=f"pass mark {pass_pct:g}%" if pass_pct else "",
                name="Mean %", head="Subject", aria="Mean percentage by subject")


@_safe
def chart_grade_progress(exam):
    """Grade mean across every exam set for this grade, oldest first."""
    import exam_analysis as ea
    exams = db.fetch_all(
        "SELECT id, name, term, year FROM exams WHERE grade_id=? ORDER BY year, term, id", (exam["grade_id"],))
    if len(exams) < 2:
        return ""
    cats, vals = [], []
    for e in exams:
        res = ea.build_results(e["id"])
        ranked = res["ranked"] if res else []
        if not ranked:
            continue
        cats.append(f"T{e['term']} {e['year']} {e['name']}")
        vals.append(round(sum(s["mean_pct"] for s in ranked) / len(ranked), 1))
    if len(cats) < 2:
        return ""
    return line(cats, [{"name": "Grade mean %", "values": vals}], unit="%", y_max=100,
                head="Exam", aria="Grade mean percentage across exams")


@_safe
def chart_compare(result, res_a, res_b):
    subs = result["subjects"] if result else []
    if not subs:
        return ""
    return vbar([s["name"] for s in subs],
                [{"name": res_a["exam"]["name"], "values": [s["mean_a"] for s in subs]},
                 {"name": res_b["exam"]["name"], "values": [s["mean_b"] for s in subs]}],
                unit="%", y_max=100, head="Subject", aria="Subject means in the two exams")


# ------------------------------------------------------------------ attendance
@_safe
def chart_attendance_classes(classes):
    rows = []
    for c in classes:
        if c.get("marked"):
            present = c["counts"]["P"] + c["counts"]["L"]
            rows.append((c["label"], round(100.0 * present / c["marked"], 1)))
    if not rows:
        return ""
    return hbar([r[0] for r in rows], [r[1] for r in rows], unit="%", max_value=100, ref=80, ref_label="80%",
                name="Attendance %", head="Class", aria="Attendance by class")


@_safe
def chart_attendance_trend(cls, start, end):
    """Daily attendance % for one class between two ISO dates."""
    import attendance as att
    ids = [s["id"] for s in att._class_students(cls)]
    if not ids:
        return ""
    marks = ",".join("?" for _ in ids)
    days = {}
    for r in db.fetch_all(
            f"SELECT att_date, status, COUNT(*) AS c FROM attendance WHERE student_id IN ({marks}) "
            "AND att_date >= ? AND att_date <= ? GROUP BY att_date, status", tuple(ids) + (start, end)):
        d = days.setdefault(r["att_date"], {"p": 0, "n": 0})
        d["n"] += r["c"]
        if r["status"] in ("P", "L"):
            d["p"] += r["c"]
    dates = sorted(days)
    if len(dates) < 2:
        return ""
    cats = [date.fromisoformat(d).strftime("%d %b") for d in dates]
    return line(cats, [{"name": "Attendance %", "values": [round(100.0 * days[d]["p"] / days[d]["n"], 1) for d in dates]}],
                unit="%", y_min=_floor10(min(100.0 * days[d]["p"] / days[d]["n"] for d in dates)), y_max=100, ref=att.LOW_ATTENDANCE_PCT, ref_label=f"{att.LOW_ATTENDANCE_PCT:g}%",
                head="Day", aria="Daily attendance for the class")


# ------------------------------------------------------------------ fees
@_safe
def chart_fees_grades(by_grade):
    rows = [g for g in by_grade if g["billed"] or g["paid"]]
    if not rows:
        return ""
    return vbar([g["grade"] for g in rows],
                [{"name": "Collected", "values": [g["paid"] for g in rows]},
                 {"name": "Still owing", "values": [g["owing"] for g in rows]}],
                unit="KES", head="Grade", aria="Fees collected and still owing, by grade")


@_safe
def chart_fees_months(months=6):
    """Money collected in each of the last few months (voided payments left out)."""
    today = date.today()
    first = today.replace(day=1)
    keys = []
    for _ in range(months):
        keys.append(first.strftime("%Y-%m"))
        first = (first - timedelta(days=1)).replace(day=1)
    keys.reverse()
    totals = {k: 0.0 for k in keys}
    for r in db.fetch_all("SELECT paid_on, amount FROM payments WHERE voided=0 AND paid_on >= ?", (keys[0] + "-01",)):
        k = str(r["paid_on"])[:7]
        if k in totals:
            totals[k] += float(r["amount"] or 0)
    if not any(totals.values()):
        return ""
    cats = [date(int(k[:4]), int(k[5:]), 1).strftime("%b %Y") for k in keys]
    return vbar(cats, [{"name": "Collected", "values": [totals[k] for k in keys]}], unit="KES",
                head="Month", aria="Fees collected per month")


def init_app(app):
    app.jinja_env.globals.update(
        chart_streams=chart_streams, chart_subjects=chart_subjects, chart_grade_progress=chart_grade_progress,
        chart_compare=chart_compare, chart_attendance_classes=chart_attendance_classes,
        chart_attendance_trend=chart_attendance_trend, chart_fees_grades=chart_fees_grades,
        chart_fees_months=chart_fees_months)
