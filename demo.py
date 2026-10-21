"""
demo.py — "View example": a safe sandbox with a pretend school.

How it works
------------
Pressing "View example" builds a separate SQLite file (demo-<id>.db, in the
system's temporary folder, never beside the real database) full of sample data
(see demo_data.py) and remembers its id in the
person's session. While that id is set, `activate()` points every database call
for their requests at the sample file instead of the real one, and a banner on
every page says so. "Exit example" clears the id and deletes the sample file.

Every example gets its OWN file with a random name. Nothing is ever deleted or
overwritten while it could be in use, so these cannot happen any more:
  * two launches at once (a double-click) trampling each other's half-built file;
  * a leftover or locked file from an earlier run (Windows refuses to delete a
    file that is open anywhere) stopping the next launch;
  * one person's reset pulling the file out from under someone else's session.
Old example files are swept away automatically, best-effort.

The real database is never opened while example mode is on, so nothing a person
tries out in the example can touch their school's records. Other people using the
app at the same time (the web version) are unaffected: the switch is per request.

SQLite only. The Render/Postgres web deployment has no example mode, and the
menu link and page hide themselves there.
"""

import os
import re
import secrets
import tempfile
import time

from flask import (
    Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for,
)

import database as db
import demo_data

demo_bp = Blueprint("demo", __name__, url_prefix="/demo")

# Where each "View example" button lands. Keys are the values posted by the form.
DESTINATIONS = {
    "home": "home",
    "timetable": "view_page",
    "builder": "builder_page",
    "exams": "analytics.exams_page",
    "students": "school.students",
    "attendance": "attendance.index",
    "fees": "fees.index",
}

_ID_RE = re.compile(r"^[0-9a-f]{16}$")      # what a valid example id looks like
_STALE_AFTER = 24 * 3600                    # sweep example files untouched for a day


def available():
    """Example mode needs the SQLite database (the offline desktop app)."""
    return not db.USE_POSTGRES


def _data_folder():
    """Where the real database lives (also where older versions put example files)."""
    return os.path.dirname(os.path.abspath(db.DB_PATH))


def _folder():
    """Where example files are written: the system's temporary folder.

    Example data is throw-away, so it does not belong beside the school's real
    records. The temporary folder is always writable (the packaged .exe itself
    unpacks there), is not synced by OneDrive or swept by backup/antivirus
    tools the way Desktop and Documents often are, and is not part of a backup.
    If it can't be used for some reason, fall back to the data folder."""
    try:
        folder = os.path.join(tempfile.gettempdir(), "ShuleHubExamples")
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".w{secrets.token_hex(4)}")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
        return folder
    except OSError:
        return _data_folder()


def _path_for(example_id):
    return os.path.join(_folder(), f"demo-{example_id}.db")


def _example_id():
    """The id of this person's example, or None when they're not in one."""
    value = session.get("demo")
    return value if isinstance(value, str) and _ID_RE.match(value) else None


def demo_path():
    """The current example's database file, or None."""
    example_id = _example_id()
    return _path_for(example_id) if example_id else None


def in_demo():
    return available() and _example_id() is not None


def _remove(path):
    """Delete an example file (and its journal) if we can. Never raises: a
    leftover file is harmless, and the sweep gets it later."""
    for target in (path, path + "-journal"):
        for attempt in range(3):
            try:
                if os.path.isfile(target):
                    os.remove(target)
                break
            except OSError:
                time.sleep(0.15)        # briefly in use (Windows): try again


def _sweep(keep=None):
    """Tidy up example files nobody needs any more.

    * In the example folder: anything untouched for a day.
    * In the data folder: leftovers from older versions, which kept their
      example files there ('demo.db', 'demo-<id>.db'). This version only writes
      there if the temporary folder is unusable, in which case the day rule applies."""
    example_folder = _folder()
    folders = [(example_folder, True)]
    data_folder = _data_folder()
    if os.path.abspath(data_folder) != os.path.abspath(example_folder):
        folders.append((data_folder, False))     # legacy location: remove regardless of age
    cutoff = time.time() - _STALE_AFTER
    for folder, only_if_old in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not (name == "demo.db" or (name.startswith("demo-") and name.endswith(".db"))):
                continue
            path = os.path.join(folder, name)
            if keep and os.path.abspath(path) == os.path.abspath(keep):
                continue
            try:
                if os.path.isfile(path) and (not only_if_old or os.path.getmtime(path) < cutoff):
                    _remove(path)
            except OSError:
                pass


def _reason(exc):
    """A short, readable cause to show on the page (the full traceback goes to the log)."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:160]}" if text else type(exc).__name__


# ---------------------------------------------------------------- request hooks
def activate():
    """before_request — registered ahead of the login gate in app.py, so even
    the "who is signed in?" lookup reads from the example database."""
    if not available():
        return None
    example_id = _example_id()
    if not example_id:
        if session.get("demo"):
            session.pop("demo", None)   # an old-style flag from a previous version: drop it
        return None
    path = _path_for(example_id)
    if not os.path.exists(path):
        session.pop("demo", None)       # the file went away (e.g. app restarted): leave example mode
        return None
    g._demo_token = db.use_db_path(path)
    return None


def deactivate(exc=None):
    """teardown_request — always put the database pointer back."""
    token = g.pop("_demo_token", None)
    if token is not None:
        try:
            db.reset_db_path(token)
        except ValueError:              # different context: fall back to the real database
            db.use_db_path(None)


@demo_bp.app_context_processor
def _inject():
    return {"demo_mode": in_demo(), "demo_available": available()}


# ---------------------------------------------------------------- routes
@demo_bp.route("/")
def index():
    if not available():
        flash("Example mode isn't available in the web version.", "error")
        return redirect(url_for("home"))
    return render_template("demo/index.html", destinations=DESTINATIONS)


@demo_bp.route("/start", methods=["POST"])
def start():
    if not available():
        return redirect(url_for("home"))
    target = DESTINATIONS.get(request.form.get("to", "home"), "home")
    rebuild = request.form.get("reset") == "1" or not in_demo()

    if rebuild:
        who = g.get("user")             # the signed-in person (the same id inside the example)
        if not who:
            flash("Sign in first, then open the example.", "error")
            return redirect(url_for("auth.login"))

        old_id = _example_id()          # a reset replaces the example they are in
        new_id = secrets.token_hex(8)
        new_path = _path_for(new_id)

        # Always read from, and build outside of, example redirection: pointing
        # at None means "the real database" even when this request is a reset
        # that arrived while example mode was on.
        token = db.use_db_path(None)
        try:
            row = db.fetch_one(
                "SELECT id, username, full_name, password_hash FROM users WHERE id=?", (who["id"],))
            if not row:
                flash("Sign in first, then open the example.", "error")
                return redirect(url_for("auth.login"))
            demo_data.build(new_path, dict(row))
        except Exception as exc:
            current_app.logger.exception("Could not build the example school")
            _remove(new_path)           # never leave a half-built file behind
            flash(f"Sorry, the example couldn't be prepared ({_reason(exc)}). "
                  "Your own records are untouched.", "error")
            return redirect(url_for("demo.index"))
        finally:
            db.reset_db_path(token)

        session["demo"] = new_id
        if old_id:
            _remove(_path_for(old_id))
        _sweep(keep=new_path)
        if request.form.get("reset") == "1":
            flash("The example has been reset to how it started.", "success")

    return redirect(url_for(target))


@demo_bp.route("/exit", methods=["POST"])
def exit_demo():
    example_id = _example_id()
    session.pop("demo", None)
    if example_id:
        _remove(_path_for(example_id))
    _sweep()
    flash("You're back in your own school's records.", "success")
    return redirect(url_for("home"))
