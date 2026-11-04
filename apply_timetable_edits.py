"""
apply_timetable_edits.py - one-time helper for the Master and Remedial timetables.

Run it from your ShuleHub folder (the one that contains app.py):

    python apply_timetable_edits.py

It adds the few lines these features need to app.py, permissions.py and
templates/base.html. It is safe to run twice (it skips anything already there)
and it never touches anything else in those files. A backup of each file it
changes is saved next to it with the ending .bak.
"""
import os
import shutil
import sys

EDITS = [
    # (file, text that means "already done", line to anchor on, text to add, "after"/"before")
    ("app.py", "from master import master_bp",
     "from fees import fees_bp, outstanding_total\n", "from master import master_bp\n", "after"),
    ("app.py", "from remedial import remedial_bp",
     "from fees import fees_bp, outstanding_total\n", "from remedial import remedial_bp\n", "after"),
    ("app.py", "app.register_blueprint(master_bp)",
     "app.register_blueprint(fees_bp)\n", "app.register_blueprint(master_bp)\n", "after"),
    ("app.py", "app.register_blueprint(remedial_bp)",
     "app.register_blueprint(fees_bp)\n", "app.register_blueprint(remedial_bp)\n", "after"),
    ("permissions.py", "_REMEDIAL_VIEW",
     "_PREFIXES = (\n",
     '# Remedial (extra hours) timetable: viewers can view and export it; everything\n'
     '# else on it (setup, plan, generate, clear) is for people who can edit timetables.\n'
     '_REMEDIAL_VIEW = {"remedial.view_page", "remedial.export_csv", "remedial.export_pdf"}\n\n', "before"),
    ("permissions.py", '("master.", "timetable_view")',
     "_PREFIXES = (\n", '    ("master.", "timetable_view"),\n', "after"),
    ("permissions.py", 'if endpoint in _REMEDIAL_VIEW',
     '    if endpoint in _TIMETABLE_EDIT:\n        return "timetable_edit"\n',
     '    if endpoint in _REMEDIAL_VIEW:\n        return "timetable_view"\n'
     '    if endpoint.startswith("remedial."):\n        return "timetable_edit"\n', "after"),
]

BASE_LINK_MASTER = ('        <a href="{{ url_for(\'master.index\') }}" class="{{ \'active\' if ep.startswith(\'master.\') '
                    'else \'\' }}">Master Timetable</a>\n')
BASE_LINK_REMEDIAL = ('        <a href="{{ url_for(\'remedial.view_page\') }}" class="{{ \'active\' if '
                      'ep.startswith(\'remedial.\') else \'\' }}">Remedial Timetable</a>\n')


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def save(path, text):
    if not os.path.exists(path + ".bak"):
        shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def main():
    problems = []
    texts = {}
    for name in ("app.py", "permissions.py", "templates/base.html"):
        if not os.path.exists(name):
            sys.exit(f"Can't find {name}. Run this from your ShuleHub folder.")
        raw = load(name)
        texts[name] = (raw, "\r\n" in raw)

    def norm(name):
        raw, crlf = texts[name]
        return raw.replace("\r\n", "\n"), crlf

    changed = set()
    for name, marker, anchor, new, where in EDITS:
        text, crlf = norm(name)
        if marker in text:
            print(f"  already done: {name}  ({marker.strip()[:48]})")
            continue
        if text.count(anchor) != 1:
            problems.append((name, marker))
            continue
        text = text.replace(anchor, anchor + new if where == "after" else new + anchor, 1)
        texts[name] = (text.replace("\n", "\r\n") if crlf else text, crlf)
        changed.add(name)
        print(f"  added:        {name}  ({marker.strip()[:48]})")

    # menu links, in templates/base.html
    text, crlf = norm("templates/base.html")
    view_link = "View / Export</a>\n"
    if text.count(view_link) != 1:
        problems.append(("templates/base.html", "menu links"))
    else:
        added = ""
        if "url_for('master.index')" not in text:
            added += BASE_LINK_MASTER
        if "url_for('remedial.view_page')" not in text:
            added += BASE_LINK_REMEDIAL
        if added:
            text = text.replace(view_link, view_link + added, 1)
            texts["templates/base.html"] = (text.replace("\n", "\r\n") if crlf else text, crlf)
            changed.add("templates/base.html")
            print("  added:        templates/base.html  (menu links)")
        else:
            print("  already done: templates/base.html  (menu links)")

    for name in changed:
        save(name, texts[name][0])

    if problems:
        print("\nCouldn't place these automatically (your file differs from what I expected):")
        for name, marker in problems:
            print(f"  - {name}: {marker}")
        print("Tell Claude which ones and paste that part of the file; nothing else was changed for them.")
        sys.exit(1)
    print("\nDone. Start the app as usual.")


if __name__ == "__main__":
    main()
