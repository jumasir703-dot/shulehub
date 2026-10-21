"""
apply_patch.py  —  run once from the ShuleHub project folder:

    python apply_patch.py

Adds the Invigilation, Teacher Attendance and Lesson Attendance sections to
YOUR CURRENT app.py and templates/base.html by inserting a few lines. It does
not replace either file, so any changes you made since are kept. Backups are
saved as app.py.bak and base.html.bak. Safe to run twice (it skips what is
already there).
"""

import os
import shutil
import sys

APP, BASE = "app.py", os.path.join("templates", "base.html")

IMPORTS = [
    "from invigilation import invigilation_bp",
    "from teacher_attendance import teacher_attendance_bp",
    "from lesson_attendance import lesson_attendance_bp",
]
REGISTERS = [
    "app.register_blueprint(invigilation_bp)",
    "app.register_blueprint(teacher_attendance_bp)",
    "app.register_blueprint(lesson_attendance_bp)",
]

EXAM_LINK = """        {% if can('exams') %}
        <a href="{{ url_for('invigilation.index') }}" class="{{ 'active' if ep.startswith('invigilation.') else '' }}">Invigilation</a>
        {% endif %}
"""
STAFF_LINKS = """        {% if can('attendance') %}
        <a href="{{ url_for('teacher_attendance.index') }}" class="{{ 'active' if ep.startswith('teacher_attendance.') else '' }}">Teacher Attendance</a>
        <a href="{{ url_for('lesson_attendance.index') }}" class="{{ 'active' if ep.startswith('lesson_attendance.') else '' }}">Lesson Attendance</a>
        {% endif %}
"""


def backup(path):
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)


def patch_app():
    if not os.path.exists(APP):
        print(f"! {APP} not found. Run this from the ShuleHub project folder.")
        return False
    src = open(APP, encoding="utf-8").read()
    lines = src.split("\n")
    changed = False

    # imports: just before "app = Flask(__name__)"
    missing = [i for i in IMPORTS if i not in src]
    if missing:
        at = next((n for n, l in enumerate(lines) if l.strip().startswith("app = Flask(")), None)
        if at is None:
            print("! Could not find 'app = Flask(' in app.py. Add these lines by hand near the other imports:")
            print("\n".join("    " + m for m in missing))
            return False
        lines[at:at] = missing
        changed = True

    # registers: after the last existing app.register_blueprint(...) line
    src2 = "\n".join(lines)
    missing = [r for r in REGISTERS if r not in src2]
    if missing:
        last = max((n for n, l in enumerate(lines) if l.strip().startswith("app.register_blueprint(")), default=None)
        if last is None:
            print("! Could not find app.register_blueprint lines. Add these by hand:")
            print("\n".join("    " + m for m in missing))
            return False
        lines[last + 1:last + 1] = missing
        changed = True

    if changed:
        backup(APP)
        open(APP, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
        print("app.py: sections registered.")
    else:
        print("app.py: already up to date.")
    return True


def insert_before(lines, anchor, block, marker):
    if any(marker in l for l in lines):
        return False, True
    at = next((n for n, l in enumerate(lines) if l.strip() == anchor), None)
    if at is None:
        return False, False
    lines[at:at] = block.rstrip("\n").split("\n")
    return True, True


def patch_base():
    if not os.path.exists(BASE):
        print(f"! {BASE} not found.")
        return False
    lines = open(BASE, encoding="utf-8").read().split("\n")
    changed = False
    ok = True
    for anchor, block, marker, label in (
            ("{% if can('exams_admin') %}", EXAM_LINK, "invigilation.index", "Invigilation"),
            ("{% if can('fees') %}", STAFF_LINKS, "teacher_attendance.index", "Teacher/Lesson Attendance")):
        did, found = insert_before(lines, anchor, block, marker)
        changed = changed or did
        if not found:
            ok = False
            print(f"! Could not place the {label} menu link automatically. Add this inside the menu in base.html:")
            print(block)
    # lesson attendance link can be missing even if teacher attendance was added earlier
    if not any("lesson_attendance.index" in l for l in lines):
        at = next((n for n, l in enumerate(lines) if "teacher_attendance.index" in l), None)
        if at is not None:
            lines.insert(at + 1, STAFF_LINKS.split("\n")[2])
            changed = True
    if changed:
        backup(BASE)
        open(BASE, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
        print("templates/base.html: menu links added.")
    else:
        print("templates/base.html: already up to date." if ok else "templates/base.html: not changed.")
    return ok


def check_files():
    need = ["invigilation.py", "teacher_attendance.py", "lesson_attendance.py",
            os.path.join("templates", "invigilation", "index.html"),
            os.path.join("templates", "teacher_attendance", "index.html"),
            os.path.join("templates", "lesson_attendance", "index.html")]
    gone = [n for n in need if not os.path.exists(n)]
    if gone:
        print("! These files are missing. Unzip the update into this folder first:")
        for n in gone:
            print("   ", n)
    return not gone


if __name__ == "__main__":
    good = check_files()
    good = patch_app() and good
    good = patch_base() and good
    print("\nDone. Restart the app and check the Menu." if good else "\nFinished with warnings. See the lines marked !")
    sys.exit(0 if good else 1)
