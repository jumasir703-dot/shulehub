"""
demo_data.py — builds the sample school used by "View example".

build() creates a brand-new SQLite file and fills it with a pretend school
(Mwanga Academy): teachers, classes, a generated timetable, students with
guardians, three rounds of exam marks, two weeks of attendance and a term of
fee invoices with payments. It never reads or writes the real database.

Everything is generated from a fixed random seed, so the example looks the same
every time. All names and phone numbers are made up (the phone numbers are
0700 000 xxx, which are not real lines).

The only thing copied across from the real install is the signed-in person's
login (same id and password hash, made an administrator inside the example),
so they stay signed in when they step into the example.
"""

import os
import random
from datetime import date, timedelta

import database as db
import fees
import scheduler

SCHOOL_NAME = "Mwanga Academy"
SCHOOL_TAGLINE = "Example school · sample data"
GRADES = ("Grade 7", "Grade 8")
STREAMS = [("Grade 7", "Grade 7 East"), ("Grade 7", "Grade 7 West"), ("Grade 8", "Grade 8 East")]
STUDENTS_PER_STREAM = 14

# (teacher, [subjects they teach in both grades])
TEACHERS = [
    ("Peter Otieno",   ["English", "Life Skills Education"]),
    ("Grace Wanjiru",  ["Kiswahili / KSL", "Creative Arts"]),
    ("James Kiprop",   ["Mathematics"]),
    ("Hellen Mutua",   ["Mathematics", "Business Studies"]),
    ("Faith Achieng",  ["Integrated Science", "Health Education"]),
    ("Samuel Mwangi",  ["Pre-Technical Studies", "Agriculture"]),
    ("Mary Njeri",     ["Social Studies", "Religious Education"]),
    ("Daniel Kamau",   ["Sports & Physical Education"]),
]
EXTRA_ROOMS = ["Room 3"]

# Subjects examined, and how hard each tends to be (added to a student's ability).
EXAM_SUBJECTS = {
    "English": 2, "Kiswahili / KSL": 0, "Mathematics": -6, "Integrated Science": -3,
    "Social Studies": 3, "Religious Education": 6, "Pre-Technical Studies": 1, "Agriculture": 4,
}

FEMALE = ["Wanjiku", "Akinyi", "Zawadi", "Neema", "Mwajuma", "Chebet", "Njeri", "Atieno", "Faith",
          "Joy", "Mercy", "Lilian", "Esther", "Naomi", "Purity", "Winnie", "Ivy", "Brenda",
          "Sharon", "Diana", "Khadija", "Amina", "Nyambura", "Wambui"]
MALE = ["Kevin", "Brian", "Otieno", "Kiprono", "Mwangi", "Juma", "Hassan", "Dennis", "Victor",
        "Collins", "Ian", "Eric", "Allan", "Felix", "Gideon", "Isaac", "Moses", "Peter",
        "Samuel", "Tom", "Wekesa", "Barasa", "Kamau", "Baraka"]
SURNAMES = ["Mwangi", "Otieno", "Kamau", "Njoroge", "Chebet", "Kiprotich", "Wafula", "Achieng",
            "Omondi", "Wanjiru", "Mutua", "Kariuki", "Odhiambo", "Cheruiyot", "Ndegwa", "Abdi",
            "Hussein", "Nyongesa", "Mbugua", "Langat", "Muriuki", "Onyango", "Kimani", "Rotich"]

FEE_ITEMS = {
    "Grade 7": [("Tuition", 12000), ("Lunch programme", 6500), ("Activity & sports", 1500),
                ("Exams & assessment", 1200), ("ICT lab", 800)],
    "Grade 8": [("Tuition", 12500), ("Lunch programme", 6500), ("Activity & sports", 1500),
                ("Exams & assessment", 1200), ("ICT lab", 800)],
}


def _term_and_year(today):
    term = 1 if today.month <= 4 else 2 if today.month <= 8 else 3
    return term, today.year


def _previous_term(term, year):
    return (term - 1, year) if term > 1 else (3, year - 1)


def _weekdays_back(end, count):
    """The last `count` weekdays up to and including `end`, oldest first."""
    days, d = [], end
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def build(path, admin):
    """Create the example database at `path` and fill it.
    `admin` = {"id", "username", "full_name", "password_hash"} of the person
    who is looking at the example. Returns a small dict of counts."""
    for stale in (path, path + "-journal"):
        if os.path.exists(stale):
            os.remove(stale)

    token = db.use_db_path(path)
    try:
        db.init_db()
        counts = _populate(admin, random.Random(2026), date.today())
    finally:
        db.reset_db_path(token)
    return counts


# ---------------------------------------------------------------------------
def _populate(admin, rng, today):
    # ---- the school -------------------------------------------------------
    db.set_school_settings(SCHOOL_NAME, SCHOOL_TAGLINE, "MA",
                           ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    db.set_onboarding_completed(True)

    # Only the classes the example school runs. Deleting a grade removes its
    # subjects and streams with it.
    db.execute("DELETE FROM grades WHERE name NOT IN (?, ?)", GRADES)
    db.execute("DELETE FROM pathways")

    # ---- the school day: 9 lessons of 40 min, tea after 3, lunch after 6 ---
    db.set_day_structure("08:00", 40, 9)
    db.replace_breaks([(3, 20, "Tea break"), (6, 40, "Lunch")])
    db.regenerate_periods()

    grade_id = {g["name"]: g["id"] for g in db.fetch_all("SELECT id, name FROM grades")}
    stream_id = {}
    for grade, stream in STREAMS:
        stream_id[stream] = db.execute(
            "INSERT INTO streams (grade_id, pathway_id, name) VALUES (?, NULL, ?)", (grade_id[grade], stream))

    for room in EXTRA_ROOMS:
        db.execute("INSERT INTO rooms (name, capacity) VALUES (?, 40)", (room,))

    # ---- teachers and what they teach -------------------------------------
    subject_ids = {}   # (grade name, subject name) -> subject id
    for row in db.fetch_all(
            "SELECT s.id, s.name, g.name AS grade FROM subjects s JOIN grades g ON s.grade_id = g.id"):
        subject_ids[(row["grade"], row["name"])] = row["id"]

    for i, (name, subjects) in enumerate(TEACHERS, start=1):
        email = name.lower().replace(" ", ".") + "@mwanga.example"
        tid = db.execute("INSERT INTO teachers (name, email, rank) VALUES (?, ?, ?)", (name, email, i))
        for subject in subjects:
            for grade in GRADES:
                sid = subject_ids.get((grade, subject))
                if sid:
                    db.execute("INSERT INTO teacher_subjects (teacher_id, subject_id) VALUES (?, ?)", (tid, sid))

    # ---- the timetable, built by the real auto-scheduler ------------------
    report = scheduler.generate_timetable(stream_ids=None, overwrite=True)
    scheduled = sum(r["scheduled"] for r in report)
    unfilled = sum(len(r["skipped"]) for r in report)

    # ---- students and guardians -------------------------------------------
    students = _make_students(rng, grade_id, stream_id)

    # ---- exams and marks ---------------------------------------------------
    n_marks = _make_exams(rng, today, students, subject_ids)

    # ---- attendance ---------------------------------------------------------
    n_att = _make_attendance(rng, today, students)

    # ---- fees ---------------------------------------------------------------
    n_pay = _make_fees(rng, today, students, grade_id)

    # ---- the visitor's own login, as an administrator ----------------------
    db.execute(
        "INSERT INTO users (id, username, full_name, password_hash, role, active, created_at) "
        "VALUES (?, ?, ?, ?, 'admin', 1, ?)",
        (admin["id"], admin["username"], admin["full_name"], admin["password_hash"], today.isoformat()))

    return {"teachers": len(TEACHERS), "students": len(students), "lessons": scheduled,
            "unfilled": unfilled, "marks": n_marks, "attendance": n_att, "payments": n_pay}


def _make_students(rng, grade_id, stream_id):
    people, used, adm = [], set(), 0
    for grade, stream in STREAMS:
        for k in range(STUDENTS_PER_STREAM):
            adm += 1
            female = k % 2 == 0
            while True:
                name = f"{rng.choice(FEMALE if female else MALE)} {rng.choice(SURNAMES)}"
                if name not in used:
                    used.add(name)
                    break
            born_year = 2013 if grade == "Grade 7" else 2012
            dob = date(born_year, rng.randint(1, 12), rng.randint(1, 28)).isoformat()
            sid = db.execute(
                "INSERT INTO students (admission_no, name, gender, grade_id, stream_id, dob, admission_date, "
                "status, active) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1)",
                (f"MA/{adm:04d}", name, "F" if female else "M", grade_id[grade], stream_id[stream],
                 dob, f"{date.today().year - 2}-01-06"))
            surname = name.split()[-1]
            parent = rng.choice([("Mrs.", "Mother"), ("Mr.", "Father"), ("Mrs.", "Guardian")])
            db.execute(
                "INSERT INTO guardians (student_id, name, relationship, phone, email, is_primary) "
                "VALUES (?, ?, ?, ?, NULL, 1)",
                (sid, f"{parent[0]} {surname}", parent[1], f"0700000{adm:03d}"))
            people.append({"id": sid, "grade": grade, "stream": stream, "adm": adm,
                           "ability": min(88, max(30, rng.gauss(58, 13)))})
    return people


def _make_exams(rng, today, students, subject_ids):
    term, year = _term_and_year(today)
    prev_term, prev_year = _previous_term(term, year)
    plan = [  # (name, term, year, date, drift in marks)
        (f"End of Term {prev_term}", prev_term, prev_year, today - timedelta(days=120), 0),
        ("Opener Exam", term, year, today - timedelta(days=45), 2),
        ("Mid-Term Exam", term, year, today - timedelta(days=14), 5),
    ]
    total = 0
    for grade in GRADES:
        grade_students = [s for s in students if s["grade"] == grade]
        for name, t, y, when, drift in plan:
            gid = db.fetch_one("SELECT id FROM grades WHERE name=?", (grade,))["id"]
            exam_id = db.execute(
                "INSERT INTO exams (name, term, year, exam_date, grade_id) VALUES (?, ?, ?, ?, ?)",
                (name, t, y, when.isoformat(), gid))
            statements = []
            for subject, offset in EXAM_SUBJECTS.items():
                sid = subject_ids[(grade, subject)]
                statements.append(("INSERT INTO exam_subjects (exam_id, subject_id, out_of) VALUES (?, ?, 100)",
                                   (exam_id, sid)))
                for st in grade_students:
                    if rng.random() < 0.012:      # a few blanks: absent on the day
                        continue
                    score = st["ability"] + offset + drift + rng.gauss(0, 8)
                    score = int(round(min(98, max(8, score))))
                    statements.append((
                        "INSERT INTO marks (exam_id, student_id, subject_id, score) VALUES (?, ?, ?, ?)",
                        (exam_id, st["id"], sid, score)))
                    total += 1
            db.execute_many(statements)
    return total


def _make_attendance(rng, today, students):
    days = _weekdays_back(today, 15)
    statements = []
    for st in students:
        chronic = rng.random() < 0.10          # a few children miss a lot of school
        for day in days:
            r = rng.random()
            absent_p = 0.28 if chronic else 0.03
            if r < absent_p:
                status, note = "A", None
            elif r < absent_p + 0.04:
                status, note = "L", None
            elif r < absent_p + 0.06:
                status, note = "E", "Clinic visit"
            else:
                status, note = "P", None
            statements.append((
                "INSERT INTO attendance (student_id, att_date, status, note, marked_by) VALUES (?, ?, ?, ?, ?)",
                (st["id"], day.isoformat(), status, note, "Class teacher (example)")))
    db.execute_many(statements)
    return len(statements)


def _make_fees(rng, today, students, grade_id):
    term, year = _term_and_year(today)
    db.set_generation_setting("fees_term", str(term))
    db.set_generation_setting("fees_year", str(year))

    for grade, items in FEE_ITEMS.items():
        for name, amount in items:
            db.execute("INSERT INTO fee_items (grade_id, term, year, name, amount) VALUES (?, ?, ?, ?, ?)",
                       (grade_id[grade], term, year, name, float(amount)))

    conn = db.get_connection()
    cur = conn.cursor()
    used_refs, n_pay = set(), 0

    def reference(method):
        while True:
            if method == "M-Pesa":
                ref = "S" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(9))
            else:
                ref = "BNK" + "".join(rng.choice("0123456789") for _ in range(7))
            if ref not in used_refs:
                used_refs.add(ref)
                return ref

    try:
        for st in students:
            lines = FEE_ITEMS[st["grade"]]
            invoice_id = db._insert_and_get_id(
                cur, "INSERT INTO invoices (student_id, term, year, created_on) VALUES (?, ?, ?, ?)",
                (st["id"], term, year, (today - timedelta(days=70)).isoformat()))
            for name, amount in lines:
                cur.execute(db._adapt(
                    "INSERT INTO invoice_lines (invoice_id, description, amount, kind) VALUES (?, ?, ?, 'fee')"),
                    (invoice_id, name, float(amount)))
            total = sum(a for _, a in lines)

            roll = rng.random()
            if roll < 0.40:
                pay_total = total                                    # fees cleared
            elif roll < 0.88:
                pay_total = int(round(total * rng.uniform(0.25, 0.85) / 100) * 100)   # part-paid
            else:
                pay_total = 0                                        # nothing paid yet

            amounts = []
            if pay_total:
                if rng.random() < 0.5:                               # paid in two instalments
                    first = int(round(pay_total * rng.uniform(0.4, 0.6) / 100) * 100)
                    amounts = [first, pay_total - first]
                else:
                    amounts = [pay_total]
            for amount in amounts:
                if amount <= 0:
                    continue
                method = rng.choices(["M-Pesa", "Cash", "Bank"], weights=[60, 20, 20])[0]
                when = today - timedelta(days=rng.randint(1, 65))
                fees._insert_payment(cur, st["id"], amount, method,
                                     reference(method) if method != "Cash" else None,
                                     when.isoformat(), "Bursar (example)")
                n_pay += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n_pay
