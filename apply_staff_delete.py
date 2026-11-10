r"""
apply_staff_delete.py - lets the administrator DELETE a staff login (Staff & Logins page).

Run it from your ShuleHub folder (the one with auth.py and the templates folder):

    python apply_staff_delete.py

It does two things:
  1. adds a "delete this login" action to auth.py,
  2. adds a "Delete" button next to each person on templates\auth\staff.html.

Rules built in:
  * you cannot delete your own login,
  * you cannot delete the last active administrator,
  * deleting a login does NOT delete the teacher record or anything the person entered
    (marks, attendance, fees ... keep their username as plain text),
  * only administrators can use it (the page is not mapped to any service in permissions.py).
If you only want to stop someone logging in but keep their login, use the Active tick box instead.

Safe to run twice. Backups: auth.py.bak_staffdelete and staff.html.bak_staffdelete.
"""
import os, re, shutil, sys

ROUTE = '''@auth_bp.route("/staff/<int:user_id>/delete", methods=["POST"])
def staff_delete(user_id):
    row = db.fetch_one("SELECT id, username, full_name, role, active FROM users WHERE id=?", (user_id,))
    if not row:
        flash("That login no longer exists.", "error")
    elif user_id == g.user["id"]:
        flash("You can't delete your own login.", "error")
    elif row["role"] == "admin" and row["active"] and _admin_count(exclude_id=user_id) == 0:
        flash("There must always be at least one active administrator, so this login can't be deleted.", "error")
    else:
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        flash(f"The login for {row['full_name'] or row['username']} was deleted. "
              "Their teacher record and the work they entered are kept.", "success")
    return redirect(url_for("auth.staff"))


'''


def load(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def save(p, text):
    if not os.path.exists(p + ".bak_staffdelete"):
        shutil.copyfile(p, p + ".bak_staffdelete")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def patch_auth(todo):
    if not os.path.exists("auth.py"):
        sys.exit("Run this from the ShuleHub folder (auth.py not found). Current folder: " + os.getcwd())
    t = load("auth.py")
    if "def staff_delete" in t:
        return print("auth.py: delete action already there.")
    if "def staff_password" not in t:
        todo.append("auth.py: this does not look like ShuleHub's auth.py, nothing was changed")
        return
    nl = "\r\n" if "\r\n" in t else "\n"
    m = re.search(r"^# -{10,} roles & access", t, re.M)
    if not m:
        todo.append("auth.py: could not find where to add the delete action")
        return
    save("auth.py", t[:m.start()] + ROUTE.replace("\n", nl) + t[m.start():])
    print("auth.py: delete action added.")


def patch_template(todo):
    p = os.path.join("templates", "auth", "staff.html")
    if not os.path.exists(p):
        todo.append(p + " not found, so no Delete button was added")
        return
    t = load(p)
    if "auth.staff_delete" in t:
        return print("staff.html: Delete button already there.")
    nl = "\r\n" if "\r\n" in t else "\n"
    m = re.search(r"url_for\(\s*['\"]auth\.staff_update['\"]\s*,\s*user_id\s*=\s*(\w+)\.id\s*\)", t)
    if not m:
        todo.append("staff.html: could not find the row form. Add this next to each person's Save button:\n"
                    "      <form method=\"post\" action=\"{{ url_for('auth.staff_delete', user_id=THEIR_ID) }}\" "
                    "onsubmit=\"return confirm('Delete this login?');\"><button type=\"submit\" class=\"secondary\">Delete</button></form>")
        return
    var = m.group(1)
    end = t.find("</form>", m.end())
    if end < 0:
        todo.append("staff.html: could not find the end of the row form")
        return
    end += len("</form>")
    snippet = (nl + "      {% if " + var + ".id != user.id %}"
               "<form method=\"post\" action=\"{{ url_for('auth.staff_delete', user_id=" + var + ".id) }}\" style=\"display:inline;\" "
               "onsubmit=\"return confirm('Delete the login for ' + " + "{{ " + var + ".full_name|tojson }}" + " + '? This cannot be undone. Their teacher record and work are kept.');\">"
               "<button type=\"submit\" class=\"secondary\">Delete</button></form>{% endif %}")
    save(p, t[:end] + snippet + t[end:])
    print("staff.html: Delete button added.")


def main():
    todo = []
    patch_auth(todo)
    patch_template(todo)
    if todo:
        print("\nStill to do by hand:")
        for x in todo:
            print(" -", x)
    else:
        print("\nDone. Restart ShuleHub, then open Menu > Staff & Logins. Each person (except you) has a Delete button.")


main()
