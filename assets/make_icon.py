"""
make_icon.py — draws the ShuleHub icon and exports every size the app needs.

Run from the project folder:   python assets/make_icon.py

Design: a graduation cap (the "school" half) sitting above a row of timetable
cells (the "hub" half), cream and gold on the app's deep green.

Writes:
  assets/shulehub.ico            Windows exe + shortcut icon (16 ... 256 px)
  assets/shulehub-1024.png       master artwork
  static/icons/icon-192.png      PWA / browser icons
  static/icons/icon-512.png
  static/icons/icon-maskable-512.png   full-bleed version for Android masks
  static/favicon.ico
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

GREEN = (16, 53, 36)
GREEN_LIGHT = (30, 84, 58)
CREAM = (251, 248, 240)
CREAM_SHADE = (222, 214, 193)
GOLD = (232, 168, 36)

S = 4              # supersampling factor for smooth edges
SIZE = 1024


def _artwork(content_scale=1.0, rounded=True):
    """Returns the 1024x1024 RGBA icon. content_scale < 1 shrinks the artwork
    inside the square (used for the maskable icon's safe zone)."""
    big = SIZE * S
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # background: deep green with a lighter top glow, as a rounded square
    bg = Image.new("RGBA", (big, big), GREEN)
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-big * 0.2, -big * 0.55, big * 1.2, big * 0.55), fill=GREEN_LIGHT + (255,))
    bg = Image.alpha_composite(bg, glow)
    mask = Image.new("L", (big, big), 0)
    md = ImageDraw.Draw(mask)
    if rounded:
        md.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=255)
    else:
        md.rectangle((0, 0, big, big), fill=255)
    img.paste(bg, (0, 0), mask)

    # everything below is drawn on a 1024 grid and mapped through `p`
    cx = cy = big / 2

    def p(x, y):
        return (cx + (x * S - cx) * content_scale, cy + (y * S - cy) * content_scale)

    def poly(points, fill):
        d.polygon([p(x, y) for x, y in points], fill=fill)

    # cap base (the part that sits on the head), with a rounded underside
    poly([(290, 470), (512, 575), (734, 470), (734, 640), (512, 735), (290, 640)], CREAM_SHADE)
    # cap board (the flat diamond)
    poly([(512, 190), (920, 372), (512, 554), (104, 372)], CREAM)
    # soft shadow line under the board's front edge
    poly([(512, 554), (920, 372), (920, 392), (512, 574), (104, 392), (104, 372)], CREAM_SHADE)
    # button + tassel
    cx0, cy0 = p(512, 372)
    r = 26 * S * content_scale
    d.ellipse((cx0 - r, cy0 - r, cx0 + r, cy0 + r), fill=GOLD)
    d.line([p(512, 372), p(850, 508)], fill=GOLD, width=int(12 * S * content_scale))
    d.line([p(850, 508), p(850, 640)], fill=GOLD, width=int(14 * S * content_scale))
    tx, ty = p(850, 668)
    tr = 34 * S * content_scale
    d.ellipse((tx - tr, ty - tr, tx + tr, ty + tr), fill=GOLD)

    # timetable cells: one gold "today" cell among cream ones
    cell_w, cell_h, gap, top = 126, 96, 30, 790
    n = 5
    total = n * cell_w + (n - 1) * gap
    left = (1024 - total) / 2
    for i in range(n):
        x0 = left + i * (cell_w + gap)
        a = p(x0, top)
        b = p(x0 + cell_w, top + cell_h)
        d.rounded_rectangle((a[0], a[1], b[0], b[1]), radius=int(20 * S * content_scale),
                            fill=GOLD if i == 2 else CREAM)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    master = _artwork()
    master.save(os.path.join(HERE, "shulehub-1024.png"))

    os.makedirs(os.path.join(ROOT, "static", "icons"), exist_ok=True)
    master.resize((512, 512), Image.LANCZOS).save(os.path.join(ROOT, "static", "icons", "icon-512.png"))
    master.resize((192, 192), Image.LANCZOS).save(os.path.join(ROOT, "static", "icons", "icon-192.png"))
    _artwork(content_scale=0.78, rounded=False).resize((512, 512), Image.LANCZOS).save(
        os.path.join(ROOT, "static", "icons", "icon-maskable-512.png"))

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(os.path.join(HERE, "shulehub.ico"), format="ICO", sizes=sizes)
    master.save(os.path.join(ROOT, "static", "favicon.ico"), format="ICO",
                sizes=[(16, 16), (32, 32), (48, 48)])
    print("Icon files written.")


if __name__ == "__main__":
    main()
