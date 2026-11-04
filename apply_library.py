"""
apply_library.py - one-time helper that plugs the Library module into ShuleHub.

Before running it, extract the library zip into your ShuleHub folder (the one
that contains app.py) so that library.py and templates/library/ are there.
Then, from that same folder:

    python apply_library.py

It adds the few lines the library needs to app.py, permissions.py, auth.py and
templates/base.html. It is safe to run twice (it skips anything already there),
it never touches anything else in those files, and a backup of each file it
changes is saved next to it with the ending .library.bak.

If it can't find the place for one of the lines it changes NOTHING and tells you
which one, so your files are never left half-edited.
"""
import os
import re
import shutil
import sys

LIB_ENDPOINTS = '"library.catalogue", "library.book_detail"'

MENU_BLOCK = """\
<div class="menu-sep">Library</div>
{% if can('library') %}
<a href="{{ url_for('library.index') }}" class="{{ 'active' if ep.startswith('library.') and ep not in ('library.catalogue', 'library.book_detail', 'library.book_new', 'library.books_import') else '' }}">Library Desk</a>
{% endif %}
<a href="{{ url_for('library.catalogue') }}" class="{{ 'active' if ep in ('library.catalogue', 'library.book_detail', 'library.book_new', 'library.books_import') else '' }}">Library Catalogue</a>
"""


def load(path):
    with open(path, encoding="utf-8", newline="") as f:
        raw = f.read()
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def line_index(lines, pattern, start=0):
    for i in range(start, len(lines)):
        if re.search(pattern, lines[i]):
            return i
    return None


def edit_app(text):
    if "from library import library_bp" in text and "app.register_blueprint(library_bp)" in text:
        return text, "already done"
    lines = text.split("\n")
    if "from library import library_bp" not in text:
        i = line_index(lines, r"^from fees import ")
        if i is None:
            return None, "couldn't find the line 'from fees import ...'"
        lines.insert(i + 1, "from library import library_bp")
    if "app.register_blueprint(library_bp)" not in "\n".join(lines):
        i = line_index(lines, r"^app\.register_blueprint\(fees_bp\)")
        if i is None:
            return None, "couldn't find the line 'app.register_blueprint(fees_bp)'"
        lines.insert(i + 1, "app.register_blueprint(library_bp)")
    return "\n".join(lines), "added"


def edit_permissions(text):
    lines = text.split("\n")
    changed = False
    if '("library", ' not in text:
        k = line_index(lines, r"^SERVICE_KEYS\s*=")
        if k is None:
            return None, "couldn't find the line 'SERVICE_KEYS = ...'"
        close = None
        for j in range(k - 1, -1, -1):
            if lines[j].strip() == "]":
                close = j
                break
        if close is None:
            return None, "couldn't find the end of the SERVICES list"
        lines[close:close] = [
            '    ("library", "Library",',
            '     "Run the school library: add and import books, issue and return, fines, reports, labels and settings. '
            '(Every signed-in staff member can already search the catalogue.)"),',
        ]
        changed = True
    if '("library.", "library")' not in "\n".join(lines):
        k = line_index(lines, r"^_PREFIXES\s*=\s*\($")
        if k is None:
            return None, "couldn't find the line '_PREFIXES = ('"
        lines.insert(k + 1, '    ("library.", "library"),')
        changed = True
    return "\n".join(lines), ("added" if changed else "already done")


def edit_auth(text):
    if "library.catalogue" in text:
        return text, "already done"
    m = re.search(r"^COMMON\s*=\s*\{([^}]*)\}", text, re.M)
    if not m:
        return None, "couldn't find the line 'COMMON = {...}'"
    inner = m.group(1)
    stripped = inner.rstrip()
    sep = " " if stripped.endswith(",") else ", "
    new_inner = stripped + sep + LIB_ENDPOINTS + inner[len(stripped):]
    return text[:m.start(1)] + new_inner + text[m.end(1):], "added"


def edit_base(text):
    if "url_for('library.catalogue')" in text:
        return text, "already done"
    lines = text.split("\n")
    s = line_index(lines, r"School Management</div>")
    if s is None:
        return None, "couldn't find the 'School Management' menu heading"
    # the {% if ... %} line that opens that menu section
    o = None
    for j in range(s - 1, -1, -1):
        if lines[j].lstrip().startswith("{% if"):
            o = j
            break
    if o is None:
        return None, "couldn't find where the 'School Management' menu section starts"
    indent = re.match(r"\s*", lines[o]).group(0)
    block = [(indent + ln) if ln else ln for ln in MENU_BLOCK.rstrip("\n").split("\n")] + [""]
    lines[o:o] = block
    return "\n".join(lines), "added"


FILES = [
    ("app.py", edit_app),
    ("permissions.py", edit_permissions),
    ("auth.py", edit_auth),
    ("templates/base.html", edit_base),
]


def main():
    if not os.path.exists("library.py") or not os.path.isdir("templates/library"):
        sys.exit("library.py and templates/library/ aren't here yet.\n"
                 "Extract the library zip into your ShuleHub folder first, then run this again from that folder.")
    results, failures = {}, []
    for name, fn in FILES:
        if not os.path.exists(name):
            sys.exit(f"Can't find {name}. Run this from your ShuleHub folder (the one that contains app.py).")
        text, crlf = load(name)
        new, status = fn(text)
        if new is None:
            failures.append((name, status))
        else:
            results[name] = (new, crlf, status, new != text)

    if failures:
        print("Nothing was changed, because I couldn't place everything:")
        for name, why in failures:
            print(f"  - {name}: {why}")
        print("\nPaste that part of the file to Claude and it will tell you exactly what to add.")
        sys.exit(1)

    for name, (new, crlf, status, changed) in results.items():
        if changed:
            if not os.path.exists(name + ".library.bak"):
                shutil.copyfile(name, name + ".library.bak")
            with open(name, "w", encoding="utf-8", newline="") as f:
                f.write(new.replace("\n", "\r\n") if crlf else new)
        print(f"  {status + ':':14}{name}")
    print("\nDone. Start the app as usual - you'll see a 'Library' section in the menu."
          "\nGive the librarian access on Roles & Access (tick 'Library'), or use the admin login.")


if __name__ == "__main__":
    main()
