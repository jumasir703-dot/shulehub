"""
apply_master_timetable.py - wires the Master Timetable module into ShuleHub.

Run automatically by build_windows.bat (step 0), or by hand:
    python apply_master_timetable.py

What it does (every step is safe to repeat; a step already done is skipped):
  1. app.py            import master_bp and register it
  2. permissions.py    give "master." pages the timetable_view service
  3. templates/base.html   add a "Master Timetable" menu link
  4. ShuleHub.spec     add 'master' to PyInstaller hiddenimports

Original files are copied to _patch_backup/ the first time they are changed.
If an anchor line cannot be found the patcher stops with an error (exit 1)
and writes NOTHING to that file, so the build never runs on a half-patched app.
Uses only the Python standard library.
"""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(ROOT, "_patch_backup")

errors = []
changed = []


def _read(rel):
    path = os.path.join(ROOT, rel)
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(rel, text):
    path = os.path.join(ROOT, rel)
    os.makedirs(BACKUP, exist_ok=True)
    bak = os.path.join(BACKUP, rel.replace("/", "__").replace("\\", "__"))
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    changed.append(rel)


def _nl(text):
    """The file's own line ending, so CRLF files stay CRLF."""
    return "\r\n" if "\r\n" in text else "\n"


def _insert_after_line(text, anchor_startswith, new_line):
    """Insert new_line after the first line starting with anchor_startswith.
    Returns new text, or None if the anchor is missing."""
    nl = _nl(text)
    lines = text.split(nl)
    for i, line in enumerate(lines):
        if line.strip().startswith(anchor_startswith):
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i + 1, indent + new_line)
            return nl.join(lines)
    return None


def _insert_before_line(text, anchor_startswith, new_line):
    nl = _nl(text)
    lines = text.split(nl)
    for i, line in enumerate(lines):
        if line.strip().startswith(anchor_startswith):
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i, indent + new_line)
            return nl.join(lines)
    return None


def patch_app():
    rel = "app.py"
    text = _read(rel)
    new = text
    if "from master import master_bp" not in new:
        out = _insert_after_line(new, "from terms import terms_bp", "from master import master_bp")
        if out is None:
            errors.append("app.py: could not find 'from terms import terms_bp' to add the import after.")
            return
        new = out
    if "app.register_blueprint(master_bp)" not in new:
        out = _insert_after_line(new, "app.register_blueprint(terms_bp)", "app.register_blueprint(master_bp)")
        if out is None:
            errors.append("app.py: could not find 'app.register_blueprint(terms_bp)' to register after.")
            return
        new = out
    if new != text:
        _write(rel, new)


def patch_permissions():
    rel = "permissions.py"
    text = _read(rel)
    if '("master.", "timetable_view")' in text:
        return
    out = _insert_before_line(text, '("analytics.", "exams"),', '("master.", "timetable_view"),')
    if out is None:
        errors.append("permissions.py: could not find the _PREFIXES list ('(\"analytics.\", \"exams\"),').")
        return
    _write(rel, out)


NAV_LINK = (
    "<a href=\"{{ url_for('master.index') }}\" "
    "class=\"{{ 'active' if ep.startswith('master.') else '' }}\">Master Timetable</a>"
)


def patch_base():
    rel = "templates/base.html"
    text = _read(rel)
    if "url_for('master.index')" in text:
        return
    nl = _nl(text)
    lines = text.split(nl)
    for i, line in enumerate(lines):
        if "url_for('view_page')" in line and "View / Export" in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i + 1, indent + NAV_LINK)
            _write(rel, nl.join(lines))
            return
    errors.append("templates/base.html: could not find the 'View / Export' menu link.")


def patch_spec():
    rel = "ShuleHub.spec"
    if not os.path.exists(os.path.join(ROOT, rel)):
        return
    text = _read(rel)
    if "'master'" in text:
        return
    anchor = "'terms', 'theme', 'invigilation',"
    if anchor in text:
        _write(rel, text.replace(anchor, "'master', " + anchor, 1))
    else:
        # PyInstaller still finds master through app.py's import, so this is
        # only a warning, not a build-stopper.
        print("note: ShuleHub.spec hiddenimports line not found; relying on app.py import.")


def main():
    for needed in ("master.py", os.path.join("templates", "master", "index.html"),
                   "app.py", "permissions.py", os.path.join("templates", "base.html")):
        if not os.path.exists(os.path.join(ROOT, needed)):
            print("ERROR: missing %s - unzip the update INTO the ShuleHub folder "
                  "(the one that contains app.py), then run again." % needed)
            return 1

    patch_app()
    patch_permissions()
    patch_base()
    patch_spec()

    if errors:
        print("MASTER TIMETABLE PATCH FAILED:")
        for e in errors:
            print("  - " + e)
        print("Nothing was half-applied to the files listed above. Send me this message.")
        return 1

    if changed:
        print("Master Timetable wired in. Updated: " + ", ".join(changed))
        print("Originals saved in _patch_backup\\")
    else:
        print("Master Timetable already wired in - nothing to change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
