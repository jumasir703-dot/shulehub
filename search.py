"""
search.py - ShuleHub search: the full results page (/search/) and the live
suggestions the search box shows while typing (/search/suggest).

Searches students (name, admission no, class, guardian name or phone), teachers,
library books and the app's own pages. Every word typed must match, in any order.
Results are limited to what the signed-in person's role may open.
"""
import re

from flask import Blueprint, current_app, g, jsonify, render_template, request, url_for

import auth
import database as db

search_bp = Blueprint("search", __name__, url_prefix="/search")

MIN_CHARS = 2
PAGE_LIMIT = 25
SUGGEST_LIMIT = 5

# title, endpoint, extra words people might type
PAGES = [
    ("Dashboard", "dashboard.index", "home overview summary"),
    ("Grades & Streams", "grades_page", "classes forms"),
    ("Subjects", "subjects_page", "learning areas"),
    ("Teachers", "teachers_page", "staff tutors"),
    ("Rooms", "rooms_page", "classrooms labs"),
    ("Day Structure", "day_structure_page", "periods lessons breaks bells"),
    ("Timetable Builder", "builder_page", "generate timetable"),
    ("View / Export Timetable", "view_page", "print pdf csv class teacher"),
    ("Remedial Timetable", "remedial.view_page", "extra hours"),
    ("Master Timetable", "master.index", "whole school"),
    ("Scheduling Rules", "rules_page", "constraints"),
    ("Terms & Rotation", "terms.index", "calendar term dates"),
    ("My Marks", "mymarks.index", "enter marks"),
    ("Published Results", "mymarks.published", "results"),
    ("Compile & Publish", "results.index", "results publish"),
    ("Subject Allocations", "results.allocations", "teachers subjects"),
    ("Exams", "analytics.exams_page", "analysis merit list"),
    ("Compare Exams", "analytics.compare_page", "comparison"),
    ("Grading Scales", "analytics.grading_page", "grades points"),
    ("Invigilation", "invigilation.index", "exam duty"),
    ("Students & Guardians", "school.students", "pupils learners register"),
    ("Promotion", "school.promote", "move class next year"),
    ("Attendance", "attendance.index", "register present absent"),
    ("Teacher Attendance", "teacher_attendance.index", "staff attendance"),
    ("Lesson Attendance", "lesson_attendance.index", "lessons"),
    ("Fees & Payments", "fees.index", "money balance receipt mpesa invoice"),
    ("Download Attendance", "attexport.index", "attendance export pdf csv register grid summary"),
    ("Clearance", "clearance.index", "clearance form cleared leaving fees library sign off"),
    ("Lesson Plans", "lp.index", "lesson plan dos marking schemes teaching"),
    ("Library", "library.index", "books issue return fines"),
    ("Library Catalogue", "library.catalogue", "books isbn"),
    ("Messages", "msg.index", "sms email notice"),
    ("School Settings", "settings_page", "logo name tagline"),
    ("Setup Wizard", "onboarding.welcome", "setup"),
    ("Staff & Logins", "auth.staff", "users passwords"),
    ("Roles & Access", "auth.roles_page", "permissions services"),
    ("My Account", "auth.account", "password profile"),
    ("Quick-Start Guide", "guide_page", "help how to"),
]


# ------------------------------------------------------------------ helpers
def _tokens(q):
    return [t for t in re.split(r"\s+", q.strip().lower()) if t]


def _like(token):
    esc = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + esc + "%"


def _phone_like(token):
    """0712 345 678, +254712345678 and 712345678 all become the same pattern."""
    digits = re.sub(r"\D", "", token)
    return "%" + digits[-9:] + "%" if len(digits) >= 7 else _like(token)


def _can_open(endpoint):
    if endpoint not in current_app.view_functions:
        return False
    return auth.allowed(g.user["role"], endpoint)


def _student_url(student_id):
    try:
        rule = next(iter(current_app.url_map.iter_rules("school.student_detail")))
        arg = next(iter(rule.arguments), "student_id")
        return url_for("school.student_detail", **{arg: student_id})
    except Exception:
        return url_for("school.students")


_ESC = " ESCAPE '\\'"
_GPHONE = "REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(gu.phone, ''), ' ', ''), '-', ''), '+', ''), '(', '')"


# ------------------------------------------------------------------ searches
def _students(tokens, limit):
    where, params = [], []
    for t in tokens:
        where.append(
            "(LOWER(st.name) LIKE ?" + _ESC + " OR LOWER(COALESCE(st.admission_no, '')) LIKE ?" + _ESC +
            " OR LOWER(COALESCE(g.name, '')) LIKE ?" + _ESC + " OR LOWER(COALESCE(sm.name, '')) LIKE ?" + _ESC +
            " OR EXISTS (SELECT 1 FROM guardians gu WHERE gu.student_id = st.id AND (LOWER(gu.name) LIKE ?" + _ESC +
            " OR " + _GPHONE + " LIKE ?" + _ESC + ")))")
        params += [_like(t)] * 5 + [_phone_like(t)]
    rows = db.fetch_all(
        "SELECT st.id, st.name, st.admission_no, g.name AS grade_name, sm.name AS stream_name "
        "FROM students st LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "WHERE st.active = 1 AND " + " AND ".join(where) + " ORDER BY st.name LIMIT " + str(int(limit) + 1),
        tuple(params))
    items = [{"title": r["name"], "badge": r["admission_no"] or "",
              "sub": r["stream_name"] or r["grade_name"] or "", "url": _student_url(r["id"])}
             for r in rows[:limit]]
    return items, len(rows) > limit


def _teachers(tokens, limit):
    where = " AND ".join(["LOWER(name) LIKE ?" + _ESC] * len(tokens))
    rows = db.fetch_all("SELECT id, name FROM teachers WHERE " + where + " ORDER BY name LIMIT " + str(int(limit) + 1),
                        tuple(_like(t) for t in tokens))
    endpoint = "teachers_page" if auth.has_service(g.user, "timetable_edit") else "view_page"
    return ([{"title": r["name"], "badge": "", "sub": "Teacher", "url": url_for(endpoint)} for r in rows[:limit]],
            len(rows) > limit)


def _books(tokens, limit):
    where, params = [], []
    for t in tokens:
        where.append("(LOWER(title) LIKE ?" + _ESC + " OR LOWER(COALESCE(author, '')) LIKE ?" + _ESC +
                     " OR LOWER(COALESCE(isbn, '')) LIKE ?" + _ESC + ")")
        params += [_like(t)] * 3
    rows = db.fetch_all("SELECT id, title, author FROM lib_books WHERE active = 1 AND " + " AND ".join(where) +
                        " ORDER BY title LIMIT " + str(int(limit) + 1), tuple(params))
    return ([{"title": r["title"], "badge": "", "sub": r["author"] or "",
              "url": url_for("library.book_detail", book_id=r["id"])} for r in rows[:limit]],
            len(rows) > limit)


def _pages(tokens, limit):
    found = []
    for title, endpoint, words in PAGES:
        hay = (title + " " + words).lower()
        if all(t in hay for t in tokens) and _can_open(endpoint):
            found.append({"title": title, "badge": "", "sub": "Page", "url": url_for(endpoint)})
    return found[:limit], len(found) > limit


def _run(q, limit):
    tokens = _tokens(q)
    plan = []
    if auth.has_service(g.user, "students_view"):
        plan.append(("students", "Students", _students))
    if auth.has_service(g.user, "timetable_view") or auth.has_service(g.user, "timetable_edit"):
        plan.append(("teachers", "Teachers", _teachers))
    if "library" in current_app.blueprints and _can_open("library.catalogue"):
        plan.append(("books", "Library books", _books))
    plan.append(("pages", "Pages", _pages))
    groups = []
    for key, label, fn in plan:
        try:
            items, more = fn(tokens, limit)
        except Exception as err:  # one failing source must not blank the whole search
            current_app.logger.warning("search source %s failed: %s", key, err)
            continue
        if items:
            groups.append({"key": key, "label": label, "items": items, "more": more})
    return groups


# ------------------------------------------------------------------ routes
@search_bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    too_short = bool(q) and len(q) < MIN_CHARS
    groups = _run(q, PAGE_LIMIT) if q and not too_short else []
    total = sum(len(gr["items"]) for gr in groups)
    return render_template("search/results.html", q=q, too_short=too_short, groups=groups, total=total)


@search_bp.route("/suggest")
def suggest():
    q = request.args.get("q", "").strip()
    if len(q) < MIN_CHARS:
        return jsonify({"groups": []})
    return jsonify({"groups": _run(q, SUGGEST_LIMIT)})
