r"""
check_library.py - finds out why the Library link is not showing in ShuleHub.

Run it from the ShuleHub folder (the one with app.py and templates):

    python check_library.py

It changes nothing. It prints a plain-English report and also saves it as
check_library_report.txt, so you can send that file to Claude.
"""
import importlib, os, re, sys, traceback

LINES = []
def say(text=""):
    print(text); LINES.append(text)

NEEDED_TEMPLATES = ["book", "book_form", "borrower", "catalogue", "circulation", "clearance",
                    "fines", "import", "index", "loans", "reports", "settings"]

def main():
    say("ShuleHub library check")
    say("Folder: " + os.getcwd())
    say("")
    problems = []

    if not os.path.exists("app.py"):
        say("!! app.py is not in this folder. Open the folder that has app.py and the templates folder, then run again.")
        return finish(["wrong folder"])
    sys.path.insert(0, os.getcwd())

    # 1. the files
    say("1. Files")
    if os.path.exists("library.py"):
        say("   OK  library.py found")
    else:
        say("   !!  library.py is NOT in this folder"); problems.append("library.py missing")
    tdir = os.path.join("templates", "library")
    if os.path.isdir(tdir):
        have = {f[:-5] for f in os.listdir(tdir) if f.endswith(".html")}
        missing = [t for t in NEEDED_TEMPLATES if t not in have]
        if missing:
            say("   !!  templates\\library is missing: " + ", ".join(t + ".html" for t in missing))
            problems.append("library templates missing")
        else:
            say("   OK  all 12 library pages (templates\\library) are there")
    else:
        say("   !!  the folder templates\\library does not exist, so no library page can be shown")
        problems.append("templates\\library missing")

    # 2. what library.py needs from the rest of the app
    say("")
    say("2. Can library.py be loaded?")
    for name, attrs in (("analytics", ["_csv_safe", "_find_column", "_norm", "_safe_filename"]),
                        ("import_files", ["MAX_BINARY_UPLOAD_BYTES", "rows_from_upload", "sniff_kind"]),
                        ("branding", ["logo_story"]), ("auth", ["COMMON", "has_service"])):
        try:
            mod = importlib.import_module(name)
            gone = [a for a in attrs if not hasattr(mod, a)]
            if gone:
                say(f"   !!  {name}.py has no: {', '.join(gone)}"); problems.append(f"{name} lacks {gone}")
            else:
                say(f"   OK  {name}.py")
        except Exception as e:
            say(f"   !!  {name}.py cannot be loaded: {type(e).__name__}: {e}"); problems.append(f"{name} import error")
    try:
        importlib.import_module("library")
        say("   OK  library.py loads without errors")
    except Exception:
        say("   !!  library.py fails to load. This is the error:")
        for ln in traceback.format_exc().strip().splitlines()[-6:]:
            say("       " + ln)
        problems.append("library import error")

    # 3. permissions / common pages
    say("")
    say("3. Permissions")
    try:
        import permissions
        svc = [k for k, _, _ in permissions.SERVICES]
        say("   OK  'library' service exists" if "library" in svc else "   !!  no 'library' service in permissions.py")
        if "library" not in svc: problems.append("no library service")
        say("   OK  library pages are tied to the library service"
            if permissions.services_for("library.index") == ("library",) else "   !!  library pages are not mapped in permissions.py")
    except Exception as e:
        say(f"   !!  permissions.py: {e}")
    try:
        import auth
        common = set(getattr(auth, "COMMON", []))
        say("   COMMON (open to every signed-in person) contains: " + (", ".join(sorted(common)) or "nothing"))
        for ep in ("library.catalogue", "library.book_detail"):
            if ep not in common:
                say(f"   ..  {ep} is not in auth.COMMON, so only people with the Library service can search the catalogue")
        for ep in ("search.index", "search.suggest"):
            if ep not in common:
                say(f"   ..  {ep} is not in auth.COMMON, so the search page is administrator-only for now")
    except Exception:
        pass

    # 4. how app.py loads it
    say("")
    say("4. app.py lines that mention the library or loaded_modules")
    src = open("app.py", encoding="utf-8", errors="replace").read().splitlines()
    hits = [(i + 1, l.rstrip()) for i, l in enumerate(src) if re.search(r"library|loaded_modules", l, re.I)]
    if hits:
        for n, l in hits[:25]:
            say(f"   line {n}: {l.strip()}")
    else:
        say("   !!  app.py never mentions the library, so it is not registered. That is why the link is hidden.")
        problems.append("library not registered in app.py")
    finish(problems)


def finish(problems):
    say("")
    if problems:
        say("RESULT: " + str(len(problems)) + " problem(s): " + "; ".join(problems))
    else:
        say("RESULT: everything looks fine here. Close ShuleHub completely and open it again.")
    say("Report saved as check_library_report.txt. Send it to Claude if you want help.")
    with open("check_library_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(LINES) + "\n")

main()
