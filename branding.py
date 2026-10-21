"""
branding.py — the school logo: validation, storage helpers and the PDF image.

The logo lives in the database (table `school_logo`) rather than as a file, so
it survives on hosts with a temporary disk and travels with a backup of the
database. Whatever is uploaded is re-encoded as a PNG no larger than 600 px,
which also strips anything unexpected from the file. SVG is refused.
"""

import io
from datetime import datetime

from PIL import Image, UnidentifiedImageError

import database as db

MAX_UPLOAD_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 25_000_000      # refuse absurdly large images before decoding them
MAX_SIDE = 600
ALLOWED = {"PNG", "JPEG", "GIF", "WEBP"}


def prepare_logo(raw):
    """-> (png_bytes, error). Validates and normalises an uploaded image."""
    if not raw:
        return None, "Choose an image file first."
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, f"That image is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."
    head = raw[:300].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return None, "SVG logos aren't supported. Export it as a PNG or JPG and upload that."
    try:
        im = Image.open(io.BytesIO(raw))
        if im.format not in ALLOWED:
            return None, "Use a PNG, JPG, GIF or WebP image."
        if im.width * im.height > MAX_PIXELS:
            return None, "That image has too many pixels. Resize it to under about 4000 × 4000 and try again."
        im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None, "That file couldn't be read as an image."
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue(), None


def save_logo(png_bytes):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    db.execute(
        "INSERT INTO school_logo (id, mime, data, updated_at) VALUES (1, 'image/png', ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET mime = excluded.mime, data = excluded.data, "
        "updated_at = excluded.updated_at",
        (png_bytes, stamp))


def remove_logo():
    db.execute("DELETE FROM school_logo WHERE id=1")


def get_logo():
    """-> (bytes, version) or None."""
    row = db.fetch_one("SELECT data, updated_at FROM school_logo WHERE id=1")
    if not row or not row["data"]:
        return None
    return bytes(row["data"]), row["updated_at"]


def logo_version():
    """Cheap check used on every page: the version string, or None if no logo."""
    try:
        row = db.fetch_one("SELECT updated_at FROM school_logo WHERE id=1")
    except Exception:
        return None
    return row["updated_at"] if row else None


def logo_flowable(max_height_mm=20, max_width_mm=45, found="fetch"):
    """A centred reportlab Image of the logo for the top of a PDF, or None.
    Pass `found=get_logo()` to reuse one lookup across many pages."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage
    if isinstance(found, str):
        found = get_logo()
    if not found:
        return None
    data, _ = found
    try:
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return None
    scale = min(max_height_mm * mm / h, max_width_mm * mm / w)
    img = RLImage(io.BytesIO(data), width=w * scale, height=h * scale)
    img.hAlign = "CENTER"
    return img


def logo_story(spacer_mm=2, found="fetch"):
    """[logo, spacer] or [] — so callers can simply do `story = branding.logo_story() + [...]`."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Spacer
    img = logo_flowable(found=found)
    return [img, Spacer(1, spacer_mm * mm)] if img else []


def xlsx_from_csv_response(resp, school_name):
    """Turn a CSV download into an Excel download with the school logo and name
    on top, so exports carry the logo too. Returns a new Response, or `resp`
    unchanged if it can't be converted. A CSV can't hold an image, hence .xlsx."""
    import csv
    import re
    from flask import Response
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    try:
        text = resp.get_data(as_text=True).lstrip("\ufeff")
        rows = list(csv.reader(io.StringIO(text)))
        disp = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename="?([^";]+)', disp)
        base = re.sub(r"\.csv$", "", m.group(1)) if m else "export"
        pretty = base.replace("_", " ").strip()
        wb = Workbook()
        ws = wb.active
        ws.title = "Export"
        row = 1
        found = get_logo()
        if found:
            img = XLImage(io.BytesIO(found[0]))
            scale = min(70 / img.height, 160 / img.width)
            img.width, img.height = int(img.width * scale), int(img.height * scale)
            ws.add_image(img, "A1")
            row = 5            # leave room for the picture (about 4 rows)
        ws.cell(row=row, column=1, value=school_name).font = Font(bold=True, size=14)
        ws.cell(row=row + 1, column=1, value=pretty).font = Font(size=11, color="555555")
        head_row = row + 3
        for r_i, r in enumerate(rows):
            for c_i, v in enumerate(r, start=1):
                v = v[1:] if v.startswith("\'") else v      # undo the CSV formula guard
                if r_i and re.fullmatch(r"-?\d+(\.\d+)?", v) and not (len(v) > 1 and v[0] == "0" and "." not in v) \
                        and not any(k in rows[0][c_i - 1].lower() for k in ("adm", "phone", "upi", "receipt", "reference")):
                    v = float(v) if "." in v else int(v)     # real numbers, so Excel can sum/sort them
                cell = ws.cell(row=head_row + r_i, column=c_i, value=v)
                if r_i == 0:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="103524")
                    cell.alignment = Alignment(horizontal="center")
        for c_i in range(1, (max((len(r) for r in rows), default=1)) + 1):
            width = max((len(str(r[c_i - 1])) for r in rows if len(r) >= c_i), default=8)
            ws.column_dimensions[get_column_letter(c_i)].width = min(max(width + 2, 10), 40)
        ws.freeze_panes = ws.cell(row=head_row + 1, column=1)
        out = io.BytesIO()
        wb.save(out)
    except Exception:
        return resp
    new = Response(out.getvalue(),
                   mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    new.headers["Content-Disposition"] = f"attachment;filename={base}.xlsx"
    return new
