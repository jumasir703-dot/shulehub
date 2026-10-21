"""
permissions.py — the list of "services" an administrator can switch on or off
for a role, and which page (endpoint) belongs to which service.

How access works
----------------
* Every person has ONE role (Teacher, Bursar, Deputy Principal, ...).
* A role is a named set of services, ticked by the administrator on the
  Roles & Access page. The built-in Administrator role always has everything.
* auth.gate() asks `service_for(endpoint)` which service a page belongs to and
  lets the person in only if their role includes it.
* Any endpoint that is NOT listed here is administrator-only. That is
  deliberate: a page added later is locked down until someone maps it.

Settings, Setup Wizard, Staff & Logins and Roles & Access can never be handed
to another role.
"""

# key, label, description — the order here is the order shown on the page.
SERVICES = [
    ("timetable_view", "View & export timetables",
     "Open class and teacher timetables and download them as PDF or CSV."),
    ("timetable_edit", "Build & edit the timetable",
     "Grades, streams, subjects, teachers, rooms, day structure, scheduling rules and the timetable builder."),
    ("exams", "Exam analysis (everything)",
     "Create exams, enter or import marks for ANY subject, and see merit lists, subject analysis, comparisons and report forms at any time, published or not."),
    ("marks_entry", "Enter marks for my subjects",
     "Subject teachers: type or upload marks only for the subjects and classes allocated to them, submit them, and view results once they are published."),
    ("results_publish", "Compile & publish results",
     "See which subject marks are in, allocate subjects to teachers, reopen a submission, preview the compiled results and publish them."),
    ("exams_admin", "Exam administration",
     "Edit the exam-module student register and change grading scales."),
    ("students_view", "View students & guardians",
     "Read student records and guardian contacts."),
    ("students_manage", "Manage students",
     "Admit, edit, import, withdraw and promote students; manage guardians; export the register."),
    ("attendance", "Attendance",
     "Mark daily attendance and see attendance reports."),
    ("fees", "Fees & payments",
     "Fee structure, invoices, payments, M-Pesa import, receipts and balances."),
]
SERVICE_KEYS = [k for k, _, _ in SERVICES]
SERVICE_LABELS = {k: label for k, label, _ in SERVICES}

# Starting point for the two roles that existed before roles became editable.
# These match what Teacher and Bursar could already do, so nothing changes for
# an existing school until the administrator edits them.
DEFAULT_ROLES = {
    "admin": ("Administrator", ["*"]),
    "teacher": ("Teacher", ["timetable_view", "marks_entry", "attendance", "students_view"]),
    "bursar": ("Bursar", ["students_view", "fees"]),
}
RESERVED_KEYS = set(DEFAULT_ROLES)

# Pages that more than one service can open are listed with a tuple: a person
# needs ANY ONE of them. Results pages are open to whoever compiles them
# (exams, results_publish) and, for the two screens teachers may see, to
# marks_entry too — but analytics.py only lets marks_entry users in once the
# exam has been published.
_RESULTS_VIEW = ("exams", "results_publish", "marks_entry")
_RESULTS_STAFF = ("exams", "results_publish")

_EXACT = {
    # timetable viewing
    "view_page": "timetable_view", "export_csv": "timetable_view", "export_pdf": "timetable_view",
    # student records
    "school.students": "students_view", "school.student_detail": "students_view",
    # exam-module register and grading scales
    "analytics.student_add": "exams_admin", "analytics.students_import": "exams_admin",
    "analytics.student_update": "exams_admin", "analytics.student_delete": "exams_admin",
    "analytics.grading_page": "exams_admin", "analytics.grading_save": "exams_admin",
    # compiled results
    "analytics.exam_overview": _RESULTS_VIEW, "analytics.merit_list": _RESULTS_VIEW,
    "analytics.subject_analysis": _RESULTS_STAFF, "analytics.merit_csv": _RESULTS_STAFF,
    "analytics.merit_pdf": _RESULTS_STAFF, "analytics.reports_pdf": _RESULTS_STAFF,
}

_TIMETABLE_EDIT = {
    "grades_page", "add_grade", "delete_grade", "add_stream", "delete_stream",
    "subjects_page", "add_subject", "delete_subject", "add_subject_group", "delete_subject_group",
    "set_subject_group", "update_subject_periods",
    "teachers_page", "add_teacher", "update_teacher", "delete_teacher", "save_teacher_subjects",
    "rooms_page", "add_room", "delete_room",
    "day_structure_page", "save_day_structure", "new_day_profile", "delete_day_profile", "assign_day_profile",
    "builder_page", "generate_timetable",
    "api_get_cell", "api_save_cell", "api_clear_cell", "api_move_cell", "api_save_group_cell",
    "rules_page", "save_rules", "add_subject_rule", "delete_subject_rule",
}

_PREFIXES = (
    ("analytics.", "exams"),
    ("mymarks.", "marks_entry"),
    ("results.", "results_publish"),
    ("attendance.", "attendance"),
    ("fees.", "fees"),
    ("school.", "students_manage"),
)


def services_for(endpoint):
    """Services that open a page — a tuple, any one of which is enough.
    Empty tuple = administrator only."""
    if endpoint in _EXACT:
        found = _EXACT[endpoint]
        return found if isinstance(found, tuple) else (found,)
    if endpoint in _TIMETABLE_EDIT:
        return ("timetable_edit",)
    for prefix, service in _PREFIXES:
        if endpoint.startswith(prefix):
            return (service,)
    return ()


def service_for(endpoint):
    """First service for a page, or None (kept for older callers)."""
    found = services_for(endpoint)
    return found[0] if found else None


def clean_services(values):
    """Keep only real service keys, in catalogue order."""
    wanted = set(values or [])
    return [k for k in SERVICE_KEYS if k in wanted]
