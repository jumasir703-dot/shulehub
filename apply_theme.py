r"""
apply_theme.py - switch ShuleHub to the Lavender Glass look (or back).

Run it from your ShuleHub folder, the one that has app.py AND the templates folder
(on your PC: C:\Users\USER\Desktop\SHULEHUB\ShuleHub):

    python apply_theme.py          # theme ON
    python apply_theme.py --off    # theme OFF (old look)

It adds one line to templates/base.html right after the style.css line, and changes the
phone/browser bar colour. Safe to run twice. A backup is saved once as base.html.bak_theme.
"""
import os, shutil, sys, re

LINK = '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/shulehub-theme.css\') }}?v=2">\n'
OLD_COLOR, NEW_COLOR = '#103524', '#6a50c8'


def find_base():
    for p in (os.path.join("templates", "base.html"),
              os.path.join("ShuleHub", "templates", "base.html")):
        if os.path.exists(p):
            return p
    return None


def main():
    path = find_base()
    if not path:
        sys.exit("I can't find templates\\base.html here.\n"
                 "Open the folder that contains app.py and the templates folder, then run this again.\n"
                 "Current folder: " + os.getcwd())
    root = os.path.dirname(os.path.dirname(path)) or "."
    if not os.path.exists(os.path.join(root, "static", "css", "shulehub-theme.css")):
        sys.exit("static\\css\\shulehub-theme.css is missing in " + os.path.abspath(root) +
                 ". Unzip theme_lavender_v2.zip into that folder first.")
    text = open(path, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in text else "\n"
    on = "shulehub-theme.css" in text
    if "--off" in sys.argv:
        if not on:
            return print("Theme is already off.")
        text = "".join(l for l in text.splitlines(True) if "shulehub-theme.css" not in l)
        text = text.replace('content="%s"' % NEW_COLOR, 'content="%s"' % OLD_COLOR)
        msg = "Theme OFF."
    else:
        if on:
            return print("Theme is already on. If you still see the old look, close ShuleHub fully, open it again and press Ctrl+F5.")
        m = re.search(r"^.*static['\"]\s*,\s*filename\s*=\s*['\"]style\.css['\"].*$", text, re.M)
        if not m:
            m = re.search(r"</head>", text)
            if not m:
                sys.exit("Could not find where to add the line in base.html.")
            text = text[:m.start()] + LINK.replace("\n", nl) + text[m.start():]
        else:
            text = text[:m.end()] + nl + LINK.rstrip("\n") + text[m.end():]
        text = text.replace('content="%s"' % OLD_COLOR, 'content="%s"' % NEW_COLOR)
        msg = "Theme ON."
    bak = path + ".bak_theme"
    if not os.path.exists(bak):
        shutil.copyfile(path, bak)
    open(path, "w", encoding="utf-8", newline="").write(text)
    print(msg, "(changed " + os.path.abspath(path) + ")")
    print("Now close ShuleHub completely, open it again, and press Ctrl+F5 if it opens in a browser.")


main()
