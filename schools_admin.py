"""
schools_admin.py - the "Schools" pages for a school group.

    /schools/                 every school at a glance, with group totals and charts
    /schools/new              add a sister school (pick its level)
    /schools/<id>/edit        rename, change level, add standard grades, archive
    /schools/<id>/people      who may work in that school
    /schools/switch  (POST)   change the school you are working in (everyone)

Only group administrators (the "Schools" flag on an administrator) can open
anything here except switching.
"""
from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from markupsafe import Markup

import database as db
import tenancy

schools_bp = Blueprint("schools", __name__, url_prefix="/schools")


@schools_bp.before_request
def _only_group_admins():
    if request.endpoint == "schools.switch":
        return None
    if not tenancy.is_group_admin(getattr(g, "user", None)):
        return render_template("auth/forbidden.html"), 403
    return None


def _school_or_404(school_id):
    for s in tenancy.list_schools(include_inactive=True):
        if s["id"] == school_id:
            return s
    return None


# ------------------------------------------------------------------ switch (everyone)
@schools_bp.route("/switch", methods=["POST"])
def switch():
    try:
        wanted = int(request.form.get("school_id", ""))
    except ValueError:
        wanted = None
    if wanted in {s["id"] for s in tenancy.allowed_schools(g.user["id"])}:
        session["school_id"] = wanted
        flash(f"Now working in {tenancy.school_name(wanted)}.", "success")
    else:
        flash("You don't have access to that school.", "error")
    return redirect(url_for("home"))


# ------------------------------------------------------------------ overview
@schools_bp.route("/")
def index():
    rows = []
    for s in tenancy.list_schools(include_inactive=True):
        snap = tenancy.snapshot(s["id"]) if s["active"] else {}
        rows.append({**s, "display": tenancy.school_name(s["id"]), "level_label": tenancy.level_label(s["level"]),
                     "snap": snap})
    live = [r for r in rows if r["active"]]
    totals = {
        "schools": len(live),
        "students": sum(r["snap"].get("students") or 0 for r in live),
        "teachers": sum(r["snap"].get("teachers") or 0 for r in live),
        "collected": sum(r["snap"].get("collected_month") or 0 for r in live),
        "outstanding": sum(r["snap"].get("outstanding") or 0 for r in live),
    }
    chart_students = chart_fees = ""
    try:
        import charts
        names = [r["display"] for r in live]
        chart_students = charts.hbar(names, [r["snap"].get("students") or 0 for r in live],
                                     name="Students", head="School", aria="Students in each school")
        chart_fees = charts.vbar(names, [
            {"name": "Collected this month", "values": [r["snap"].get("collected_month") or 0 for r in live]},
            {"name": "Still owing", "values": [r["snap"].get("outstanding") or 0 for r in live]}],
            unit="KES", head="School", aria="Fees collected and owing in each school")
    except Exception:
        pass
    return render_template("schools/index.html", rows=rows, totals=totals, current=g.school_id,
                           chart_students=Markup(chart_students) if chart_students else "",
                           chart_fees=Markup(chart_fees) if chart_fees else "")


# ------------------------------------------------------------------ add a school
@schools_bp.route("/new", methods=["GET", "POST"])
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        level = request.form.get("level", "custom")
        if not name:
            flash("Give the school a name.", "error")
        else:
            try:
                sid = tenancy.create_school(name, level, request.form.get("tagline", "").strip())
            except Exception as exc:  # pragma: no cover - surfaced to the person, not swallowed
                flash(f"Couldn't create the school: {exc}", "error")
            else:
                n = len(tenancy.LEVELS.get(level, tenancy.LEVELS["custom"])[2])
                flash(f"{name} was created" + (f" with {n} grades ready." if n else "."), "success")
                return redirect(url_for("schools.people", school_id=sid))
    return render_template("schools/new.html", levels=tenancy.LEVELS, form=request.form)


# ------------------------------------------------------------------ edit
@schools_bp.route("/<int:school_id>/edit", methods=["GET", "POST"])
def edit(school_id):
    s = _school_or_404(school_id)
    if not s:
        flash("That school doesn't exist.", "error")
        return redirect(url_for("schools.index"))
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action in ("archive", "restore"):
            if school_id == tenancy.PRIMARY:
                flash("The first school can't be archived.", "error")
            else:
                with tenancy.using(tenancy.PRIMARY):
                    db.execute("UPDATE schools SET active=? WHERE id=?", (1 if action == "restore" else 0, school_id))
                flash("School restored." if action == "restore" else
                      "School archived. Its records are kept and it can be restored at any time.", "success")
            tenancy.forget_names()
            return redirect(url_for("schools.index"))
        name = request.form.get("name", "").strip()
        level = request.form.get("level", s["level"])
        if level not in tenancy.LEVELS and level != "mixed":
            level = s["level"]
        if not name:
            flash("Give the school a name.", "error")
        else:
            with tenancy.using(school_id):
                cur = db.get_school_settings()
                db.set_school_settings(name, request.form.get("tagline", cur["tagline"]).strip(),
                                       cur["brand_mark"], cur["active_days"])
            with tenancy.using(tenancy.PRIMARY):
                db.execute("UPDATE schools SET name=?, level=? WHERE id=?", (name, level, school_id))
            added = tenancy.apply_level(school_id, level) if request.form.get("add_grades") else 0
            tenancy.forget_names()
            flash("Saved." + (f" {added} grade(s) added." if added else ""), "success")
            return redirect(url_for("schools.index"))
    with tenancy.using(school_id):
        settings = db.get_school_settings()
    return render_template("schools/edit.html", s=s, settings=settings, levels=tenancy.LEVELS,
                           level_label=tenancy.level_label(s["level"]))


# ------------------------------------------------------------------ people
def _people(school_id):
    with tenancy.using(tenancy.PRIMARY):
        users = [dict(r) for r in db.fetch_all(
            "SELECT id, full_name, username, role, group_admin, active FROM users ORDER BY full_name")]
        rows = db.fetch_all("SELECT user_id, school_id, teacher_id FROM user_schools")
    has_rows, here = set(), {}
    for r in rows:
        has_rows.add(r["user_id"])
        if r["school_id"] == school_id:
            here[r["user_id"]] = r["teacher_id"]
    for u in users:
        u["group_admin"] = tenancy.user_access(u["id"])[0] if u["role"] == "admin" else 0
        implicit = school_id == tenancy.PRIMARY and u["id"] not in has_rows
        u["access"] = u["id"] in here or implicit or bool(u["group_admin"])
        u["teacher_id"] = here.get(u["id"])
    return users


@schools_bp.route("/<int:school_id>/people", methods=["GET", "POST"])
def people(school_id):
    s = _school_or_404(school_id)
    if not s:
        flash("That school doesn't exist.", "error")
        return redirect(url_for("schools.index"))
    with tenancy.using(school_id):
        teachers = [dict(r) for r in db.fetch_all("SELECT id, name FROM teachers ORDER BY name")]
    users = _people(school_id)
    if request.method == "POST":
        want = {int(x) for x in request.form.getlist("access") if x.isdigit()}
        admins = {int(x) for x in request.form.getlist("group_admin") if x.isdigit()}
        valid_t = {t["id"] for t in teachers}
        with tenancy.using(tenancy.PRIMARY):
            everything, old_teacher = {}, {}
            for r in db.fetch_all("SELECT user_id, school_id, teacher_id FROM user_schools"):
                everything.setdefault(r["user_id"], set()).add(r["school_id"])
                old_teacher[(r["user_id"], r["school_id"])] = r["teacher_id"]
        problems, stmts = [], []
        keep_admins = 0
        for u in users:
            uid = u["id"]
            is_admin = (uid in admins) if u["role"] == "admin" else False
            if u["role"] == "admin" and u["active"] and is_admin:
                keep_admins += 1
            # School 1 access that was implicit becomes explicit, so unticking really removes it
            have = set(everything.get(uid, set())) or {tenancy.PRIMARY}
            if uid in want or is_admin:
                have.add(school_id)
            else:
                have.discard(school_id)
            if not have and not is_admin:
                problems.append(u["full_name"])
            tid = request.form.get(f"teacher_{uid}", "")
            tid = int(tid) if tid.isdigit() and int(tid) in valid_t else None
            stmts.append((uid, is_admin, have, tid))

        if keep_admins == 0:
            flash("At least one administrator must stay in charge of the whole group.", "error")
        elif problems:
            flash("Everyone needs at least one school. Give these people another school first, or switch their "
                  "account off under Staff: " + ", ".join(problems), "error")
        else:
            with tenancy.using(tenancy.PRIMARY):
                for uid, is_admin, have, tid in stmts:
                    db.execute("UPDATE users SET group_admin=? WHERE id=?", (1 if is_admin else 0, uid))
                    db.execute("DELETE FROM user_schools WHERE user_id=?", (uid,))
                    for sid in sorted(have):
                        db.execute("INSERT INTO user_schools (user_id, school_id, teacher_id) VALUES (?, ?, ?)",
                                   (uid, sid, tid if sid == school_id else old_teacher.get((uid, sid))))
            flash("Saved.", "success")
            return redirect(url_for("schools.people", school_id=school_id))
    return render_template("schools/people.html", s=s, users=users, teachers=teachers,
                           name=tenancy.school_name(school_id))

