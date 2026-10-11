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
