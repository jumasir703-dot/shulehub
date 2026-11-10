"""
apply_new_modules.py - plugs Clearance, Lesson Plans and the Attendance downloads into ShuleHub.

Run it from your ShuleHub folder (the one with app.py) AFTER copying in clearance.py,
lesson_plans.py, attendance_export.py and the templates folders:

    python apply_new_modules.py

It edits app.py, permissions.py and templates/base.html. Safe to run twice. Nothing is changed
unless every edit can be placed, and each changed file is backed up next to itself as *.nm.bak.
"""
import os
import re
import shutil
import sys

NEW_SERVICES = [
    '    ("clearance", "Student clearance",',
    '     "See which students are cleared by fees and the library, sign students off for other departments, '
    'and print clearance forms."),',
    '    ("lesson_plans", "Write lesson plans",',
    '     "Teachers: write lesson plans, submit them to the DOS, and see the score and comments. Each teacher sees only their own."),',
    '    ("lesson_plans_mark", "Mark lesson plans (DOS)",',
    '     "See every submitted lesson plan, give a score and comments, approve it or return it for revision, and download them."),',
]
LP_ANY = ("index", "new", "edit", "save", "view", "submit", "delete", "copy", "pdf", "all_pdf", "list_csv")
NEW_EXACT = (['    # lesson plans: teachers and markers (review is for markers only)']
             + [f'    "lp.{n}": ("lesson_plans", "lesson_plans_mark"),' for n in LP_ANY]
             + ['    "lp.review": ("lesson_plans_mark",),',
                '    # clearance departments and fee rules: administrator only',
                '    "clearance.settings": (), "clearance.settings_save": (), "clearance.dept_add": (), "clearance.dept_delete": (),'])
NEW_PREFIXES = ['    ("clearance.", "clearance"),', '    ("attexport.", "attendance"),']

MENU_SCHOOL = ("{% if 'attexport' in loaded_modules and can('attendance') %}\n"
               "        <a href=\"{{ url_for('attexport.index') }}\" class=\"{{ 'active' if ep.startswith('attexport.') else '' }}\">Download Attendance</a>\n"
               "        {% endif %}\n"
               "        {% if 'clearance' in loaded_modules and can('clearance') %}\n"
               "        <a href=\"{{ url_for('clearance.index') }}\" class=\"{{ 'active' if ep.startswith('clearance.') else '' }}\">Clearance</a>\n"
               "        {% endif %}")
MENU_LP = ("{% if 'lp' in loaded_modules and (can('lesson_plans') or can('lesson_plans_mark')) %}\n"
           "        <div class=\"menu-sep\">Academics</div>\n"
           "        <a href=\"{{ url_for('lp.index') }}\" class=\"{{ 'active' if ep.startswith('lp.') else '' }}\">Lesson Plans</a>\n"
           "        {% endif %}\n")
LOADER_ADD = [('"clearance", "clearance_bp"', '("clearance", "clearance_bp")'),
              ('"lesson_plans", "lp_bp"', '("lesson_plans", "lp_bp")'),
              ('"attendance_export", "attexport_bp"', '("attendance_export", "attexport_bp")')]


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def edit_app(t):
    m = re.search(r"^(\s*for _module, _blueprint in \()(.*)(\):\s*)$", t, re.M)
    if not m:
        return None, "couldn't find the line 'for _module, _blueprint in (...):' (run apply_dashboard_search.py first)"
    inner = m.group(2)
    for marker, entry in LOADER_ADD:
        if marker not in inner:
            inner += ", " + entry
    return t[:m.start(2)] + inner + t[m.end(2):], ("already done" if inner == m.group(2) else "added")


def edit_permissions(t):
    lines = t.split("\n")
    text = "\n".join(lines)
    if '("clearance", "Student clearance"' not in text:
        k = next((i for i, l in enumerate(lines) if re.match(r"^SERVICE_KEYS\s*=", l)), None)
        if k is None:
            return None, "couldn't find the line 'SERVICE_KEYS = ...'"
        close = next((j for j in range(k - 1, -1, -1) if lines[j].strip() == "]"), None)
        if close is None:
            return None, "couldn't find the end of the SERVICES list"
        lines[close:close] = NEW_SERVICES
    text = "\n".join(lines)
    if '"lp.index"' not in text:
        k = next((i for i, l in enumerate(lines) if re.match(r"^_EXACT\s*=\s*\{", l)), None)
        if k is None:
            return None, "couldn't find '_EXACT = {'"
        lines[k + 1:k + 1] = NEW_EXACT
    text = "\n".join(lines)
    if '("clearance.", "clearance")' not in text:
        k = next((i for i, l in enumerate(lines) if re.match(r"^_PREFIXES\s*=\s*\($", l)), None)
        if k is None:
            return None, "couldn't find '_PREFIXES = ('"
        lines[k + 1:k + 1] = NEW_PREFIXES
    new = "\n".join(lines)
    return new, ("added" if new != t else "already done")


def edit_base(t):
    new = t
    if "url_for('clearance.index')" not in new:
        m = re.search(r"^([ \t]*)\{% if role == 'admin' %\}\n[ \t]*<a href=\"\{\{ url_for\('auth\.staff'\)", new, re.M)
        if not m:
            return None, "couldn't find the Staff & Logins part of the menu"
        new = new[:m.start()] + m.group(1) + MENU_SCHOOL + "\n" + new[m.start():]
    if "url_for('lp.index')" not in new:
        m = re.search(r"^([ \t]*)\{% if user %\}\n[ \t]*<div class=\"menu-sep\">\{\{ user\.full_name", new, re.M)
        if not m:
            return None, "couldn't find the account part of the menu"
        new = new[:m.start()] + m.group(1) + MENU_LP + new[m.start():]
    return new, ("added" if new != t else "already done")


def main():
    need = ["app.py", "permissions.py", os.path.join("templates", "base.html"), "clearance.py", "lesson_plans.py",
            "attendance_export.py", os.path.join("templates", "clearance", "index.html"),
            os.path.join("templates", "lessonplans", "index.html"), os.path.join("templates", "attexport", "index.html")]
    for f in need:
        if not os.path.exists(f):
            sys.exit(f"Can't find {f}. Run this from your ShuleHub folder after copying the new files in.")
    jobs = [("app.py", edit_app), ("permissions.py", edit_permissions), (os.path.join("templates", "base.html"), edit_base)]
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
            if not os.path.exists(name + ".nm.bak"):
                shutil.copyfile(name, name + ".nm.bak")
            with open(name, "w", encoding="utf-8", newline="") as f:
                f.write(new.replace("\n", "\r\n") if crlf else new)
        print(f"  {status + ':':14}{name}")
    print("\nDone. Close ShuleHub completely, then start it again.")
    print("Then open Roles & Access and tick the new services for the right roles:")
    print("  Student clearance / Write lesson plans / Mark lesson plans (DOS)")


if __name__ == "__main__":
    main()
