"""
theme.py — ShuleHub's colours for everything that is NOT the website.

The website takes its colours from the variables at the top of
static/style.css. PDFs and Excel exports are built in Python and cannot read
that file, so they take the same colours from here. Keep the two in step: if
you change a colour in one, change it in the other. (Names match the CSS
variables: GREEN_900 <-> --green-900, GOLD_100 <-> --gold-100, ...)

Nothing else in the code base should hard-code a colour; import from here.
"""

from reportlab.lib.colors import HexColor

# --- the palette (same values as :root in static/style.css) ------------------
GREEN_900   = HexColor("#103524")   # headings, table header bars
GREEN_700   = HexColor("#1d5c3f")
GREEN_500   = HexColor("#2f7d57")
GOLD_500    = HexColor("#c8932b")   # accent
GOLD_100    = HexColor("#f4e6c6")   # soft header / break rows
INK         = HexColor("#1b2b22")   # body text
MUTED       = HexColor("#655f50")   # secondary text (passes AA on every surface)
LINE        = HexColor("#e3dcc9")   # soft grid lines
LINE_STRONG = HexColor("#b5ad96")   # firmer grid lines on dense tables
ROW_ALT     = HexColor("#fbf8f0")   # alternate-row tint
DANGER      = HexColor("#a3372c")

# The official-portal look used for timetables (web page and PDF): navy header
# bars, pale-blue label column, cool grey grid. Mirrors --portal-* in style.css.
PORTAL_NAVY   = HexColor("#12356b")
PORTAL_BLUE   = HexColor("#1f5fa8")
PORTAL_TINT   = HexColor("#e9f0fa")   # period/label column
PORTAL_BREAK  = HexColor("#eef1f6")   # break rows
PORTAL_LINE   = HexColor("#aab8cf")   # grid lines
PORTAL_MUTED  = HexColor("#52607a")   # teacher / room text

# Same colours as bare "RRGGBB" strings, which openpyxl (Excel) wants.
_HEX = {
    "GREEN_900": "103524", "GREEN_700": "1d5c3f", "GREEN_500": "2f7d57",
    "GOLD_500": "c8932b", "GOLD_100": "f4e6c6", "INK": "1b2b22",
    "MUTED": "655f50", "LINE": "e3dcc9", "LINE_STRONG": "b5ad96",
    "ROW_ALT": "fbf8f0", "DANGER": "a3372c",
    "PORTAL_NAVY": "12356b", "PORTAL_BLUE": "1f5fa8", "PORTAL_TINT": "e9f0fa",
    "PORTAL_BREAK": "eef1f6", "PORTAL_LINE": "aab8cf", "PORTAL_MUTED": "52607a",
}


def css(name):
    """'PORTAL_MUTED' -> '#52607a' (for <font color=...> inside PDF text)."""
    return "#" + _HEX[name]


def hex6(name):
    """'GREEN_900' -> '103524' (for Excel fills and fonts)."""
    return _HEX[name].upper()
