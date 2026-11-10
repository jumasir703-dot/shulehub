"""
apply_dashboard_search.py - plugs the Dashboard and Search into ShuleHub.

Run it from your ShuleHub folder (the one with app.py) AFTER copying in
dashboard.py, search.py, templates/dashboard/ and (already there) templates/search/:

    python apply_dashboard_search.py

It edits app.py, auth.py and templates/base.html. Safe to run twice. Nothing is
changed unless every edit can be placed, and each changed file is backed up next
to itself with the ending .ds.bak. Use this INSTEAD of apply_search.py.
"""
import os
import re
import shutil
import sys

COMMON_ADD = ('"dashboard.index", "search.index", "search.suggest", '
              '"library.catalogue", "library.book_detail"')
CSS = '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/search.css\') }}">'
JS = '<script src="{{ url_for(\'static\', filename=\'js/search.js\') }}" defer></script>'
MENU = ("{% if 'dashboard' in loaded_modules %}\n"
        "        <a href=\"{{ url_for('dashboard.index') }}\" class=\"{{ 'active' if ep.startswith('dashboard.') else '' }}\">Dashboard</a>\n"
        "        {% endif %}")


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def edit_app(t):
    new = re.sub(r'\(\("library", "library_bp"\), \("messaging", "msg_bp"\)\)',
                 '(("library", "library_bp"), ("messaging", "msg_bp"), '
                 '("search", "search_bp"), ("dashboard", "dashboard_bp"))', t, count=1) \
        if '"search_bp"' not in t else t
    if '"search_bp"' not in new:
        return None, 'couldn\'t find the line with (("library", "library_bp"), ("messaging", "msg_bp"))'
    return new, "added" if new != t else "already done"


def edit_auth(t):
    if "dashboard.index" in t:
        return t, "already done"
    new, n = re.subn(r'^COMMON\s*=\s*\{\s*"home",', 'COMMON = {"home", ' + COMMON_ADD + ',', t, count=1, flags=re.M)
    return (new, "added") if n else (None, "couldn't find the line starting COMMON = {\"home\",")


def edit_base(t):
    new = t
    if "url_for('dashboard.index')" not in new:
        m = re.search(r"^([ \t]*)<a href=\"\{\{ url_for\('home'\) \}\}\".*$", new, re.M)
        if not m:
            return None, "couldn't find the Home link in the menu"
        new = new[:m.end()] + "\n" + m.group(1) + MENU + new[m.end():]
    if "css/search.css" not in new:
        if "</head>" not in new:
            return None, "couldn't find </head>"
        new = new.replace("</head>", CSS + "\n" + JS + "\n</head>", 1)
    return new, "added" if new != t else "already done"


def main():
    for f in ("app.py", "auth.py", "dashboard.py", "search.py",
              os.path.join("templates", "base.html"),
              os.path.join("templates", "dashboard", "index.html"),
              os.path.join("templates", "search", "results.html")):
        if not os.path.exists(f):
            sys.exit(f"Can't find {f}. Run this from your ShuleHub folder after copying the new files in.")
    jobs = [("app.py", edit_app), ("auth.py", edit_auth), (os.path.join("templates", "base.html"), edit_base)]
    done, bad = {}, []
    for name, fn in jobs:
        text, crlf = load(name)
        new, status = fn(text)
        if new is None:
            bad.append((name, status))
        else:
            done[name] = (new, crlf, status, new != text)
    if bad:
        print("Nothing was changed, because I couldn't place everything:")
        for n, why in bad:
            print(f"  - {n}: {why}")
        sys.exit(1)
    for name, (new, crlf, status, changed) in done.items():
        if changed:
            if not os.path.exists(name + ".ds.bak"):
                shutil.copyfile(name, name + ".ds.bak")
            with open(name, "w", encoding="utf-8", newline="") as f:
                f.write(new.replace("\n", "\r\n") if crlf else new)
        print(f"  {status + ':':14}{name}")
    print("\nDone. Close ShuleHub completely, then run:  python run_desktop.py")


if __name__ == "__main__":
    main()
