"""
app.py — ShuleHub: Timetable + Exam Analysis + School Management (Flask; runs on
Render as a web app and, via run_desktop.py, as the offline Windows desktop app)

Modules in one app:
  - Timetable      : this file (grades, streams, subjects, teachers, rooms, builder)
  - Exam Analysis  : analytics.py (students, exams, marks, merit lists, reports)

Levels supported (see curriculum.py):
  - Grades 1-3   : Lower Primary (CBC)
  - Grades 7-9   : Junior Secondary (CBC)
  - Grade 10     : Senior School (CBE, pathway-based)
  - Form 3 / 4   : legacy 8-4-4
  Other levels (Pre-Primary, Upper Primary, ...) can be added from the Grades page.
"""

import csv
import io
import os
import re
import secrets
from datetime import date, timedelta

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, Response, flash, g
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import curriculum
import database as db
import day_profiles
import scheduler
import auth
import branding
import demo
from analytics import analytics_bp
from attendance import attendance_bp
from auth import auth_bp
from fees import fees_bp, outstanding_total
from invigilation import invigilation_bp
from teacher_attendance import teacher_attendance_bp
from onboarding import onboarding_bp
from exam_results import mymarks_bp, results_bp
from school import school_bp

app = Flask(__name__)
# Sessions are signed with SECRET_KEY. Render generates one (render.yaml). If
# none is set (local / desktop use) a random key is made per run, which just
# means everyone signs in again after a restart.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=auth.SESSION_HOURS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # HTTPS on Render; plain http locally
)
app.register_blueprint(auth_bp)
app.register_blueprint(onboarding_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(mymarks_bp)
app.register_blueprint(results_bp)
app.register_blueprint(school_bp)
app.register_blueprint(attendance_bp)
app.register_blueprint(fees_bp)
app.register_blueprint(invigilation_bp)
app.register_blueprint(teacher_attendance_bp)
app.register_blueprint(demo.demo_bp)

# Example mode ("View example"): swaps in the sample school's database for the
# request. It must run FIRST, ahead of the login gate, so that even the lookup
# of who is signed in reads the example database while example mode is on.
app.before_request(demo.activate)
app.teardown_request(demo.deactivate)

# Login / role / CSRF gate. Registered BEFORE the onboarding redirect below so
# nobody reaches the setup wizard (or anything else) without signing in.
app.before_request(auth.gate)
app.after_request(auth.add_security_headers)
app.context_processor(auth.inject_user)

with app.app_context():
    db.init_db()


@app.context_processor
def inject_school():
    """Makes `school` (name/tagline/brand_mark/active_days) available in
    every template without every route having to fetch and pass it —
    this is what lets a school's own name appear in the header, browser
    tab title, and PWA-install title on every single page."""
    return {
        "school": db.get_school_settings(),
        "curriculum_labels": curriculum.CURRICULA,
    }


# Endpoints reachable even before setup is finished: the wizard itself,
# static files (css/js/icons), and the offline fallback page.
_ONBOARDING_EXEMPT_PREFIXES = ("onboarding.", "static", "auth.")
_ONBOARDING_EXEMPT_ENDPOINTS = {"offline_page", "guide_page", "logo_image", "get_app_page"}


@app.before_request
def _require_onboarding():
    endpoint = request.endpoint or ""
    if endpoint in _ONBOARDING_EXEMPT_ENDPOINTS:
        return
    if any(endpoint.startswith(p) for p in _ONBOARDING_EXEMPT_PREFIXES):
        return
    if not db.get_onboarding_completed():
        return redirect(url_for("onboarding.welcome"))


# --------------------------------------------------------------------------
# Home
# --------------------------------------------------------------------------
@app.route("/")
def home():
    def count(table, where=""):
        return db.fetch_one(f"SELECT COUNT(*) AS c FROM {table} {where}")["c"]
    counts = {
        "grades": count("grades"), "streams": count("streams"),
        "teachers": count("teachers"), "students": count("students", "WHERE active = 1"),
        "exams": count("exams"),
    }
    today = {}
    if auth.has_service(g.user, "attendance"):
        att = {r["status"]: r["c"] for r in db.fetch_all(
            "SELECT status, COUNT(*) AS c FROM attendance WHERE att_date=? GROUP BY status",
            (date.today().isoformat(),))}
        marked = sum(att.values())
        today["att_marked"] = marked
        today["att_pct"] = round(100 * (att.get("P", 0) + att.get("L", 0)) / marked, 1) if marked else None
        today["absent"] = att.get("A", 0) + att.get("E", 0)
    if auth.has_service(g.user, "fees"):
        today["owing"] = outstanding_total()
        today["month"] = db.fetch_one(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE voided=0 AND paid_on >= ?",
            (date.today().replace(day=1).isoformat(),))["s"]
    return render_template("home.html", counts=counts, today=today)


@app.route("/offline")
def offline_page():
    """Shown by the service worker when there's no connection and the
    requested page hasn't been cached yet from an earlier visit."""
    return render_template("offline.html")


@app.route("/get-app")
def get_app_page():
    """Public page: install ShuleHub on a phone (home-screen app) and, if the owner
    has set WINDOWS_DOWNLOAD_URL, download the offline Windows version."""
    return render_template("auth/get_app.html",
                           windows_url=os.environ.get("WINDOWS_DOWNLOAD_URL", "").strip())


@app.route("/guide")
def guide_page():
    return render_template("guide.html")


# --------------------------------------------------------------------------
# School Settings (name/tagline/brand + which weekdays are taught)
# --------------------------------------------------------------------------
@app.route("/settings")
def settings_page():
    school = db.get_school_settings()
    return render_template(
        "settings.html", school=school, all_weekdays=db.ALL_WEEKDAYS,
    )


@app.after_request
def _excel_exports_with_logo(resp):
    """Any CSV export can be fetched as Excel by adding ?fmt=xlsx — the Excel
    copy carries the school logo and name at the top (a CSV can't hold an image).
    Import templates stay plain CSV so they can be filled in and uploaded back."""
    if (request.args.get("fmt") == "xlsx" and resp.status_code == 200
            and resp.mimetype == "text/csv"
            and "template" not in resp.headers.get("Content-Disposition", "")):
        return branding.xlsx_from_csv_response(resp, db.get_school_settings()["name"])
    return resp


@app.route("/logo")
def logo_image():
    """The school logo (public, so the sign-in page can show it). The ?v=
    in the URL changes whenever a new logo is uploaded, so browsers can
    cache it for a long time and still pick up a replacement at once."""
    found = branding.get_logo()
    if not found:
        return "", 404
    data, version = found
    resp = Response(data, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    resp.headers["ETag"] = f'"{version}"'
    return resp


@app.route("/settings/logo", methods=["POST"])
def save_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("Choose an image file first.", "error")
        return redirect(url_for("settings_page"))
    png, err = branding.prepare_logo(f.read(branding.MAX_UPLOAD_BYTES + 1))
    if err:
        flash(err, "error")
    else:
        branding.save_logo(png)
        flash("Logo saved. It now shows in the header, on the sign-in page and on printed PDFs.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/logo/remove", methods=["POST"])
def remove_logo():
    branding.remove_logo()
    flash("Logo removed.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/save", methods=["POST"])
def save_settings():
    name = request.form.get("name", "").strip()
    tagline = request.form.get("tagline", "").strip()
    brand_mark = request.form.get("brand_mark", "").strip()
    active_days = request.form.getlist("active_days")

    if not name:
        flash("Enter a school name.", "error")
        return redirect(url_for("settings_page"))
    if not active_days:
        flash("Choose at least one teaching day.", "error")
        return redirect(url_for("settings_page"))

    old_days = set(db.get_active_days())
    db.set_school_settings(name, tagline, brand_mark, active_days)

    if set(active_days) != old_days:
        db.regenerate_periods()
        flash(
            "School details saved — the timetable's day columns were "
            "regenerated to match your new teaching days.",
            "success",
        )
    else:
        flash("School details saved.", "success")

    return redirect(url_for("settings_page"))


# --------------------------------------------------------------------------
# Grades & Streams
# --------------------------------------------------------------------------
@app.route("/grades")
def grades_page():
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")
    selected_id = request.args.get("grade_id", type=int) or (grades[0]["id"] if grades else None)
    streams = []
    if selected_id:
        streams = db.fetch_all(
            """SELECT s.id, s.name, p.name as pathway_name FROM streams s
               LEFT JOIN pathways p ON s.pathway_id = p.id
               WHERE s.grade_id = ? ORDER BY s.name""",
            (selected_id,),
        )
    pathways = db.fetch_all("SELECT * FROM pathways ORDER BY name")
    selected_grade = db.fetch_one("SELECT * FROM grades WHERE id=?", (selected_id,)) if selected_id else None
    return render_template(
        "grades.html", grades=grades, streams=streams, pathways=pathways,
        selected_id=selected_id, selected_grade=selected_grade,
    )


@app.route("/grades/add", methods=["POST"])
def add_grade():
    name = request.form.get("name", "").strip()
    curr = request.form.get("curriculum", "")
    with_subjects = bool(request.form.get("with_subjects"))
    if not name or curr not in curriculum.CURRICULA:
        flash("Enter a grade name and choose its level.", "error")
        return redirect(url_for("grades_page"))
    try:
        new_id = db.create_grade(name, curr, with_subjects)
    except Exception:
        flash(f"A grade called “{name}” already exists.", "error")
        return redirect(url_for("grades_page"))
    msg = f"Added {name}."
    if with_subjects and curriculum.LEVEL_SUBJECTS.get(curr):
        msg += " Its standard subjects were added too — adjust periods per week on the Subjects page."
    flash(msg, "success")
    return redirect(url_for("grades_page", grade_id=new_id))


@app.route("/grades/<int:grade_id>/delete", methods=["POST"])
def delete_grade(grade_id):
    grade = db.fetch_one("SELECT name FROM grades WHERE id=?", (grade_id,))
    if grade:
        db.execute("DELETE FROM grades WHERE id=?", (grade_id,))
        flash(f"Deleted {grade['name']} with its streams, subjects, students and exams.", "success")
    return redirect(url_for("grades_page"))


@app.route("/streams/add", methods=["POST"])
def add_stream():
    grade_id = request.form.get("grade_id", type=int)
    name = request.form.get("name", "").strip()
    pathway_name = request.form.get("pathway", "")
    if not grade_id or not name:
        flash("Enter a stream name.", "error")
        return redirect(url_for("grades_page", grade_id=grade_id))
    grade = db.fetch_one("SELECT curriculum FROM grades WHERE id=?", (grade_id,))
    pathway_id = None
    if grade and grade["curriculum"] == "CBC" and pathway_name and pathway_name != "(none)":
        prow = db.fetch_one("SELECT id FROM pathways WHERE name=?", (pathway_name,))
        pathway_id = prow["id"] if prow else None
    try:
        db.execute(
            "INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, ?, ?)",
            (grade_id, pathway_id, name),
        )
    except Exception as e:
        flash(f"Could not add stream: {e}", "error")
    return redirect(url_for("grades_page", grade_id=grade_id))


@app.route("/streams/<int:stream_id>/delete", methods=["POST"])
def delete_stream(stream_id):
    grade_id = request.form.get("grade_id", type=int)
    db.execute("DELETE FROM streams WHERE id=?", (stream_id,))
    return redirect(url_for("grades_page", grade_id=grade_id))


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------
@app.route("/subjects")
def subjects_page():
    grade_filter = request.args.get("grade", "(all)")
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")
    pathways = db.fetch_all("SELECT * FROM pathways ORDER BY name")

    query = """SELECT sub.id, sub.name, sub.category, sub.periods_per_week,
                      sub.group_id, sg.name as group_name,
                      g.name as grade_name, p.name as pathway_name
               FROM subjects sub
               JOIN grades g ON sub.grade_id = g.id
               LEFT JOIN pathways p ON sub.pathway_id = p.id
               LEFT JOIN subject_groups sg ON sub.group_id = sg.id"""
    params = ()
    if grade_filter and grade_filter != "(all)":
        query += " WHERE g.name = ?"
        params = (grade_filter,)
    query += " ORDER BY g.sort_order, g.id, sub.category, sub.name"
    subjects = db.fetch_all(query, params)

    groups = db.list_subject_groups()

    return render_template(
        "subjects.html", grades=grades, pathways=pathways, subjects=subjects,
        grade_filter=grade_filter, groups=groups,
    )


@app.route("/subjects/add", methods=["POST"])
def add_subject():
    name = request.form.get("name", "").strip()
    grade_name = request.form.get("grade", "")
    category = request.form.get("category", "general")
    pathway_name = request.form.get("pathway", "")
    periods_per_week = request.form.get("periods_per_week", type=int) or 5
    if not name or not grade_name:
        flash("Enter a subject name and choose a grade.", "error")
        return redirect(url_for("subjects_page"))
    grow = db.fetch_one("SELECT id, curriculum FROM grades WHERE name=?", (grade_name,))
    if not grow:
        return redirect(url_for("subjects_page"))
    pathway_id = None
    if grow["curriculum"] == "CBC" and pathway_name and pathway_name != "(none)":
        prow = db.fetch_one("SELECT id FROM pathways WHERE name=?", (pathway_name,))
        pathway_id = prow["id"] if prow else None
    try:
        db.execute(
            "INSERT INTO subjects (name, category, grade_id, pathway_id, periods_per_week) VALUES (?, ?, ?, ?, ?)",
            (name, category, grow["id"], pathway_id, periods_per_week),
        )
    except Exception as e:
        flash(f"Could not add subject: {e}", "error")
    return redirect(url_for("subjects_page", grade=grade_name))


@app.route("/subjects/<int:subject_id>/periods", methods=["POST"])
def update_subject_periods(subject_id):
    grade_filter = request.form.get("grade_filter", "(all)")
    periods_per_week = request.form.get("periods_per_week", type=int)
    if periods_per_week is None or periods_per_week < 1:
        flash("Periods per week must be a positive number.", "error")
        return redirect(url_for("subjects_page", grade=grade_filter))
    db.execute(
        "UPDATE subjects SET periods_per_week=? WHERE id=?",
        (periods_per_week, subject_id),
    )
    return redirect(url_for("subjects_page", grade=grade_filter))


@app.route("/subjects/<int:subject_id>/delete", methods=["POST"])
def delete_subject(subject_id):
    grade_filter = request.form.get("grade_filter", "(all)")
    db.execute("DELETE FROM subjects WHERE id=?", (subject_id,))
    return redirect(url_for("subjects_page", grade=grade_filter))


@app.route("/subjects/<int:subject_id>/group", methods=["POST"])
def set_subject_group(subject_id):
    """Assign (or clear) which optional-subject group a subject belongs to,
    e.g. putting Business Studies, Computer Studies, Home Science, and
    Agriculture all in one "Electives" group so they're scheduled and
    displayed together as a single "pick one" slot."""
    grade_filter = request.form.get("grade_filter", "(all)")
    group_id = request.form.get("group_id", type=int)
    db.set_subject_group(subject_id, group_id)
    return redirect(url_for("subjects_page", grade=grade_filter))


@app.route("/subject_groups/add", methods=["POST"])
def add_subject_group():
    name = request.form.get("name", "").strip()
    grade_name = request.form.get("grade", "")
    pathway_name = request.form.get("pathway", "")
    periods_per_week = request.form.get("periods_per_week", type=int) or 5
    grade_filter = request.form.get("grade_filter", "(all)")

    if not name or not grade_name:
        flash("Enter a group name and choose a grade.", "error")
        return redirect(url_for("subjects_page", grade=grade_filter))

    grow = db.fetch_one("SELECT id, curriculum FROM grades WHERE name=?", (grade_name,))
    if not grow:
        return redirect(url_for("subjects_page", grade=grade_filter))
    pathway_id = None
    if grow["curriculum"] == "CBC" and pathway_name and pathway_name != "(none)":
        prow = db.fetch_one("SELECT id FROM pathways WHERE name=?", (pathway_name,))
        pathway_id = prow["id"] if prow else None

    try:
        db.create_subject_group(name, grow["id"], pathway_id, periods_per_week)
        flash(f'Created optional-subject group "{name}". Now assign subjects to it below.', "success")
    except Exception as e:
        flash(f"Could not create group: {e}", "error")
    return redirect(url_for("subjects_page", grade=grade_filter))


@app.route("/subject_groups/<int:group_id>/delete", methods=["POST"])
def delete_subject_group(group_id):
    grade_filter = request.form.get("grade_filter", "(all)")
    db.delete_subject_group(group_id)
    flash("Group deleted — its subjects are back to being scheduled individually.", "success")
    return redirect(url_for("subjects_page", grade=grade_filter))


# --------------------------------------------------------------------------
# Teachers
# --------------------------------------------------------------------------
@app.route("/teachers")
def teachers_page():
    teachers = db.fetch_all(
        "SELECT * FROM teachers ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, name"
    )
    selected_id = request.args.get("teacher_id", type=int)
    all_subjects = db.fetch_all(
        """SELECT sub.id, sub.name, g.name as grade_name FROM subjects sub
           JOIN grades g ON sub.grade_id = g.id ORDER BY g.sort_order, g.id, sub.name"""
    )
    assigned_ids = set()
    if selected_id:
        assigned_ids = {r["subject_id"] for r in db.fetch_all(
            "SELECT subject_id FROM teacher_subjects WHERE teacher_id=?", (selected_id,))}
    selected_teacher = next((t for t in teachers if t["id"] == selected_id), None)
    return render_template(
        "teachers.html", teachers=teachers, selected_id=selected_id,
        all_subjects=all_subjects, assigned_ids=assigned_ids, selected_teacher=selected_teacher,
    )


@app.route("/teachers/add", methods=["POST"])
def add_teacher():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    rank = request.form.get("rank", type=int)
    if not name:
        flash("Enter a teacher name.", "error")
        return redirect(url_for("teachers_page"))
    db.execute("INSERT INTO teachers (name, email, rank) VALUES (?, ?, ?)", (name, email, rank))
    return redirect(url_for("teachers_page"))


@app.route("/teachers/<int:teacher_id>/update", methods=["POST"])
def update_teacher(teacher_id):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    rank = request.form.get("rank", type=int)
    if not name:
        flash("Enter a teacher name.", "error")
        return redirect(url_for("teachers_page", teacher_id=teacher_id))
    db.execute(
        "UPDATE teachers SET name=?, email=?, rank=? WHERE id=?",
        (name, email, rank, teacher_id),
    )
    flash("Teacher details updated.", "success")
    return redirect(url_for("teachers_page", teacher_id=teacher_id))


@app.route("/teachers/<int:teacher_id>/delete", methods=["POST"])
def delete_teacher(teacher_id):
    db.execute("DELETE FROM teachers WHERE id=?", (teacher_id,))
    return redirect(url_for("teachers_page"))


@app.route("/teachers/<int:teacher_id>/subjects", methods=["POST"])
def save_teacher_subjects(teacher_id):
    chosen_ids = request.form.getlist("subject_ids", type=int)
    db.execute("DELETE FROM teacher_subjects WHERE teacher_id=?", (teacher_id,))
    for sid in chosen_ids:
        db.execute("INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES (?, ?)", (teacher_id, sid))
    flash("Subject assignments updated.", "success")
    return redirect(url_for("teachers_page", teacher_id=teacher_id))


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------
@app.route("/rooms")
def rooms_page():
    rooms = db.fetch_all("SELECT * FROM rooms ORDER BY name")
    return render_template("rooms.html", rooms=rooms)


@app.route("/rooms/add", methods=["POST"])
def add_room():
    name = request.form.get("name", "").strip()
    capacity = request.form.get("capacity", "").strip() or None
    if not name:
        flash("Enter a room name.", "error")
        return redirect(url_for("rooms_page"))
    try:
        db.execute("INSERT INTO rooms (name, capacity) VALUES (?, ?)", (name, capacity))
    except Exception as e:
        flash(f"Could not add room: {e}", "error")
    return redirect(url_for("rooms_page"))


@app.route("/rooms/<int:room_id>/delete", methods=["POST"])
def delete_room(room_id):
    db.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    return redirect(url_for("rooms_page"))


# --------------------------------------------------------------------------
# Scheduling Rules (auto-generation settings)
# --------------------------------------------------------------------------
@app.route("/rules")
def rules_page():
    settings = db.get_generation_settings()
    subjects = db.fetch_all(
        "SELECT sub.id, sub.name, g.name AS grade_name FROM subjects sub "
        "JOIN grades g ON sub.grade_id = g.id ORDER BY g.name, sub.name"
    )
    subject_rules = db.list_subject_rules()
    # periods 1..8 for the "not after period" dropdown (matches the seeded day length)
    max_period = db.fetch_one("SELECT MAX(period_number) AS m FROM periods WHERE is_break=0")
    period_options = list(range(1, (max_period["m"] or 8) + 1))
    return render_template(
        "rules.html", settings=settings, subjects=subjects,
        subject_rules=subject_rules, period_options=period_options,
    )


@app.route("/rules/save", methods=["POST"])
def save_rules():
    # Checkbox: present in form data only when checked.
    avoid_same_day = "1" if request.form.get("avoid_same_day_repeat") else "0"
    vary_period = "1" if request.form.get("vary_period_slot") else "0"
    db.set_generation_setting("avoid_same_day_repeat", avoid_same_day)
    db.set_generation_setting("vary_period_slot", vary_period)
    flash("Scheduling rules updated.", "success")
    return redirect(url_for("rules_page"))


@app.route("/rules/subject_rule/add", methods=["POST"])
def add_subject_rule():
    subject_id = request.form.get("subject_id", type=int)
    rule_type = request.form.get("rule_type", "")
    value = request.form.get("value", type=int)

    if not subject_id or rule_type not in db.VALID_RULE_TYPES or not value or value < 1:
        flash("Choose a subject, rule type, and a valid positive number.", "error")
        return redirect(url_for("rules_page"))

    db.add_subject_rule(subject_id, rule_type, value)
    flash("Rule added.", "success")
    return redirect(url_for("rules_page"))


@app.route("/rules/subject_rule/<int:rule_id>/delete", methods=["POST"])
def delete_subject_rule(rule_id):
    db.delete_subject_rule(rule_id)
    flash("Rule removed.", "success")
    return redirect(url_for("rules_page"))


# --------------------------------------------------------------------------
# Day Structure (start time, lesson length, breaks)
# --------------------------------------------------------------------------
@app.route("/day-structure")
def day_structure_page():
    profiles = day_profiles.list_profiles()
    pid = request.args.get("profile", type=int)
    current = next((p for p in profiles if p["id"] == pid), profiles[0])
    grades = db.fetch_all(
        "SELECT id, name, curriculum, day_profile_id FROM grades ORDER BY sort_order, id")
    return render_template(
        "day_structure.html", profiles=profiles, current=current, grades=grades,
        default_id=day_profiles.default_id(), clashes=day_profiles.double_bookings(),
    )


@app.route("/day-structure/save", methods=["POST"])
def save_day_structure():
    pid = request.form.get("profile_id", type=int)
    name = (request.form.get("name") or "").strip()
    start_time = request.form.get("start_time", "08:00")
    lesson_minutes = request.form.get("lesson_minutes", type=int)
    periods_per_day = request.form.get("periods_per_day", type=int)

    if not pid or not name:
        flash("Give the profile a name.", "error")
        return redirect(url_for("day_structure_page"))
    if not lesson_minutes or lesson_minutes < 10 or not periods_per_day or periods_per_day < 1:
        flash("Enter a valid lesson length and number of periods per day.", "error")
        return redirect(url_for("day_structure_page", profile=pid))

    break_list = []
    for ap_raw, dur_raw, label in zip(request.form.getlist("after_period[]"),
                                      request.form.getlist("duration[]"),
                                      request.form.getlist("label[]")):
        if ap_raw and dur_raw:
            break_list.append((int(ap_raw), int(dur_raw), label.strip() or "Break"))

    old = day_profiles.get_profile(pid)
    try:
        dropped = day_profiles.save_profile(pid, name, start_time, lesson_minutes, periods_per_day, break_list)
    except Exception:
        flash("Could not save — another profile already uses that name.", "error")
        return redirect(url_for("day_structure_page", profile=pid))

    flash(f"{name}: periods and break times have been regenerated.", "success")
    if old["periods_per_day"] > periods_per_day:
        flash(f"You reduced periods/day from {old['periods_per_day']} to {periods_per_day}: any lessons in the "
              f"removed periods were cleared. Re-run Auto-Generate or fill those slots manually.", "error")
    if dropped:
        flash("One or more breaks were dropped because their period no longer fits in the day.", "error")
    return redirect(url_for("day_structure_page", profile=pid))


@app.route("/day-structure/new", methods=["POST"])
def new_day_profile():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the new profile a name.", "error")
        return redirect(url_for("day_structure_page"))
    try:
        pid = day_profiles.create_profile(name, copy_from=request.form.get("copy_from", type=int))
    except Exception:
        flash("A profile with that name already exists.", "error")
        return redirect(url_for("day_structure_page"))
    day_profiles.regenerate(pid)
    flash(f"Profile \u201c{name}\u201d created (copied from an existing one). Adjust its times, then assign grades to it.", "success")
    return redirect(url_for("day_structure_page", profile=pid))


@app.route("/day-structure/<int:pid>/delete", methods=["POST"])
def delete_day_profile(pid):
    if day_profiles.delete_profile(pid):
        flash("Profile deleted. Its grades now use the Default profile.", "success")
    else:
        flash("The Default profile cannot be deleted.", "error")
    return redirect(url_for("day_structure_page"))


@app.route("/day-structure/assign", methods=["POST"])
def assign_day_profile():
    pid = request.form.get("profile_id", type=int)
    grade_ids = request.form.getlist("grade_ids", type=int)
    code = request.form.get("curriculum")
    if not pid:
        return redirect(url_for("day_structure_page"))
    moved, removed = day_profiles.assign_curriculum(pid, code) if code else day_profiles.assign_grades(pid, grade_ids)
    flash("Grades updated." + (f" {moved} lesson(s) moved onto the new grid." if moved else ""), "success")
    if removed:
        flash(f"{removed} lesson(s) sat in a period that does not exist on the new grid and were removed.", "error")
    return redirect(url_for("day_structure_page", profile=pid))


# --------------------------------------------------------------------------
# Timetable Builder
# --------------------------------------------------------------------------
@app.route("/builder")
def builder_page():
    streams = db.fetch_all(
        """SELECT s.id, s.name, g.name as grade_name FROM streams s
           JOIN grades g ON s.grade_id = g.id ORDER BY g.sort_order, g.id, s.name"""
    )
    stream_id = request.args.get("stream_id", type=int) or (streams[0]["id"] if streams else None)

    # Each class uses the day profile of its grade, so the grid (period times,
    # breaks, number of periods) is that profile's, not one for the whole school.
    profile_id = day_profiles.profile_id_for_stream(stream_id) if stream_id else day_profiles.default_id()
    days = db.get_active_days()
    display_rows = day_profiles.rows_with_breaks(profile_id)

    # Real period ids differ per day even for the same period_number, and the
    # drag-and-drop grid needs the exact id to move/swap lessons — build a
    # (day, period_number) -> period_id lookup for the template to use.
    period_ids = {
        (p["day"], p["period_number"]): p["id"]
        for p in db.fetch_all("SELECT id, day, period_number FROM periods WHERE is_break=0 AND profile_id=?",
                              (profile_id,))
    }

    entries = {}
    if stream_id:
        entry_rows = db.fetch_all(
            """SELECT te.id, sub.name as subject_name, sub.group_id, sg.name as group_name,
                      t.name as teacher_name, rm.name as room_name, p.day, p.period_number
               FROM timetable_entries te
               JOIN periods p ON te.period_id = p.id
               JOIN subjects sub ON te.subject_id = sub.id
               LEFT JOIN subject_groups sg ON sub.group_id = sg.id
               LEFT JOIN teachers t ON te.teacher_id = t.id
               LEFT JOIN rooms rm ON te.room_id = rm.id
               WHERE te.stream_id = ?""",
            (stream_id,),
        )
        for r in entry_rows:
            entries.setdefault((r["day"], r["period_number"]), []).append(r)

    return render_template(
        "builder.html", streams=streams, stream_id=stream_id,
        rows=display_rows, days=days, entries=entries, period_ids=period_ids,
    )


@app.route("/builder/generate", methods=["POST"])
def generate_timetable():
    scope = request.form.get("scope", "stream")  # "stream" or "all"
    overwrite = request.form.get("overwrite") == "1"
    stream_id = request.form.get("stream_id", type=int)

    stream_ids = None if scope == "all" else ([stream_id] if stream_id else [])

    report = scheduler.generate_timetable(stream_ids=stream_ids, overwrite=overwrite)

    total_scheduled = sum(r["scheduled"] for r in report)
    total_skipped = sum(len(r["skipped"]) for r in report)
    flash(f"Auto-generated {total_scheduled} periods across {len(report)} stream(s).", "success")
    if total_skipped:
        details = " | ".join(
            f"{r['stream']}: " + "; ".join(r["skipped"])
            for r in report if r["skipped"]
        )
        flash(f"{total_skipped} slot(s) left unfilled — {details}", "error")

    return redirect(url_for("builder_page", stream_id=stream_id))


@app.route("/api/cell")
def api_get_cell():
    """Returns subject/teacher/room options + any existing entry/entries for
    a cell. A cell can hold more than one entry when it's an optional
    subject group (e.g. Business Studies / Computer Studies / Home Science /
    Agriculture taught in parallel) — `existing` is a list."""
    stream_id = request.args.get("stream_id", type=int)
    day = request.args.get("day")
    period_number = request.args.get("period_number", type=int)

    stream = db.fetch_one("SELECT * FROM streams WHERE id=?", (stream_id,))
    period = db.fetch_one(
        "SELECT * FROM periods WHERE day=? AND period_number=? AND profile_id=?",
        (day, period_number, day_profiles.profile_id_for_stream(stream_id)))
    if not stream or not period:
        return jsonify({"error": "not found"}), 404

    subjects = db.fetch_all(
        "SELECT id, name, group_id FROM subjects "
        "WHERE grade_id=? AND (pathway_id IS NULL OR pathway_id=?) ORDER BY name",
        (stream["grade_id"], stream["pathway_id"]),
    )
    teachers = db.fetch_all("SELECT id, name FROM teachers ORDER BY name")
    rooms = db.fetch_all("SELECT id, name FROM rooms ORDER BY name")
    existing = db.fetch_all(
        "SELECT * FROM timetable_entries WHERE stream_id=? AND period_id=?",
        (stream_id, period["id"]),
    )

    return jsonify({
        "subjects": [dict(s) for s in subjects],
        "teachers": [dict(t) for t in teachers],
        "rooms": [dict(r) for r in rooms],
        "existing": [dict(e) for e in existing],
        "period_id": period["id"],
        "period_label": f"{day} — Period {period_number} ({period['start_time']}-{period['end_time']})",
    })


@app.route("/api/cell/save", methods=["POST"])
def api_save_cell():
    """Save a single, ungrouped subject into a slot. Any existing entry/entries
    already occupying that stream+period (whether a lone subject or a whole
    optional-subject group) are replaced, since a slot can only be one thing
    at a time from the builder's point of view."""
    data = request.get_json()
    stream_id = data.get("stream_id")
    period_id = data.get("period_id")
    subject_id = data.get("subject_id")
    teacher_id = data.get("teacher_id") or None
    room_id = data.get("room_id") or None
    force = data.get("force", False)

    if not subject_id:
        return jsonify({"error": "Choose a subject."}), 400

    conflicts = []
    if teacher_id:
        clash = db.fetch_one(
            "SELECT te.id FROM timetable_entries te WHERE te.teacher_id=? AND " + day_profiles.OVERLAP + " AND te.stream_id != ?",
            (teacher_id, period_id, stream_id),
        )
        if clash:
            conflicts.append("This teacher is already teaching another stream in this period.")
    if room_id:
        clash = db.fetch_one(
            "SELECT te.id FROM timetable_entries te WHERE te.room_id=? AND " + day_profiles.OVERLAP + " AND te.stream_id != ?",
            (room_id, period_id, stream_id),
        )
        if clash:
            conflicts.append("This room is already booked by another stream in this period.")

    if conflicts and not force:
        return jsonify({"conflicts": conflicts})

    db.execute("DELETE FROM timetable_entries WHERE stream_id=? AND period_id=?", (stream_id, period_id))
    db.execute(
        "INSERT INTO timetable_entries (stream_id, subject_id, teacher_id, room_id, period_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (stream_id, subject_id, teacher_id, room_id, period_id),
    )
    return jsonify({"ok": True})


@app.route("/api/cell/save_group", methods=["POST"])
def api_save_group_cell():
    """Save a whole optional-subject group into a slot: one timetable_entries
    row per member subject, all at the same stream+period, each with its own
    teacher/room — e.g. Business Studies with Mr Otieno in Room 2, Computer
    Studies with Mrs Wanjiru in the Computer Lab, at the same time, so
    students can be split across them."""
    data = request.get_json()
    stream_id = data.get("stream_id")
    period_id = data.get("period_id")
    rows = data.get("entries") or []  # [{subject_id, teacher_id, room_id}, ...]
    force = data.get("force", False)

    if not rows or not any(r.get("subject_id") for r in rows):
        return jsonify({"error": "Choose at least one subject for this group."}), 400

    conflicts = []
    for row in rows:
        teacher_id = row.get("teacher_id") or None
        room_id = row.get("room_id") or None
        if teacher_id:
            clash = db.fetch_one(
                "SELECT te.id FROM timetable_entries te WHERE te.teacher_id=? AND " + day_profiles.OVERLAP + " AND te.stream_id != ?",
                (teacher_id, period_id, stream_id),
            )
            if clash:
                conflicts.append(f"A teacher for one of these subjects is already teaching another stream in this period.")
        if room_id:
            clash = db.fetch_one(
                "SELECT te.id FROM timetable_entries te WHERE te.room_id=? AND " + day_profiles.OVERLAP + " AND te.stream_id != ?",
                (room_id, period_id, stream_id),
            )
            if clash:
                conflicts.append("A room for one of these subjects is already booked by another stream in this period.")

    # Two member subjects can't share the same teacher at the same time.
    teacher_ids_used = [r.get("teacher_id") for r in rows if r.get("teacher_id")]
    if len(teacher_ids_used) != len(set(teacher_ids_used)):
        conflicts.append("Two subjects in this group can't share the same teacher at the same time.")

    conflicts = list(dict.fromkeys(conflicts))  # de-duplicate, keep order
    if conflicts and not force:
        return jsonify({"conflicts": conflicts})

    db.execute("DELETE FROM timetable_entries WHERE stream_id=? AND period_id=?", (stream_id, period_id))
    for row in rows:
        subject_id = row.get("subject_id")
        if not subject_id:
            continue
        db.execute(
            "INSERT INTO timetable_entries (stream_id, subject_id, teacher_id, room_id, period_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (stream_id, subject_id, row.get("teacher_id") or None, row.get("room_id") or None, period_id),
        )
    return jsonify({"ok": True})


@app.route("/api/cell/clear", methods=["POST"])
def api_clear_cell():
    data = request.get_json()
    stream_id = data.get("stream_id")
    period_id = data.get("period_id")
    db.execute(
        "DELETE FROM timetable_entries WHERE stream_id=? AND period_id=?",
        (stream_id, period_id),
    )
    return jsonify({"ok": True})


@app.route("/api/cell/move", methods=["POST"])
def api_move_cell():
    """Move a lesson from one period to another for the same stream (drag
    and drop on the Builder grid). If the destination slot already has a
    lesson, the two swap places instead of one overwriting the other.

    Optional-subject group slots (which hold more than one entry) can't be
    dragged — there's no single "lesson" to move, and swapping a whole group
    into a slot occupied by something else is ambiguous. Those have to be
    edited/cleared from the cell editor instead.
    """
    data = request.get_json()
    stream_id = data.get("stream_id")
    from_period_id = data.get("from_period_id")
    to_period_id = data.get("to_period_id")
    force = data.get("force", False)

    if not stream_id or not from_period_id or not to_period_id:
        return jsonify({"error": "Missing data for that move."}), 400
    if from_period_id == to_period_id:
        return jsonify({"ok": True})  # dropped back on itself, nothing to do

    from_rows = db.fetch_all(
        "SELECT * FROM timetable_entries WHERE stream_id=? AND period_id=?",
        (stream_id, from_period_id),
    )
    if not from_rows:
        return jsonify({"error": "That slot is empty — nothing to move."}), 400
    if len(from_rows) > 1:
        return jsonify({"error": "That slot has an optional-subject group in it — edit or clear it from the cell editor instead of dragging."}), 400
    entry_from = from_rows[0]

    to_rows = db.fetch_all(
        "SELECT * FROM timetable_entries WHERE stream_id=? AND period_id=?",
        (stream_id, to_period_id),
    )
    if len(to_rows) > 1:
        return jsonify({"error": "The destination slot has an optional-subject group in it — edit or clear it from the cell editor instead of dragging."}), 400
    entry_to = to_rows[0] if to_rows else None

    conflicts = []

    def _check_conflicts(teacher_id, room_id, at_period_id):
        if teacher_id:
            clash = db.fetch_one(
                "SELECT te.id FROM timetable_entries te WHERE te.teacher_id=? AND " + day_profiles.OVERLAP + " AND te.stream_id != ?",
                (teacher_id, at_period_id, stream_id),
            )
            if clash:
                conflicts.append("The teacher for one of these lessons is already booked elsewhere in that period.")
        if room_id:
            clash = db.fetch_one(
                "SELECT te.id FROM timetable_entries te WHERE te.room_id=? AND " + day_profiles.OVERLAP + " AND te.stream_id != ?",
                (room_id, at_period_id, stream_id),
            )
            if clash:
                conflicts.append("The room for one of these lessons is already booked elsewhere in that period.")

    # Moving entry_from into to_period_id...
    _check_conflicts(entry_from["teacher_id"], entry_from["room_id"], to_period_id)
    # ...and, if this is a swap, entry_to moving into from_period_id.
    if entry_to:
        _check_conflicts(entry_to["teacher_id"], entry_to["room_id"], from_period_id)

    if conflicts and not force:
        return jsonify({"conflicts": conflicts})

    if entry_to:
        # Swap the lesson content between the two existing rows rather than
        # touching period_id — sidesteps the UNIQUE(stream_id, period_id)
        # constraint entirely (both destination rows already exist).
        db.execute(
            "UPDATE timetable_entries SET subject_id=?, teacher_id=?, room_id=? WHERE id=?",
            (entry_to["subject_id"], entry_to["teacher_id"], entry_to["room_id"], entry_from["id"]),
        )
        db.execute(
            "UPDATE timetable_entries SET subject_id=?, teacher_id=?, room_id=? WHERE id=?",
            (entry_from["subject_id"], entry_from["teacher_id"], entry_from["room_id"], entry_to["id"]),
        )
    else:
        # Destination is empty — just relocate this lesson there.
        db.execute(
            "UPDATE timetable_entries SET period_id=? WHERE id=?",
            (to_period_id, entry_from["id"]),
        )

    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# View / Export
# --------------------------------------------------------------------------
def _safe_filename(title, extension):
    """Turn a display title (which may contain unicode like the '—' between
    school name and stream name) into a plain-ASCII filename. HTTP headers
    must be latin-1/ASCII-safe — a non-ASCII character in Content-Disposition
    causes a 500 error on a real WSGI server (e.g. gunicorn on Render), even
    though it can look fine when tested locally. Non-ASCII characters are
    dropped rather than substituted, and runs of anything else collapse to a
    single underscore, so 'School — Timetable for X' becomes
    'School_Timetable_for_X.pdf'."""
    ascii_only = title.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_")
    return f"{safe or 'timetable'}.{extension}"


def _rows_with_breaks(periods):
    """Interleave lesson periods with break marker rows, in day order.
    `periods` is a list of period rows (period_number, start_time, end_time).
    Returns a list of dicts, each either:
      {"type": "lesson", "period_number": n, "start_time": s, "end_time": e}
      {"type": "break", "label": lbl, "start_time": s, "end_time": e}
    """
    break_by_after = {b["after_period"]: b for b in db.get_break_rows()}
    rows = []
    for p in periods:
        rows.append({
            "type": "lesson",
            "period_number": p["period_number"],
            "start_time": p["start_time"],
            "end_time": p["end_time"],
        })
        b = break_by_after.get(p["period_number"])
        if b:
            rows.append({
                "type": "break",
                "label": b["label"],
                "start_time": b["start_time"],
                "end_time": b["end_time"],
            })
    return rows


def _grid_data(mode, target):
    days = db.get_active_days()
    # A class uses its grade's day profile. A teacher can serve classes on
    # different grids, so with more than one profile their timetable is laid
    # out by clock time instead of period number.
    merged = mode != "stream" and day_profiles.profile_count() > 1
    if mode == "stream":
        display_rows = day_profiles.rows_with_breaks(day_profiles.profile_id_for_stream(target))
    elif merged:
        display_rows = day_profiles.teacher_rows()
    else:
        display_rows = day_profiles.rows_with_breaks(day_profiles.default_id())

    if mode == "stream":
        stream = db.fetch_one("SELECT * FROM streams WHERE id=?", (target,))
        if not stream:
            return None
        rows = db.fetch_all(
            """SELECT p.day, p.period_number, sub.name as subject_name,
                      t.name as teacher_name, rm.name as room_name
               FROM timetable_entries te
               JOIN periods p ON te.period_id = p.id
               JOIN subjects sub ON te.subject_id = sub.id
               LEFT JOIN teachers t ON te.teacher_id = t.id
               LEFT JOIN rooms rm ON te.room_id = rm.id
               WHERE te.stream_id = ?
               ORDER BY sub.name""",
            (stream["id"],),
        )
        cells = {}
        for r in rows:
            key = (r["day"], r["period_number"])
            piece = f"{r['subject_name']} ({r['teacher_name'] or '-'}, {r['room_name'] or '-'})"
            cells[key] = f"{cells[key]} / {piece}" if key in cells else piece
        title = f"{db.get_school_settings()['name']} — Timetable for {stream['name']}"
    else:
        teacher = db.fetch_one("SELECT * FROM teachers WHERE id=?", (target,))
        if not teacher:
            return None
        rows = db.fetch_all(
            """SELECT p.day, p.period_number, p.start_time, sub.name as subject_name,
                      st.name as stream_name, rm.name as room_name
               FROM timetable_entries te
               JOIN periods p ON te.period_id = p.id
               JOIN subjects sub ON te.subject_id = sub.id
               JOIN streams st ON te.stream_id = st.id
               LEFT JOIN rooms rm ON te.room_id = rm.id
               WHERE te.teacher_id = ?
               ORDER BY sub.name""",
            (teacher["id"],),
        )
        cells = {}
        for r in rows:
            key = (r["day"], r["start_time"] if merged else r["period_number"])
            piece = f"{r['subject_name']} - {r['stream_name']} ({r['room_name'] or '-'})"
            cells[key] = f"{cells[key]} / {piece}" if key in cells else piece
        title = f"{db.get_school_settings()['name']} — Timetable for {teacher['name']}"

    return title, days, display_rows, cells


@app.route("/view")
def view_page():
    streams = db.fetch_all(
        """SELECT s.id, s.name, g.name as grade_name FROM streams s
           JOIN grades g ON s.grade_id = g.id ORDER BY g.sort_order, g.id, s.name"""
    )
    teachers = db.fetch_all("SELECT id, name FROM teachers ORDER BY name")

    mode = request.args.get("mode", "stream")
    target = request.args.get("target", type=int)

    grid = None
    if target:
        grid = _grid_data(mode, target)

    return render_template(
        "view.html", streams=streams, teachers=teachers, mode=mode, target=target, grid=grid,
    )


@app.route("/export.csv")
def export_csv():
    mode = request.args.get("mode", "stream")
    target = request.args.get("target", type=int)
    data = _grid_data(mode, target)
    if not data:
        return "Nothing to export", 400
    title, days, display_rows, cells = data

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Period", "Time"] + days)
    for r in display_rows:
        if r["type"] == "break":
            row = [r["label"], f"{r['start_time']}-{r['end_time']}"] + [""] * len(days)
        else:
            pnum = r["period_number"]
            row = [r.get("plabel") or f"P{pnum}", f"{r['start_time']}-{r['end_time']}"] + [cells.get((d, pnum), "") for d in days]
        writer.writerow(row)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={_safe_filename(title, 'csv')}"},
    )


@app.route("/export.pdf")
def export_pdf():
    mode = request.args.get("mode", "stream")
    target = request.args.get("target", type=int)
    data = _grid_data(mode, target)
    if not data:
        return "Nothing to export", 400
    title, days, display_rows, cells = data

    page_size = landscape(A4)
    left_margin = right_margin = 14 * mm
    top_margin = bottom_margin = 12 * mm

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=page_size,
        leftMargin=left_margin, rightMargin=right_margin,
        topMargin=top_margin, bottomMargin=bottom_margin,
    )
    styles = getSampleStyleSheet()
    story = branding.logo_story() + [Paragraph(title, styles["Title"]), Spacer(1, 10)]

    # Build the table body: a header row, then one row per lesson period or
    # break marker (mirrors the on-screen grid and the CSV export).
    table_data = [["Period"] + days]
    break_row_indices = []
    for r in display_rows:
        row_index = len(table_data)  # index this row will land at in table_data
        if r["type"] == "break":
            label = f"{r['label']}  ({r['start_time']}\u2013{r['end_time']})"
            table_data.append([label] + [""] * len(days))
            break_row_indices.append(row_index)
        else:
            pnum = r["period_number"]
            period_label = f"{r.get('plabel') or 'P' + str(pnum)}\n{r['start_time']}-{r['end_time']}"
            row = [period_label] + [cells.get((d, pnum), "\u2014") for d in days]
            table_data.append(row)

    usable_width = page_size[0] - left_margin - right_margin
    period_col_width = 26 * mm
    day_col_width = (usable_width - period_col_width) / len(days)
    col_widths = [period_col_width] + [day_col_width] * len(days)

    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103524")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e3dcc9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbf8f0")]),
    ]
    for idx in break_row_indices:
        style_cmds.append(("SPAN", (0, idx), (len(days), idx)))
        style_cmds.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#f4e6c6")))
        style_cmds.append(("FONTNAME", (0, idx), (-1, idx), "Helvetica-Bold"))
        style_cmds.append(("TEXTCOLOR", (0, idx), (-1, idx), colors.HexColor("#103524")))

    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    doc.build(story)
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment;filename={_safe_filename(title, 'pdf')}"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
