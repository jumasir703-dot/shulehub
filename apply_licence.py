r"""
apply_licence.py - adds "Licence & Check-in" to ShuleHub.

Run it from your ShuleHub folder (the one with app.py and templates):

    python apply_licence.py          # install
    python apply_licence.py --off    # remove the menu link

It registers licence.py in app.py and adds a "Licence & Check-in" link to the menu (administrators only).
Before you build for a client, open checkin_config.py and put your ShuleHub Control address in SERVER_URL.
Safe to run twice. Backups: app.py.bak_licence and base.html.bak_licence.
"""
import os, re, shutil, sys

MENU = ("        <a href=\"{{ url_for('licence.index') }}\" class=\"{{ 'active' if ep.startswith('licence.') else '' }}\">"
        "Licence &amp; Check-in</a>")


def load(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def save(p, t):
    if not os.path.exists(p + ".bak_licence"):
        shutil.copyfile(p, p + ".bak_licence")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(t)


def base_path():
    for p in (os.path.join("templates", "base.html"), os.path.join("ShuleHub", "templates", "base.html")):
        if os.path.exists(p):
            return p


def main():
    bp = base_path()
    if not os.path.exists("app.py") or not bp:
        sys.exit("Run this from the ShuleHub folder (the one with app.py and the templates folder).\nCurrent folder: " + os.getcwd())
    base = load(bp)
    nl = "\r\n" if "\r\n" in base else "\n"
    strip = lambda t: re.sub(r"^[^\n]*url_for\('licence\.index'\)[^\n]*\r?\n", "", t, flags=re.M)
    if "--off" in sys.argv:
        save(bp, strip(base))
        return print("Menu link removed. Close ShuleHub completely and open it again.")
    missing = [f for f in ("licence.py", "checkin_config.py", os.path.join("templates", "licence", "index.html")) if not os.path.exists(f)]
    if missing:
        sys.exit("Unzip shulehub_licence.zip into this folder first. Missing: " + ", ".join(missing))
    todo = []
    t = load("app.py")
    new = t
    if "from licence import licence_bp" not in new:
        m = re.search(r"^app\s*=\s*Flask\(", new, re.M)
        if m:
            new = new[:m.start()] + "from licence import licence_bp" + nl + new[m.start():]
        else:
            todo.append("app.py: add  from licence import licence_bp  near the other imports")
    if "register_blueprint(licence_bp)" not in new:
        last = None
        for m in re.finditer(r"^app\.register_blueprint\(.*\)[ \t]*\r?$", new, re.M):
            last = m
        if last:
            new = new[:last.end()] + nl + "app.register_blueprint(licence_bp)" + new[last.end():]
        else:
            todo.append("app.py: add  app.register_blueprint(licence_bp)  after the other register_blueprint lines")
    if new != t:
        save("app.py", new)
        print("app.py: licence registered.")
    else:
        print("app.py: already done.")
    if "from licence import licence_bp" in new and "register_blueprint(licence_bp)" in new:
        b2 = strip(base)
        m = re.search(r"^.*url_for\('settings_page'\).*$", b2, re.M)
        if m:
            save(bp, b2[:m.end()] + nl + MENU + b2[m.end():])
            print("templates/base.html: menu link added.")
        else:
            todo.append("base.html: add inside the admin part of the menu:  " + MENU.strip())
    else:
        todo.append("base.html was not changed because licence is not registered in app.py yet")
    if todo:
        print("\nStill to do by hand:")
        for x in todo:
            print(" -", x)
    else:
        print("\nDone. Set SERVER_URL in checkin_config.py, rebuild (build_windows.bat), then Menu > Licence & Check-in.")


main()
