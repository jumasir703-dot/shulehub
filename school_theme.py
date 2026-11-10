"""
school_theme.py - lets each school choose its own colours and style.

Administrators open  Menu > Theme & Colours  and either pick a ready-made theme or
enter their own two colours (a main colour and an accent) and a style:
  * Soft glass - rounded floating top bar, pastel cards, pill buttons
  * Classic    - keeps ShuleHub's original layout and only changes the colours
or switch back to the original ShuleHub green.

How it works: the choice is saved in a small table (theme_settings). /theme/theme.css
turns it into CSS variables (every shade is worked out from the two colours, and the
main colour is darkened automatically if white text would be hard to read on it),
and base.html loads that stylesheet after style.css. Nothing here changes any page.
"""
import colorsys
import hashlib
import os
import re

from flask import Blueprint, Response, current_app, flash, g, redirect, render_template, request, url_for

import database as db

theme_bp = Blueprint("themes", __name__, url_prefix="/theme")

# theme_settings has no id column, so the database layer must not append RETURNING id to its inserts.
_no_id = getattr(db, "TABLES_WITHOUT_ID", None)
if _no_id is not None:
    _no_id.add("theme_settings")

_DDL = "CREATE TABLE IF NOT EXISTS theme_settings (key TEXT PRIMARY KEY, value TEXT)"
_ready = False


def ensure_schema():
    global _ready
    if _ready:
        return
    conn = db.get_connection()
    try:
        conn.cursor().execute(_DDL)
        conn.commit()
    finally:
        conn.close()
    _ready = True

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# key, label, primary, accent, style
PRESETS = [
    {"key": "luminous", "label": "Luminous Blue", "primary": "#1a66ff", "accent": "#22d3ee", "style": "glass"},
    {"key": "navy", "label": "Royal Navy", "primary": "#1e3a8a", "accent": "#fbbf24", "style": "glass"},
    {"key": "teal", "label": "Ocean Teal", "primary": "#0f766e", "accent": "#fcd34d", "style": "glass"},
    {"key": "lavender", "label": "Lavender", "primary": "#6a50c8", "accent": "#f6d98b", "style": "glass"},
    {"key": "maroon", "label": "Maroon & Gold", "primary": "#8a1c2b", "accent": "#f6d98b", "style": "glass"},
    {"key": "classic-blue", "label": "Classic Blue", "primary": "#1d4ed8", "accent": "#f6d98b", "style": "classic"},
    {"key": "original", "label": "Original green", "primary": "#103524", "accent": "#f6d98b", "style": "classic"},
]
DEFAULT_KEY = "luminous"
_BY_KEY = {p["key"]: p for p in PRESETS}


# ------------------------------------------------------------------ colour maths
def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c))) for c in rgb)


def mix(a, b, t):
    """t=0 gives a, t=1 gives b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def _lum(h):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g_, b = (ch(c) for c in _rgb(h))
    return 0.2126 * r + 0.7152 * g_ + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable(colour, bg="#ffffff", minimum=4.5):
    """Darken `colour` until it has `minimum` contrast against `bg`. Returns (colour, changed)."""
    out, changed = colour, False
    for _ in range(40):
        if contrast(out, bg) >= minimum:
            break
        out, changed = mix(out, "#000000", 0.06), True
    return out, changed


def _pastel(base, hue_shift, light, sat):
    h, _l, _s = colorsys.rgb_to_hls(*(c / 255 for c in _rgb(base)))
    r, g_, b = colorsys.hls_to_rgb((h + hue_shift) % 1.0, light, sat)
    return _hex((r * 255, g_ * 255, b * 255))


def palette(primary, accent):
    """Every CSS variable the app and the glass style use, from just two colours."""
    p, adjusted = readable(primary, "#ffffff", 4.5)
    paper = mix(p, "#ffffff", 0.97)
    ink = mix(p, "#000000", 0.78)
    muted, _ = readable(mix(ink, "#ffffff", 0.42), paper, 4.5)
    r, g_, b = _rgb(p)
    pr, pg, pb = _rgb(paper)
    v = {
        "--green-900": p, "--green-800": mix(p, "#ffffff", .10), "--green-700": mix(p, "#ffffff", .20),
        "--green-600": mix(p, "#ffffff", .32), "--green-100": mix(p, "#ffffff", .88), "--green-50": mix(p, "#ffffff", .94),
        "--gold-500": mix(accent, "#ffffff", .45), "--gold-400": mix(accent, "#ffffff", .62),
        "--paper": paper, "--ink": ink, "--muted": muted, "--line": mix(p, "#ffffff", .82),
        "--purple-1": mix(p, "#ffffff", .20), "--purple-2": p,
        "--lav-1": mix(p, "#ffffff", .88), "--lav-2": mix(p, "#ffffff", .76),
        "--pastel-lav": mix(p, "#ffffff", .86),
        "--pastel-pink": _pastel(p, 0.12, .92, .80),
        "--pastel-yellow": _pastel(accent, 0.0, .90, .85),
        "--pastel-blue": _pastel(p, -0.07, .92, .80),
        "--glass": f"rgba({pr},{pg},{pb},.94)",
        "--rgb": f"{r},{g_},{b}",
    }
    return v, p, adjusted


# ------------------------------------------------------------------ storage
def _read():
    rows = db.fetch_all("SELECT key, value FROM theme_settings")
    return {r["key"]: r["value"] for r in rows}


def load():
    """Saved settings merged over the default preset. Never raises."""
    base = dict(_BY_KEY[DEFAULT_KEY])
    try:
        ensure_schema()
        saved = _read()
    except Exception as err:
        current_app.logger.warning("theme: using default (%s)", err)
        return base
    for k in ("primary", "accent"):
        if HEX.match(saved.get(k, "")):
            base[k] = saved[k].lower()
    if saved.get("style") in ("glass", "classic"):
        base["style"] = saved["style"]
    if saved.get("preset") in _BY_KEY or saved.get("preset") == "custom":
        base["key"] = saved["preset"]
    return base


def _save(values):
    ensure_schema()
    for k, v in values.items():
        db.execute("INSERT INTO theme_settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, v))


def _is_original(t):
    return t.get("key") == "original"


def current():
    """Small dict for templates: stylesheet version + browser theme colour. Cached per request."""
    cached = getattr(g, "_school_theme", None)
    if cached is not None:
        return cached
    t = load()
    if _is_original(t):
        info = {"version": "orig", "color": "#103524"}
    else:
        _v, main, _adj = palette(t["primary"], t["accent"])
        ver = hashlib.md5(f"{t['primary']}{t['accent']}{t['style']}".encode()).hexdigest()[:8]
        info = {"version": ver, "color": main}
    g._school_theme = info
    return info


@theme_bp.app_context_processor
def _inject():
    try:
        return {"school_theme": current()}
    except Exception:
        return {"school_theme": None}


# ------------------------------------------------------------------ the stylesheet
_RECOLOUR = """
/* hard-coded greens in the original stylesheet, pointed at the school's colour */
a{ color:var(--purple-2); }
.content [class*="card"] h2, .content [class*="card"] h3, .content [class*="card"] h4{ color:var(--ink) !important; }
.content [class*="card"] a, .content [class*="card"] [class*="go"], .content [class*="card"] [class*="link"]{ color:var(--purple-2) !important; }
.content [class*="icon"]:not(svg):not(img){ background:var(--purple-2) !important; color:#fff !important; }
.shell .menu-toggle{ background:var(--purple-2) !important; color:#fff !important; }
.shell .menu-panel a.active{ background:var(--purple-2) !important; color:#fff !important; }
.content button:not(.secondary):not(.ghost), .content .btn:not(.secondary), .content input[type=submit], .content a.button{ background:var(--purple-2); color:#fff !important; }
.content input[type=checkbox], .content input[type=radio]{ accent-color:var(--purple-2); }
"""


def _css_text(t):
    if _is_original(t):
        return "/* Original ShuleHub colours: nothing to override. */\n"
    v, _main, _adj = palette(t["primary"], t["accent"])
    out = [":root{"] + [f"  {k}:{val};" for k, val in v.items()] + [
        "  --radius:22px;", "  --shadow:0 10px 30px rgba(var(--rgb),.16), 0 2px 6px rgba(var(--rgb),.08);", "}"]
    out.append(_RECOLOUR)
    if t["style"] == "glass":
        path = os.path.join(current_app.static_folder or "static", "css", "theme-glass.css")
        try:
            with open(path, encoding="utf-8") as f:
                out.append(f.read())
        except OSError:
            current_app.logger.warning("theme: %s not found, using classic style", path)
    return "\n".join(out)


@theme_bp.route("/theme.css")
def css():
    resp = Response(_css_text(load()), mimetype="text/css")
    # the link carries ?v=<hash>, so a changed theme is a new URL and can be cached hard
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.args.get("v") else "no-cache"
    return resp


# ------------------------------------------------------------------ settings page (administrator only:
# these endpoints are deliberately not listed in permissions.py)
@theme_bp.route("/")
def index():
    t = load()
    return render_template("theme/settings.html", t=t, presets=PRESETS, active=t["key"])


@theme_bp.route("/save", methods=["POST"])
def save():
    key = request.form.get("preset", "custom")
    primary = request.form.get("primary", "").strip()
    accent = request.form.get("accent", "").strip()
    style = request.form.get("style", "glass")
    if key == "original":
        primary, accent, style = _BY_KEY["original"]["primary"], _BY_KEY["original"]["accent"], "classic"
    if not HEX.match(primary) or not HEX.match(accent) or style not in ("glass", "classic"):
        flash("Pick both colours using the colour boxes, and choose a style.", "error")
        return redirect(url_for("themes.index"))
    if key not in _BY_KEY:
        key = "custom"
    elif (primary.lower(), accent.lower(), style) != (_BY_KEY[key]["primary"], _BY_KEY[key]["accent"], _BY_KEY[key]["style"]):
        key = "custom"  # a preset the admin then tweaked
    _save({"preset": key, "primary": primary.lower(), "accent": accent.lower(), "style": style})
    _v, main, adjusted = palette(primary, accent)
    if adjusted and key != "original":
        flash(f"Theme saved. Your main colour was darkened a little to {main} so white text stays easy to read on it.", "success")
    else:
        flash("Theme saved. It now applies to everyone.", "success")
    return redirect(url_for("themes.index"))


@theme_bp.route("/reset", methods=["POST"])
def reset():
    try:
        ensure_schema()
        db.execute("DELETE FROM theme_settings", ())
    except Exception as err:
        current_app.logger.warning("theme reset failed: %s", err)
    flash("Theme reset to the default (Luminous Blue).", "success")
    return redirect(url_for("themes.index"))
