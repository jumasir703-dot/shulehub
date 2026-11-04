"""
fix_permissions.py - finishes the one edit apply_timetable_edits.py couldn't place.

Run it from your ShuleHub folder (the one with permissions.py):

    python fix_permissions.py

It adds the Remedial permission lines to the top of service_for() in
permissions.py. Safe to run twice. A backup is saved as permissions.py.before_fix
"""
import os
import re
import shutil
import sys

PATH = "permissions.py"
if not os.path.exists(PATH):
    sys.exit("Can't find permissions.py. Run this from your ShuleHub folder.")

with open(PATH, encoding="utf-8", newline="") as f:
    raw = f.read()
crlf = "\r\n" in raw
text = raw.replace("\r\n", "\n")

if 'endpoint.startswith("remedial.")' in text or "startswith('remedial.')" in text:
    print("Already done: permissions.py maps remedial pages. Nothing to change.")
    sys.exit(0)

m = re.search(r"^def service_for\(\s*(\w+)[^)]*\)[^:\n]*:[ \t]*\n", text, re.M)
if not m:
    sys.exit("Couldn't find 'def service_for(...)' in permissions.py.\n"
             "Paste that part of the file to Claude and it will say where the lines go.")
arg = m.group(1)
pos = m.end()

# skip a docstring, if the function has one
lines = text[pos:].split("\n")
i = 0
while i < len(lines) and not lines[i].strip():
    i += 1
if i < len(lines):
    s = lines[i].strip()
    for q in ('"""', "'''"):
        if s.startswith(q):
            if s.count(q) >= 2 and len(s) > 3:
                i += 1
            else:
                i += 1
                while i < len(lines) and q not in lines[i]:
                    i += 1
                i += 1
            break
    else:
        i = 0  # no docstring: insert right after the def line
if i:
    pos += len("\n".join(lines[:i])) + 1

block = (
    f"    if {arg} in _REMEDIAL_VIEW:\n"
    f'        return "timetable_view"\n'
    f'    if {arg}.startswith("remedial."):\n'
    f'        return "timetable_edit"\n'
)
new = text[:pos] + block + text[pos:]

if "_REMEDIAL_VIEW" not in text:
    sys.exit("permissions.py has no _REMEDIAL_VIEW yet. Run apply_timetable_edits.py first.")

if not os.path.exists(PATH + ".before_fix"):
    shutil.copyfile(PATH, PATH + ".before_fix")
with open(PATH, "w", encoding="utf-8", newline="") as f:
    f.write(new.replace("\n", "\r\n") if crlf else new)
print("Done: permissions.py now maps the Remedial pages (view = timetable_view, the rest = timetable_edit).")
