"""
apply_theme_system.py - plugs the school Theme system into ShuleHub.

Run it from your ShuleHub folder (the one with app.py) AFTER copying in school_theme.py,
static/css/theme-glass.css and templates/theme/ (and the new dashboard.py):

    python apply_theme_system.py

It edits app.py, auth.py and templates/base.html. Safe to run twice. Nothing is changed
unless every edit can be placed, and each changed file is backed up next to itself with
the ending .theme.bak.
"""
import os
import re
import shutil
import sys

LINK = "<link rel=\"stylesheet\" href=\"{{ url_for('themes.css', v=school_theme.version) }}\">"
LINK_BLOCK = "{% if school_theme is defined and school_theme %}" + LINK + "{% endif %}"
META = '<meta name="theme-color" content="{{ school_theme.color if school_theme is defined and school_theme else \'#103524\' }}">'
MENU = ("{% if 'themes' in loaded_modules %}\n"
        "        <a href=\"{{ url_for('themes.index') }}\" class=\"{{ 'active' if ep.startswith('themes.') else '' }}\">Theme &amp; Colours</a>\n"
        "        {% endif %}")


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def edit_app(t):
    new = t
    if '"school_theme"' not in new:
        new = new.replace('("dashboard", "dashboard_bp"))', '("dashboard", "dashboard_bp"), ("school_theme", "theme_bp"))', 1)
        if '"school_theme"' not in new:
            return None, 'couldn\'t find ("dashboard", "dashboard_bp")) in the loader line (run apply_dashboard_search.py first)'
    if '"themes.css"' not in new:
        new, n = re.subn(r"(_ONBOARDING_EXEMPT_ENDPOINTS\s*=\s*\{)", r'\1"themes.css", ', new, count=1)
        if not n:
            return None, "couldn't find _ONBOARDING_EXEMPT_ENDPOINTS"
    return new, "added" if new != t else "already done"


def edit_auth(t):
    if '"themes.css"' in t:
        return t, "already done"
    new, n = re.subn(r"^(PUBLIC\s*=\s*\{)", r'\1"themes.css", ', t, count=1, flags=re.M)
    return (new, "added") if n else (None, "couldn't find the line starting PUBLIC = {")


def edit_base(t):
    new = t
    if "themes.css" not in new:
        m = re.search(r"^.*filename='style\.css'.*$", new, re.M)
        if not m:
            return None, "couldn't find the style.css link"
        new = new[:m.end()] + "\n" + LINK_BLOCK + new[m.end():]
    if "school_theme.color" not in new:
        new, n = re.subn(r'<meta name="theme-color" content="[^"]*">', lambda _m: META, new, count=1)
        if not n:
            return None, "couldn't find the theme-color meta tag"
    if "url_for('themes.index')" not in new:
        m = re.search(r"^([ \t]*)<a href=\"\{\{ url_for\('settings_page'\) \}\}\".*$", new, re.M)
        if not m:
            return None, "couldn't find the School Settings menu link"
        new = new[:m.end()] + "\n" + m.group(1) + MENU + new[m.end():]
    return new, "added" if new != t else "already done"


def main():
    for f in ("app.py", "auth.py", "school_theme.py", os.path.join("templates", "base.html"),
              os.path.join("static", "css", "theme-glass.css"),
              os.path.join("templates", "theme", "settings.html")):
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
            if not os.path.exists(name + ".theme.bak"):
                shutil.copyfile(name, name + ".theme.bak")
            with open(name, "w", encoding="utf-8", newline="") as f:
                f.write(new.replace("\n", "\r\n") if crlf else new)
        print(f"  {status + ':':14}{name}")
    print("\nDone. Close ShuleHub completely, then run:  python run_desktop.py")
    print("Then open Menu > Theme & Colours (administrator).")


if __name__ == "__main__":
    main()
