"""
apply_timetable_edits.py - one-time helper for the Master Timetable, Remedial
Timetable and Staff Alerts (pager) features.

Run it from your ShuleHub folder (the one that contains app.py):

    python apply_timetable_edits.py

It adds the few lines these features need to app.py, permissions.py and
templates/base.html. It is safe to run again (it skips anything already there)
and it never changes anything else in those files. A backup of each file it
changes is saved next to it with the ending .bak.
"""
import os
import shutil
import sys

# (file, text that means "already done", line to anchor on, text to add, "after"/"before")
EDITS = [
    ("app.py", "from master import master_bp",
     "from fees import fees_bp, outstanding_total\n", "from master import master_bp\n", "after"),
    ("app.py", "from remedial import remedial_bp",
     "from fees import fees_bp, outstanding_total\n", "from remedial import remedial_bp\n", "after"),
    ("app.py", "from pager import pager_bp",
     "from fees import fees_bp, outstanding_total\n", "from pager import pager_bp\n", "after"),
    ("app.py", "app.register_blueprint(master_bp)",
     "app.register_blueprint(fees_bp)\n", "app.register_blueprint(master_bp)\n", "after"),
    ("app.py", "app.register_blueprint(remedial_bp)",
     "app.register_blueprint(fees_bp)\n", "app.register_blueprint(remedial_bp)\n", "after"),
    ("app.py", "app.register_blueprint(pager_bp)",
     "app.register_blueprint(fees_bp)\n", "app.register_blueprint(pager_bp)\n", "after"),
    ("permissions.py", "_REMEDIAL_VIEW",
     "_PREFIXES = (\n",
     '# Remedial (extra hours) timetable: viewers can view and export it; everything\n'
     '# else on it (setup, plan, generate, clear) is for people who can edit timetables.\n'
     '_REMEDIAL_VIEW = {"remedial.view_page", "remedial.export_csv", "remedial.export_pdf"}\n\n', "before"),
    ("permissions.py", '("master.", "timetable_view")',
     "_PREFIXES = (\n", '    ("master.", "timetable_view"),\n', "after"),
    ("permissions.py", '("pager.", "pager")',
     "_PREFIXES = (\n", '    ("pager.", "pager"),\n', "after"),
    ("permissions.py", 'if endpoint in _REMEDIAL_VIEW',
     '    if endpoint in _TIMETABLE_EDIT:\n        return "timetable_edit"\n',
     '    if endpoint in _REMEDIAL_VIEW:\n        return "timetable_view"\n'
     '    if endpoint.startswith("remedial."):\n        return "timetable_edit"\n', "after"),
    ("permissions.py", '("pager", "Send staff alerts"',
     "]\nSERVICE_KEYS = ",
     '    ("pager", "Send staff alerts",\n'
     '     "Send meeting and event alerts to staff (in the app and by SMS), see who has confirmed them, "\n'
     '     "and keep staff phone numbers. Everyone with a login can always read the alerts sent to them."),\n', "before"),
    # ---- charts (bar and line graphs)
    ("app.py", "charts.init_app(app)",
     "app.register_blueprint(fees_bp)\n", "import charts; charts.init_app(app)\n", "after"),
    ("templates/analytics/overview.html", "charts/_exam_overview.html",
     '<div class="card">\n  <h3>Report forms</h3>', None, "before"),
    ("templates/analytics/compare.html", "charts/_compare.html",
     '<div class="card">\n  <h3>Subject movement</h3>', None, "before"),
    ("templates/attendance/index.html", "charts/_attendance_day.html",
     '<div class="card">\n  <h3>Classes</h3>', None, "before"),
    ("templates/attendance/report.html", "charts/_attendance_trend.html",
     '<div class="card">\n  <h3>{{ cls.label }}</h3>', None, "before"),
    ("templates/fees/index.html", "charts/_fees.html",
     '<div class="grid-even" style="margin-top:0;">', None, "before"),
    # ---- several schools in one system
    ("app.py", "from schools_admin import schools_bp",
     "from fees import fees_bp, outstanding_total\n", "from schools_admin import schools_bp\n", "after"),
    ("app.py", "app.register_blueprint(schools_bp)",
     "app.register_blueprint(fees_bp)\n", "app.register_blueprint(schools_bp)\n", "after"),
    ("app.py", "tenancy.install(app)",
     "app.context_processor(auth.inject_user)\n", "import tenancy; tenancy.install(app)\n", "after"),
]
INCLUDE = '{% include "<P>" %}\n'

# templates/base.html is edited line by line (so a different indentation doesn't matter).
# (text that means "already done", how to recognise the anchor line, text to add, how many lines
#  below the anchor line the next line must look like, or None)
LINK_MASTER = ("<a href=\"{{ url_for('master.index') }}\" class=\"{{ 'active' if ep.startswith('master.') "
               "else '' }}\">Master Timetable</a>")
LINK_REMEDIAL = ("<a href=\"{{ url_for('remedial.view_page') }}\" class=\"{{ 'active' if "
                 "ep.startswith('remedial.') else '' }}\">Remedial Timetable</a>")


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def save(path, text):
    if not os.path.exists(path + ".bak"):
        shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _unique(lines, test):
    hits = [i for i, ln in enumerate(lines) if test(i, ln.strip())]
    return hits[0] if len(hits) == 1 else None


def base_html_edits(text):
    """-> (new text, [(label, 'added'/'already'/'problem')])"""
    lines = text.split("\n")
    report = []

    def put(index, new_line, where="before"):
        indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
        lines.insert(index if where == "before" else index + 1, indent + new_line)

    # the two timetable links go right after the View / Export link
    for marker, link, label in (("url_for('master.index')", LINK_MASTER, "menu link: Master Timetable"),
                                ("url_for('remedial.view_page')", LINK_REMEDIAL, "menu link: Remedial Timetable")):
        if marker in text:
            report.append((label, "already"))
            continue
        i = _unique(lines, lambda i, s: "url_for('view_page')" in s and s.endswith("View / Export</a>"))
        if i is None:
            report.append((label, "problem"))
            continue
        put(i, link, "after")
        text = "\n".join(lines)
        report.append((label, "added"))

    # the Schools link for group administrators, after School Settings
    if "url_for('schools.index')" in "\n".join(lines):
        report.append(("menu link: Schools", "already"))
    else:
        i = _unique(lines, lambda i, s: "url_for('settings_page')" in s and s.endswith("School Settings</a>"))
        if i is None:
            report.append(("menu link: Schools", "problem"))
        else:
            put(i, "{% if group_admin %}<a href=\"{{ url_for('schools.index') }}\" class=\"{{ 'active' if ep.startswith('schools.') else '' }}\">Our Schools</a>{% endif %}", "after")
            report.append(("menu link: Schools", "added"))

    # the pager: bell, menu section, pop-up script
    pager_edits = [
        ("schools/_switcher.html", "the school switcher",
         lambda i, s: s == '<nav class="menu">',
         '{% if user and school_choices and school_choices|length > 1 %}{% include "schools/_switcher.html" %}{% endif %}'),
        ("pager/_bell.html", "the bell",
         lambda i, s: s == '<nav class="menu">',
         '{% if pager_on and user %}{% include "pager/_bell.html" %}{% endif %}'),
        ("pager/_menu.html", "the menu section",
         lambda i, s: s == "{% if user %}" and i + 1 < len(lines)
         and lines[i + 1].strip().startswith('<div class="menu-sep">{{ user.full_name }}'),
         '{% if pager_on and user %}{% include "pager/_menu.html" %}{% endif %}'),
        ("pager/_poll.html", "the pop-up script",
         lambda i, s: s == "<script src=\"{{ url_for('static', filename='csrf.js') }}\"></script>",
         '{% if pager_on and user %}{% include "pager/_poll.html" %}{% endif %}'),
    ]
    for marker, label, test, new_line in pager_edits:
        if marker in "\n".join(lines):
            report.append((f"Staff Alerts: {label}", "already"))
            continue
        i = _unique(lines, test)
        if i is None:
            report.append((f"Staff Alerts: {label}", "problem"))
            continue
        put(i, new_line, "before")
        report.append((f"Staff Alerts: {label}", "added"))
    return "\n".join(lines), report


def main():
    files = ("app.py", "permissions.py", "templates/base.html", "templates/analytics/overview.html",
             "templates/analytics/compare.html", "templates/attendance/index.html",
             "templates/attendance/report.html", "templates/fees/index.html")
    texts = {}
    for name in files:
        if not os.path.exists(name):
            sys.exit(f"Can't find {name}. Run this from your ShuleHub folder.")
        raw = load(name)
        texts[name] = (raw.replace("\r\n", "\n"), "\r\n" in raw)

    problems, changed = [], set()

    for name, marker, anchor, new, where in EDITS:
        text, crlf = texts[name]
        if marker in text:
            print(f"  already done: {name}  ({marker.strip()[:48]})")
            continue
        if text.count(anchor) != 1:
            problems.append((name, marker))
            continue
        if new is None:
            new = INCLUDE.replace("<P>", marker)
        text = text.replace(anchor, anchor + new if where == "after" else new + anchor, 1)
        texts[name] = (text, crlf)
        changed.add(name)
        print(f"  added:        {name}  ({marker.strip()[:48]})")

    text, crlf = texts["templates/base.html"]
    new_text, report = base_html_edits(text)
    for label, state in report:
        if state == "problem":
            problems.append(("templates/base.html", label))
        else:
            print(f"  {'added:       ' if state == 'added' else 'already done:'} templates/base.html  ({label})")
    if new_text != text:
        texts["templates/base.html"] = (new_text, crlf)
        changed.add("templates/base.html")

    for name in changed:
        text, crlf = texts[name]
        save(name, text.replace("\n", "\r\n") if crlf else text)

    if problems:
        print("\nCouldn't place these automatically (your file differs from what I expected):")
        for name, marker in problems:
            print(f"  - {name}: {marker}")
        print("Tell Claude which ones and paste that part of the file; nothing else was changed for them.")
        sys.exit(1)
    print("\nDone. Start the app as usual.")


if __name__ == "__main__":
    main()
