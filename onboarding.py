"""
First-run setup wizard.

Runs once, for a brand-new install with no teachers yet: `school_settings.
onboarding_completed` starts False, and app.py's before_request sends every
page to /setup/welcome until the wizard is finished. Existing installations
that already have teachers are marked complete automatically the first time
this app starts with the new column (see database._migrate_schema), so
nobody already using the app gets interrupted.

Steps: welcome -> teachers -> subjects -> complete.
Each step's form posts straight into this blueprint (not the main app's
/teachers/add or /subjects/add), but inserts the same rows the same way, so
by the time someone reaches the real Teachers/Subjects pages afterwards,
their data is already there.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash

import database as db

onboarding_bp = Blueprint("onboarding", __name__, url_prefix="/setup")

STEPS = ["welcome", "teachers", "subjects", "complete"]


@onboarding_bp.route("/welcome")
def welcome():
    return render_template("onboarding/welcome.html", steps=STEPS, active_step="welcome")


@onboarding_bp.route("/teachers", methods=["GET", "POST"])
def teachers():
    if request.method == "POST":
        names = request.form.getlist("teacher_name")
        ranks = request.form.getlist("teacher_rank")
        added = 0
        for name, rank in zip(names, ranks):
            name = name.strip()
            if not name:
                continue
            db.execute(
                "INSERT INTO teachers (name, rank) VALUES (?, ?)",
                (name, int(rank) if rank.strip().isdigit() else None),
            )
            added += 1
        if added:
            flash(f"Added {added} teacher(s).", "success")
        return redirect(url_for("onboarding.subjects"))

    existing = db.fetch_all("SELECT * FROM teachers ORDER BY id")
    return render_template(
        "onboarding/teachers.html", steps=STEPS, active_step="teachers", teachers=existing
    )


@onboarding_bp.route("/subjects", methods=["GET", "POST"])
def subjects():
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")

    if request.method == "POST":
        names = request.form.getlist("subject_name")
        grade_names = request.form.getlist("subject_grade")
        added = 0
        for name, grade_name in zip(names, grade_names):
            name = name.strip()
            if not name or not grade_name:
                continue
            grow = db.fetch_one("SELECT id FROM grades WHERE name=?", (grade_name,))
            if not grow:
                continue
            try:
                db.execute(
                    "INSERT INTO subjects (name, category, grade_id, periods_per_week) "
                    "VALUES (?, ?, ?, ?)",
                    (name, "general", grow["id"], 5),
                )
                added += 1
            except Exception:
                pass
        if added:
            flash(f"Added {added} subject(s).", "success")
        return redirect(url_for("onboarding.complete"))

    existing = db.fetch_all(
        """SELECT sub.id, sub.name, g.name as grade_name FROM subjects sub
           JOIN grades g ON sub.grade_id = g.id ORDER BY g.sort_order, g.id, sub.name"""
    )
    return render_template(
        "onboarding/subjects.html", steps=STEPS, active_step="subjects",
        grades=grades, subjects=existing,
    )


@onboarding_bp.route("/complete", methods=["GET", "POST"])
def complete():
    if request.method == "POST":
        db.set_onboarding_completed(True)
        flash("Setup complete. Welcome to your timetable.", "success")
        return redirect(url_for("grades_page"))

    return render_template("onboarding/complete.html", steps=STEPS, active_step="complete")


@onboarding_bp.route("/skip", methods=["POST"])
def skip():
    """Escape hatch — marks setup done without adding anything, in case
    someone just wants to explore the app or already set things up by hand
    on the real Teachers/Subjects pages."""
    db.set_onboarding_completed(True)
    return redirect(url_for("grades_page"))
