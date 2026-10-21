# ShuleHub — Timetable · Exam Analysis · School Management

One app, three purposes. Everyone signs in; what they see depends on their role.

| Role | Can do |
|---|---|
| **Administrator** | Everything, including staff logins, promotion and school settings |
| **Teacher** | View/export timetables, exam marks & analysis, take attendance, read student records |
| **Bursar** | Fees, payments, M-Pesa import, receipts; read student records |

## School Management module

- **Students & Guardians** — admissions, profiles, up to many guardians per student, CSV import/export, enrolment status (active / graduated / transferred / withdrawn). Students are never deleted from here, so fee and exam history stay intact.
- **Promotion** — move a class up at year end (promote, repeat, graduate, transfer, withdraw), with stream mapping. Old exam results and merit lists are preserved.
- **Attendance** — daily class register (present / late / absent / excused), school-wide overview, absentee list with guardian phone numbers, per-class reports and CSV.
- **Fees & Payments** — fee items per grade per term, one-click invoices, extra charges and discounts/bursaries, payments (cash, M-Pesa, bank, cheque), PDF receipts and statements, balances and collections registers with CSV, and **M-Pesa Paybill statement import** (matches the account number to the admission number; each transaction code is recorded once, so overlapping statements are safe). Payments are never deleted, only voided with a reason.
- **Staff & Logins** — create and manage logins and roles.

## ShuleHub desktop app: name, icon and example mode

**Name.** The app is called **ShuleHub** (the offline Windows build is `ShuleHub.exe`; data lives in `~/ShuleHubData`,
and an older `~/GTimetableData` folder is moved there automatically on first start so nobody loses their records).
Passkeys keep their `GT-` prefix so every key already issued stays valid.

**Icon.** `assets/shulehub.ico` (a graduation cap over a row of timetable cells) is set as the exe's icon in
`ShuleHub.spec`. The artwork is drawn by `assets/make_icon.py`; edit it and run `python assets/make_icon.py` to
regenerate the `.ico` and the web/PWA icons in `static/icons/`.

**Build (on Windows):** `build_windows.bat` -> `dist\ShuleHub.exe`. Windows caches exe icons, so if an old icon still
shows, rename the file or restart Explorer.

**Example mode ("See an Example").** Menu -> *See an Example* (or the card on the home page) opens a pretend school,
Mwanga Academy: 8 teachers, 3 classes with a finished timetable, 42 students with guardians, 3 exams per grade,
3 weeks of attendance and a term of fee invoices and payments. It runs on a **separate throw-away database**
(a private `demo-<id>.db` file for each example, built fresh each time and deleted on exit), so the school's real records are never read or changed. A banner shows on
every page while it is on; *Exit example* discards it. See `demo.py` (switching) and `demo_data.py` (the sample data).
Desktop/SQLite only: the Render (Postgres) web deployment hides the option.

### First run / upgrading
Existing databases upgrade automatically on start (new tables and columns are added; nothing is removed). The first time the app opens with no logins, it asks you to create the administrator. **On a public deployment do this immediately**, or set a `SETUP_TOKEN` environment variable so the setup page also asks for that code. Set `SECRET_KEY` (render.yaml already does) so sessions survive restarts.

### Notes
- Live M-Pesa (Daraja STK push / automatic C2B callbacks) is not included; it needs your Safaricom credentials. The statement import covers reconciliation without them.
- The offline cache now stores only the timetable view and the guide. Student, attendance and fee pages are never cached on the device, and the cache is cleared at logout.

---

# ShuleHub — Timetable + Exam Analysis

One app, two modules:

* **Timetable** — grades, streams, subjects, teachers, rooms, auto-scheduler, drag-and-drop builder, PDF/CSV export.
* **Exam Analysis** (`analytics.py`, `exam_analysis.py`) — students, exams, marks entry, merit lists, subject / stream / teacher analysis, exam comparison, report forms, editable grading scales.

## Classes supported

| Level | Grades | `curriculum` code |
|---|---|---|
| Lower Primary | Grade 1–3 | `CBC-LP` |
| Junior Secondary | Grade 7–9 | `CBC-JS` |
| Senior School (pathways) | Grade 10 | `CBC` |
| Legacy 8-4-4 | Form 3–4 | `8-4-4` |
| Pre-Primary / Upper Primary | add from *Grades & Streams* | `PP` / `CBC-UP` |

Default grades and their CBC subjects are added **once** to an existing database on first start of this version
(tracked by the `levels_seeded_v1` flag, so deleting a grade is permanent). No streams are created for them —
add the streams you actually run. Standard subjects and weekly periods live in `curriculum.py`.

## Exam Analysis: how results are computed

* Each mark → % of the subject's "out of" → grade band (`grading_bands` table; defaults in `curriculum.py`).
* Total = sum of marks for subjects sat; **Mean % = total ÷ sum of "out of" for those subjects**. A blank mark means *not taking / absent*.
* **Position is ranked on Mean %** (equal means share a position), overall and within stream.
* Subject × stream analysis and teacher performance use the teacher assigned in the **timetable** for that stream and subject.
* Upgrades are automatic: new tables are created with `CREATE TABLE IF NOT EXISTS`; `grades.sort_order` is added by `_migrate_schema`.

## Known limitations

* **Day structure per level.** *Day Structure* now holds several profiles (start time, lesson length, lessons per day, breaks); each grade uses one (`grades.day_profile_id`, empty = Default). Code in `day_profiles.py`. Clash checks compare real clock times, so a teacher cannot be in Grade 3 and Form 1 at overlapping times. The auto-scheduler (`scheduler.py`) must pick periods by the class's profile — see `day_profiles.profile_id_for_stream()`.
* Exam results are recomputed on every page load (fine for a school's volume; a student's history page runs one calculation per exam).

---

# ShuleHub — Onboarding Wizard

A one-time setup flow for brand-new schools: language choice, teachers,
leave policy, and scheduling rules — plus a welcome screen with a read-only
demo and tutorials. Fires **once per school**, not once per user, so
existing schools are never interrupted or reset.

## What's in this folder

```
migrations/0001_add_onboarding_fields.sql   Adds onboarding columns to your School table
onboarding/__init__.py                       Blueprint definition
onboarding/routes.py                         Wizard routes (welcome → teachers → leave → rules → complete)
onboarding/demo_data.py                      Static demo dataset (never touches real DB rows)
templates/onboarding/_base.html              Shared layout + progress rail
templates/onboarding/welcome.html            Salutation, language picker, demo/tutorial links
templates/onboarding/teachers.html           Step 1
templates/onboarding/leave.html              Step 2
templates/onboarding/rules.html              Step 3 (onboarding-scoped, separate from your existing rules.html)
templates/onboarding/complete.html           Step 4
templates/onboarding/demo.html               Read-only demo timetable
static/css/onboarding.css                    Design system for all of the above
static/js/onboarding.js                      Row add/remove + smooth scroll
```

## 1. Run the migration

In the Render console (or via `psql`) against your production database:

```bash
psql $DATABASE_URL -f migrations/0001_add_onboarding_fields.sql
```

This adds `language`, `onboarding_completed`, `onboarding_step`,
`onboarding_started_at`, `onboarding_completed_at` to your `schools` table,
and marks any school that already has teachers as already onboarded — so
nobody currently using the app gets dropped into the wizard.

## 2. Fix the three imports in `onboarding/routes.py`

```python
from app import db
from models import School, Teacher, LeaveType
```

Point these at wherever your `db = SQLAlchemy()` instance and your actual
model classes live. If your models use different field names (e.g.
`full_name` instead of `name`), adjust the `Teacher(...)` / `LeaveType(...)`
constructor calls in `routes.py` to match.

## 3. Register the blueprint

In your main app file:

```python
from onboarding import onboarding_bp
app.register_blueprint(onboarding_bp)
```

## 4. Guard your existing routes

Add this to your app so any school that hasn't finished setup gets sent to
the wizard instead of the real dashboard — this is what makes it a
"new person starts with a clean slate" experience:

```python
ONBOARDING_ENDPOINTS = {"onboarding.welcome", "onboarding.teachers",
                         "onboarding.leave", "onboarding.rules",
                         "onboarding.complete", "onboarding.demo",
                         "static", "login", "logout"}

@app.before_request
def enforce_onboarding():
    if current_user.is_authenticated:
        school = current_user.school
        if not school.onboarding_completed and request.endpoint not in ONBOARDING_ENDPOINTS:
            return redirect(url_for("onboarding.welcome"))
```

## 5. Make sure new schools start clean

Wherever you currently create a `School` row (signup, admin-creates-school,
etc.), do not copy any teachers/leave types/rules from another school or a
template. `onboarding_completed` defaults to `False`, which is all that's
needed — the wizard and the before_request guard do the rest.

```python
school = School(name=request.form["name"])  # onboarding_completed defaults False
db.session.add(school)
db.session.commit()
return redirect(url_for("onboarding.welcome"))
```

## 6. Swap the dashboard redirect

In `onboarding/routes.py`, the `complete()` view redirects to
`url_for("dashboard")` — change `"dashboard"` to whatever your real
post-setup landing endpoint is named.

## Design notes

The setup rail on the left is a small timetable grid — each setup step is a
"period" (P1, P2, P3...) that lights up as it's completed, echoing the
product's own core object instead of a generic progress bar. Colors and
type (Zilla Slab / Inter / IBM Plex Mono) live entirely in
`onboarding.css` as CSS variables, so re-theming later is a one-file change.

## Notes on the demo

`/onboarding/demo` is intentionally **not authenticated** and reads only
from `onboarding/demo_data.py` — a plain Python file, not the database. A
visitor can open it, look around, and close it without any risk of writing
to or reading a real school's data.

## Logo, roles and access

**School logo** — School Settings → *School logo*. Upload a PNG/JPG (resized automatically). It replaces the
badge in the header and sign-in page and is printed at the top of timetable, merit list, report form, receipt
and fee statement PDFs. It is stored in the database, so it is included in any backup of the database.

**Roles & Access** — Staff & Logins → *Roles & Access* (administrators only).
Each person has one role; each role is a set of services the administrator ticks:
timetable viewing, timetable editing, exam analysis, exam administration, viewing students,
managing students, attendance, fees, plus two for the exam workflow below. Create roles such as *Deputy Principal* or *Secretary*, then
choose the role for each person on the Staff & Logins page. Changes apply on the person's next click.
Administrator is always full access; Settings, Setup Wizard, Staff & Logins and Roles & Access
stay administrator-only. *Teacher* and *Bursar* start with the access they always had and can be edited.

A new page that isn't listed in `permissions.py` is administrator-only until it is mapped to a service.

## Importing marks and students

Import forms accept CSV, Excel (`.xlsx`) and PDF files with a real text table (not a scan).

## Teachers enter their own marks; the office publishes

**How an exam flows**
1. **Administrator** creates the exam (Exam Analysis → Exams) as before.
2. **Allocate subjects** — Compile & Publish → *Subject allocations*: say who teaches which subject in
   which class (or press *Fill from timetable*). Each teacher's login must be linked to their teacher
   record on Staff & Logins.
3. **Teachers** open **My Marks**. They see only the subject + class sheets allocated to them, type or
   upload marks (CSV, Excel or a PDF table), *Save draft* as often as needed, then *Submit final marks*,
   which locks the sheet. Blank = did not sit.
4. **Compile & Publish** shows every sheet's status, teacher and progress. Totals, means, grades and
   positions are compiled automatically from whatever has been submitted — there is nothing to run. The
   publisher can preview the results, *Reopen* a sheet, and finally **Publish**. Publishing locks all marks
   and releases the Overview and Merit list to teachers (**Published Results**). *Unpublish* reverses it.

Until an exam is published, its results are visible only to people with **Exam analysis** or
**Compile & publish results**. Teachers never see other teachers' subjects, the teacher-performance
analysis or report-form PDFs.

**Services** (Roles & Access): *Enter marks for my subjects* (subject teachers),
*Compile & publish results* (give it to a Deputy Principal / Exams Officer — the administrator always has it),
*Exam analysis (everything)* (full access: any subject, any time, for e.g. a head of department).
On upgrade, a Teacher role that was never customised is switched once from *Exam analysis* to
*Enter marks for my subjects*; edit it on Roles & Access if you want otherwise.
