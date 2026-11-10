"""
rename_workflow.py - renames workflow.py to results_workflow.py and fixes the imports.

Why: PyInstaller has a built-in rule for a third-party package called "workflow", so a
project file with that name makes build_windows.bat crash. Nothing else changes.

Run from your ShuleHub folder (the one with app.py):   python rename_workflow.py
Backups of every file it edits are saved next to them as  *.wf.bak
Safe to run twice.
"""
import glob
import os
import re
import shutil
import sys

OLD, NEW = "workflow", "results_workflow"

if not os.path.exists("app.py"):
    sys.exit("Run this from your ShuleHub folder (app.py not found).")
if os.path.exists(f"{OLD}.py") and os.path.exists(f"{NEW}.py"):
    sys.exit(f"Both {OLD}.py and {NEW}.py exist. Please tell Claude, so nothing is overwritten.")

edited = []
for path in sorted(glob.glob("*.py")):
    if path in (f"{OLD}.py", f"{NEW}.py", os.path.basename(__file__)):
        continue
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    new = re.sub(r"^([ \t]*)from[ \t]+workflow[ \t]+import\b", r"\1from results_workflow import", text, flags=re.M)
    new = re.sub(r"^([ \t]*)import[ \t]+workflow[ \t]+as[ \t]+(\w+)", r"\1import results_workflow as \2", new, flags=re.M)
    new = re.sub(r"^([ \t]*)import[ \t]+workflow[ \t]*(?=(\r?\n|#|$))", r"\1import results_workflow as workflow", new, flags=re.M)
    if new != text:
        shutil.copyfile(path, path + ".wf.bak")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        edited.append(path)

if os.path.exists(f"{OLD}.py"):
    os.replace(f"{OLD}.py", f"{NEW}.py")
    print(f"Renamed {OLD}.py -> {NEW}.py")
else:
    print(f"{OLD}.py is already renamed.")
print("Imports updated in:", ", ".join(edited) if edited else "(nothing needed changing)")

left = []
for path in sorted(glob.glob("*.py")):
    if path == os.path.basename(__file__):
        continue
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if re.search(r"\bimport\s+workflow\b|\bfrom\s+workflow\b|[\"']workflow[\"']", line):
                left.append(f"  {path}:{n}: {line.strip()}")
if left:
    print("\nThese lines still mention workflow. Send them to Claude:")
    print("\n".join(left))
else:
    print("\nAll good. Now run build_windows.bat again.")
