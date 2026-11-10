"""
dashboard.py - the ShuleHub Dashboard: a few live figures for today, quick links,
and (for administrators) shortcuts to the settings pages.

Each card only appears if the signed-in person's role can open the page behind it,
and one card failing (for example a table that does not exist yet) never breaks
the rest of the page. Each person can also hide cards with "Customise"; that
choice is remembered in their own browser.
"""
from datetime import date

from flask import Blueprint, current_app, g, render_template, url_for

import auth
import database as db

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


def _n(sql, params=()):
    row = db.fetch_one(sql, params)
    return (row["n"] or 0) if row else 0


def _can_open(endpoint):
    """True if the page exists in this app and this person's role may open it."""
    if endpoint not in current_app.view_functions:
        return False
    return auth.allowed(g.user["role"], endpoint)


def _money(x):
    return f"KES {float(x or 0):,.0f}"


def _cards():
    user, cards, today = g.user, [], date.today().isoformat()

    def add(key, title, value, sub, endpoint, label):
        cards.append({"key": key, "title": title, "value": value, "sub": sub,
                      "url": url_for(endpoint) if endpoint and _can_open(endpoint) else None,
                      "label": label})

    def attempt(fn):
        try:
            fn()
        except Exception as err:  # one broken card must not take the page down
            current_app.logger.warning("dashboard card failed: %s", err)

    if auth.has_service(user, "students_view"):
        attempt(lambda: add("students", "Students", _n("SELECT COUNT(*) AS n FROM students WHERE active = 1"),
                            "active on the register", "school.students", "Open students"))

    if auth.has_service(user, "timetable_view") or auth.has_service(user, "timetable_edit"):
        endpoint = "teachers_page" if auth.has_service(user, "timetable_edit") else "view_page"
        attempt(lambda: add("teachers", "Teachers", _n("SELECT COUNT(*) AS n FROM teachers"),
                            "on staff", endpoint, "Open"))

    if auth.has_service(user, "attendance"):
        def att():
            rows = {r["status"]: r["c"] for r in db.fetch_all(
                "SELECT status, COUNT(*) AS c FROM attendance WHERE att_date = ? GROUP BY status", (today,))}
            marked = sum(rows.values())
            if marked:
                pct = round(100 * (rows.get("P", 0) + rows.get("L", 0)) / marked, 1)
                add("attendance", "Attendance today", f"{pct}%",
                    f"{marked} marked · {rows.get('A', 0) + rows.get('E', 0)} absent",
                    "attendance.index", "Open attendance")
            else:
                add("attendance", "Attendance today", "Not marked", "nobody has been marked yet today",
                    "attendance.index", "Mark attendance")
        attempt(att)

    if auth.has_service(user, "fees"):
        def fees():
            from fees import outstanding_total
            month = db.fetch_one(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE voided = 0 AND paid_on >= ?",
                (date.today().replace(day=1).isoformat(),))["s"]
            add("fees", "Fees this month", _money(month), f"{_money(outstanding_total())} still owed",
                "fees.index", "Open fees")
        attempt(fees)

    if "library" in current_app.blueprints and auth.has_service(user, "library"):
        def lib():
            out = _n("SELECT COUNT(*) AS n FROM lib_loans WHERE returned_on IS NULL")
            late = _n("SELECT COUNT(*) AS n FROM lib_loans WHERE returned_on IS NULL AND due_on < ?", (today,))
            add("library", "Library", out, f"books on loan · {late} overdue", "library.index", "Open library")
        attempt(lib)

    if "lp" in current_app.blueprints and auth.has_service(user, "lesson_plans_mark"):
        def plans():
            waiting = _n("SELECT COUNT(*) AS n FROM lp_plans WHERE status = 'submitted'")
            add("plans", "Lesson plans", waiting, "waiting to be marked", "lp.index", "Mark plans")
        attempt(plans)

    if auth.has_service(user, "exams"):
        def exams():
            last = db.fetch_one("SELECT * FROM exams ORDER BY id DESC LIMIT 1")
            name = last["name"] if last and "name" in last.keys() else None
            add("exams", "Exams", _n("SELECT COUNT(*) AS n FROM exams"),
                f"latest: {name}" if name else "created so far", "analytics.exams_page", "Open exams")
        attempt(exams)

    return cards


_QUICK = [
    ("Mark attendance", "attendance.index"), ("Download attendance", "attexport.index"), ("Record a payment", "fees.index"),
    ("Clearance", "clearance.index"), ("Lesson plans", "lp.index"),
    ("Timetable builder", "builder_page"), ("View timetables", "view_page"),
    ("Enter my marks", "mymarks.index"), ("Library desk", "library.index"),
    ("Send messages", "msg.index"), ("Search", "search.index"),
]
_ADMIN = [
    ("School Settings", "settings_page", "Name, logo, tagline and teaching days"),
    ("Roles & Access", "auth.roles_page", "Choose what each role can open"),
    ("Staff & Logins", "auth.staff", "Add staff and reset passwords"),
    ("Theme & Colours", "themes.index", "Choose the school's colours and style"),
    ("Setup Wizard", "onboarding.welcome", "Run the first-time setup again"),
]


@dashboard_bp.route("/")
def index():
    quick = [{"label": l, "url": url_for(e)} for l, e in _QUICK if _can_open(e)]
    admin = ([{"label": l, "url": url_for(e), "hint": h} for l, e, h in _ADMIN if e in current_app.view_functions]
             if g.user["role"] == "admin" else [])
    return render_template("dashboard/index.html", cards=_cards(), quick=quick, admin=admin,
                           today_label=date.today().strftime("%A, %d %B %Y"))
