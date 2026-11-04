"""
apply_search.py - connects the new search to ShuleHub.

Run it from your ShuleHub folder (the one that contains app.py):

    python apply_search.py

It (1) registers the search blueprint in app.py, and (2) adds the search stylesheet and
script to templates/base.html. Safe to run twice. A backup of each file it changes is
saved next to it with the ending .bak (an existing .bak is never overwritten).
It also checks that permissions.py already knows the "search." pages and tells you if not.
"""
import os, re, shutil, sys

CSS = '    <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/search.css\') }}">\n'
JS = '    <script src="{{ url_for(\'static\', filename=\'js/search.js\') }}" defer></script>\n'


def load(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def save(p, t):
    if not os.path.exists(p + ".bak"):
        shutil.copyfile(p, p + ".bak")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(t)


def patch_app(manual):
    if not os.path.exists("app.py"):
        sys.exit("Run this from the ShuleHub folder (app.py not found).")
    t = load("app.py")
    nl = "\r\n" if "\r\n" in t else "\n"
    new = t
    if "from search import search_bp" not in new:
        m = re.search(r"^app\s*=\s*Flask\(", new, re.M)
        if m:
            new = new[:m.start()] + "from search import search_bp" + nl + new[m.start():]
        else:
            manual.append("app.py: add the line  from search import search_bp  near the other imports")
    if "app.register_blueprint(search_bp)" not in new:
        last = None
        for m in re.finditer(r"^app\.register_blueprint\(.*\)[ \t]*\r?$", new, re.M):
            last = m
        if last:
            new = new[:last.end()] + nl + "app.register_blueprint(search_bp)" + new[last.end():]
        else:
            manual.append("app.py: add the line  app.register_blueprint(search_bp)  after the other register_blueprint lines")
    if new != t:
        save("app.py", new)
        print("app.py: search registered.")
    else:
        print("app.py: already done.")


def patch_base(manual):
    p = os.path.join("templates", "base.html")
    if not os.path.exists(p):
        manual.append("templates/base.html not found")
        return
    t = load(p)
    new = t
    if "css/search.css" not in new:
        if "</head>" in new:
            new = new.replace("</head>", CSS + "</head>", 1)
        else:
            manual.append("base.html: add the search.css link before </head>")
    if "js/search.js" not in new:
        if "</head>" in new:
            new = new.replace("</head>", JS + "</head>", 1)
        else:
            manual.append("base.html: add the search.js script tag")
    if new != t:
        save(p, new)
        print("templates/base.html: search style and script added.")
    else:
        print("templates/base.html: already done.")


def main():
    manual = []
    patch_app(manual)
    patch_base(manual)
    if os.path.exists("permissions.py") and "search." not in load("permissions.py"):
        manual.append('permissions.py has no "search." entry yet. Without one the search page may be refused. '
                      "Send permissions.py and auth.py to Claude to add it.")
    for f in ("search.py", os.path.join("templates", "search", "results.html")):
        if not os.path.exists(f):
            manual.append(f"{f} is missing from the ShuleHub folder")
    if manual:
        print("\nStill to do by hand:")
        for m in manual:
            print(" -", m)
    else:
        print("\nDone. Restart the app and refresh the browser (Ctrl+F5).")


main()
