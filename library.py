"""
library.py — School Library: catalogue, issue / return, fines, reports, labels.

    /library/                    desk: numbers, overdue list, quick issue / return
    /library/catalogue           search the catalogue (title, author, ISBN, category, shelf)
    /library/books/new           add a book (and its copies)
    /library/books/<id>          one book: edit, copies, availability, loan history
    /library/books/import        bulk-add books from a CSV / Excel file
    /library/circulation         issue, return and renew (barcode-scanner friendly)
    /library/loans               every loan, filterable (+ CSV)
    /library/borrower/<s|t>/<id> one student / teacher: books held, history, fines, clearance
    /library/fines               fines owed: mark paid or waive
    /library/reports             most borrowed, readers, by class; overdue / inventory / clearance exports
    /library/labels.pdf          printable accession-number labels with barcodes
    /library/settings            loan periods, limits, fines, accession prefix

How it works
------------
* A BOOK is a title. A COPY is one physical book with its own accession number
  (the number written/stuck inside the book). Several copies share one title.
* A LOAN is one copy lent to one student or teacher. A copy can only have one
  open loan at a time (a database rule, so two desks can't lend the same book).
* Whether a copy is "on loan" comes from its open loan, not from a flag, so
  it can never get out of step. A copy's own status is only available /
  lost / damaged / withdrawn.
* Fines: overdue days (after the grace days) × the daily rate, capped at the
  book's price if it has one. A lost book is charged at the book's price (or
  the default replacement fee). Fines are recorded on the loan when the book
  comes back and are marked paid or waived (with a reason) on the Fines page.
  They are kept separate from school fees on purpose.
* Barcode scanners type the accession number and press Enter, so the issue
  and return boxes work with a scanner or with the keyboard.

Access: every signed-in staff member can search the catalogue (the two endpoints are
listed in auth.COMMON); everything else needs the "library" service (see permissions.py).
"""

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

from flask import (
    Blueprint, Response, flash, g, jsonify, redirect, render_template, request, url_for,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import auth
import branding
import database as db
import import_files
from analytics import _csv_safe, _find_column, _norm, _safe_filename

library_bp = Blueprint("library", __name__, url_prefix="/library")

# lib_settings has no id column, so the database layer must not append RETURNING id to its inserts.
_no_id = getattr(db, "TABLES_WITHOUT_ID", None)
if _no_id is not None:
    _no_id.add("lib_settings")

PAGE_SIZE = 40
LOANS_PAGE_SIZE = 100
MAX_COPIES_AT_ONCE = 500
MAX_IMPORT_ROWS = 5000
COPY_STATUSES = ("available", "damaged", "lost", "withdrawn")
STATUS_LABELS = {"available": "Available", "damaged": "Damaged", "lost": "Lost", "withdrawn": "Withdrawn"}
ACCESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,29}$")

# ---------------------------------------------------------------- schema
_DDL = """
CREATE TABLE IF NOT EXISTS lib_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS lib_books (
    id {PK},
    title TEXT NOT NULL,
    author TEXT,
    isbn TEXT,
    publisher TEXT,
    year TEXT,
    edition TEXT,
    category TEXT,
    subject TEXT,
    shelf TEXT,
    price {REAL},
    notes TEXT,
    created_on TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lib_copies (
    id {PK},
    book_id INTEGER NOT NULL REFERENCES lib_books(id) ON DELETE CASCADE,
    accession_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'available',
    notes TEXT,
    added_on TEXT
);

CREATE TABLE IF NOT EXISTS lib_loans (
    id {PK},
    copy_id INTEGER NOT NULL REFERENCES lib_copies(id) ON DELETE CASCADE,
    borrower_type TEXT NOT NULL,
    student_id INTEGER REFERENCES students(id) ON DELETE SET NULL,
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    borrower_name TEXT,
    borrower_ref TEXT,
    issued_on TEXT NOT NULL,
    due_on TEXT NOT NULL,
    returned_on TEXT,
    renewals INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    fine_amount {REAL} NOT NULL DEFAULT 0,
    fine_status TEXT NOT NULL DEFAULT 'none',
    fine_note TEXT,
    fine_settled_on TEXT,
    issued_by TEXT,
    returned_to TEXT
);

CREATE INDEX IF NOT EXISTS ix_lib_copies_book ON lib_copies(book_id);
CREATE INDEX IF NOT EXISTS ix_lib_loans_copy ON lib_loans(copy_id);
CREATE INDEX IF NOT EXISTS ix_lib_loans_student ON lib_loans(student_id);
CREATE INDEX IF NOT EXISTS ix_lib_loans_teacher ON lib_loans(teacher_id);
CREATE INDEX IF NOT EXISTS ix_lib_loans_due ON lib_loans(due_on);
CREATE UNIQUE INDEX IF NOT EXISTS ux_lib_loans_open_copy ON lib_loans(copy_id) WHERE returned_on IS NULL;
"""

_ready = False


def ensure_schema():
    """Create the library tables if they're missing. Idempotent, so existing
    databases (SQLite or Postgres) upgrade in place and nothing is lost."""
    global _ready
    if _ready:
        return
    conn = db.get_connection()
    try:
        ddl = (_DDL.replace("{PK}", "SERIAL PRIMARY KEY" if db.USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT")
                   .replace("{REAL}", "DOUBLE PRECISION" if db.USE_POSTGRES else "REAL"))
        if db.USE_POSTGRES:
            conn.cursor().execute(ddl)
        else:
            conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    _ready = True


@library_bp.before_request
def _before():
    ensure_schema()


# ---------------------------------------------------------------- settings
SETTING_DEFAULTS = {
    "accession_prefix": "LIB-",
    "next_accession": "1",
    "loan_days_student": "14",
    "loan_days_teacher": "30",
    "max_loans_student": "3",
    "max_loans_teacher": "5",
    "max_renewals": "1",
    "fine_per_day": "5",
    "fine_grace_days": "0",
    "fine_cap_to_price": "1",
    "lost_fee_default": "500",
    "block_when_overdue": "1",
    "block_when_fines": "1",
}
_INT_SETTINGS = ("next_accession", "loan_days_student", "loan_days_teacher", "max_loans_student",
                 "max_loans_teacher", "max_renewals", "fine_grace_days")
_FLOAT_SETTINGS = ("fine_per_day", "lost_fee_default")
_FLAG_SETTINGS = ("fine_cap_to_price", "block_when_overdue", "block_when_fines")


def get_settings():
    raw = {r["key"]: r["value"] for r in db.fetch_all("SELECT key, value FROM lib_settings")}
    s = {}
    for key, default in SETTING_DEFAULTS.items():
        val = raw.get(key)
        val = default if val is None else val
        if key in _INT_SETTINGS:
            try:
                val = int(val)
            except (TypeError, ValueError):
                val = int(default)
        elif key in _FLOAT_SETTINGS:
            try:
                val = float(val)
            except (TypeError, ValueError):
                val = float(default)
        elif key in _FLAG_SETTINGS:
            val = str(val) == "1"
        s[key] = val
    return s


def _put_setting(key, value):
    db.execute("INSERT INTO lib_settings (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ---------------------------------------------------------------- small helpers
@library_bp.app_template_filter("libmoney")
def _libmoney(v):
    v = round(float(v or 0), 2)
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


@library_bp.app_template_filter("libdate")
def _libdate(value):
    if not value:
        return "—"
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return str(value)


def _today():
    return date.today().isoformat()


def _who():
    return g.user["username"] if getattr(g, "user", None) else None


def _clean(text, limit=300):
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def _parse_date(value):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _num(value, default=None):
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text != "" else default
    except (TypeError, ValueError):
        return default


def _like(text):
    """Pattern for a case-insensitive contains search (use with LOWER(col) LIKE ?)."""
    return "%" + re.sub(r"([%_\\])", r"\\\1", (text or "").strip().lower()) + "%"


def _page(total, size):
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    pages = max((total + size - 1) // size, 1)
    page = min(page, pages)
    return page, pages, (page - 1) * size


def _qs(**changes):
    """Current query string with some values replaced (for paging links)."""
    args = {k: v for k, v in request.args.items() if v != ""}
    for k, v in changes.items():
        if v in (None, ""):
            args.pop(k, None)
        else:
            args[k] = v
    return args


def _clamp_int(value, low, high, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


# ---------------------------------------------------------------- fines
def calc_fine(due_on, until, price, s):
    """(fine amount, days late) for a loan due on `due_on`, judged on `until`."""
    try:
        late = (date.fromisoformat(until) - date.fromisoformat(due_on)).days
    except (TypeError, ValueError):
        return 0.0, 0
    late = max(late, 0)
    chargeable = late - s["fine_grace_days"]
    if chargeable <= 0 or s["fine_per_day"] <= 0:
        return 0.0, late
    amount = chargeable * s["fine_per_day"]
    if s["fine_cap_to_price"] and price:
        amount = min(amount, float(price))
    return round(amount, 2), late


# ---------------------------------------------------------------- loan queries
_LOAN_SELECT = """
SELECT l.id, l.copy_id, l.borrower_type, l.student_id, l.teacher_id, l.issued_on, l.due_on,
       l.returned_on, l.renewals, l.outcome, l.fine_amount, l.fine_status, l.fine_note,
       l.fine_settled_on, l.issued_by, l.returned_to,
       c.accession_no, c.book_id, b.title, b.author, b.price,
       COALESCE(st.name, t.name, l.borrower_name, 'Unknown') AS who,
       COALESCE(st.admission_no, l.borrower_ref, '') AS ref,
       COALESCE(sm.name, g.name, '') AS class_name
FROM lib_loans l
JOIN lib_copies c ON l.copy_id = c.id
JOIN lib_books b ON c.book_id = b.id
LEFT JOIN students st ON l.student_id = st.id
LEFT JOIN teachers t ON l.teacher_id = t.id
LEFT JOIN grades g ON st.grade_id = g.id
LEFT JOIN streams sm ON st.stream_id = sm.id
"""


def _decorate(rows, s=None):
    """Plain dicts with is_overdue / days_late / fine_now added (open loans accrue on the fly)."""
    s = s or get_settings()
    today = _today()
    out = []
    for r in rows:
        d = dict(r)
        d["is_open"] = d["returned_on"] is None
        if d["is_open"]:
            fine, late = calc_fine(d["due_on"], today, d["price"], s)
            d["days_late"], d["fine_now"] = late, fine
            d["is_overdue"] = late > 0
        else:
            d["days_late"] = d["fine_now"] = 0
            d["is_overdue"] = False
        out.append(d)
    return out


def open_loans(extra_where="", params=(), order="l.due_on, l.id"):
    return _decorate(db.fetch_all(
        _LOAN_SELECT + " WHERE l.returned_on IS NULL " + extra_where + " ORDER BY " + order, params))


def borrower_loans(kind, borrower_id, open_only=False):
    col = "l.student_id" if kind == "s" else "l.teacher_id"
    where = f"WHERE {col} = ?" + (" AND l.returned_on IS NULL" if open_only else "")
    return _decorate(db.fetch_all(
        _LOAN_SELECT + f" {where} ORDER BY (l.returned_on IS NULL) DESC, l.issued_on DESC, l.id DESC",
        (borrower_id,)))


def _guardians(student_ids):
    """{student_id: (guardian name, phone)} using the primary guardian, else the first."""
    ids = sorted({i for i in student_ids if i})
    out = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for r in db.fetch_all(
                f"SELECT student_id, name, phone FROM guardians WHERE student_id IN ({marks}) "
                f"ORDER BY is_primary DESC, id", tuple(chunk)):
            out.setdefault(r["student_id"], (r["name"] or "", r["phone"] or ""))
    return out


# ---------------------------------------------------------------- borrowers
def parse_borrower(key):
    """'s:12' -> ('s', 12); 't:5' -> ('t', 5); anything else -> None."""
    m = re.fullmatch(r"([st]):(\d+)", (key or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def get_borrower(kind, borrower_id):
    """Dict with key, kind, name, ref, sub — or None."""
    if kind == "s":
        r = db.fetch_one(
            "SELECT st.id, st.name, st.admission_no, st.active, COALESCE(sm.name, g.name, '') AS cls "
            "FROM students st LEFT JOIN grades g ON st.grade_id = g.id "
            "LEFT JOIN streams sm ON st.stream_id = sm.id WHERE st.id = ?", (borrower_id,))
        if not r:
            return None
        return {"key": f"s:{r['id']}", "kind": "s", "id": r["id"], "name": r["name"],
                "ref": r["admission_no"], "sub": r["cls"], "type_label": "Student", "active": bool(r["active"])}
    r = db.fetch_one("SELECT id, name FROM teachers WHERE id = ?", (borrower_id,))
    if not r:
        return None
    return {"key": f"t:{r['id']}", "kind": "t", "id": r["id"], "name": r["name"], "ref": "",
            "sub": "Teacher", "type_label": "Teacher", "active": True}


def find_borrowers(q, limit=12):
    q = _clean(q, 80)
    if not q:
        return []
    pat = _like(q)
    out = []
    for r in db.fetch_all(
            "SELECT st.id, st.name, st.admission_no, COALESCE(sm.name, g.name, '') AS cls "
            "FROM students st LEFT JOIN grades g ON st.grade_id = g.id "
            "LEFT JOIN streams sm ON st.stream_id = sm.id "
            "WHERE st.active = 1 AND (LOWER(st.name) LIKE ? ESCAPE '\\' OR LOWER(st.admission_no) LIKE ? ESCAPE '\\') "
            "ORDER BY st.name LIMIT ?", (pat, pat, limit)):
        out.append({"key": f"s:{r['id']}", "name": r["name"], "ref": r["admission_no"], "sub": r["cls"],
                    "type_label": "Student"})
    room = limit - len(out)
    if room > 0:
        for r in db.fetch_all(
                "SELECT id, name FROM teachers WHERE LOWER(name) LIKE ? ESCAPE '\\' ORDER BY name LIMIT ?",
                (pat, room)):
            out.append({"key": f"t:{r['id']}", "name": r["name"], "ref": "", "sub": "Teacher",
                        "type_label": "Teacher"})
    return out


# ---------------------------------------------------------------- copies and books
_BOOK_STATS = """
(SELECT COUNT(*) FROM lib_copies c WHERE c.book_id = b.id AND c.status <> 'withdrawn') AS copies,
(SELECT COUNT(*) FROM lib_copies c WHERE c.book_id = b.id AND c.status = 'available'
   AND NOT EXISTS (SELECT 1 FROM lib_loans l WHERE l.copy_id = c.id AND l.returned_on IS NULL)) AS available,
(SELECT COUNT(*) FROM lib_loans l JOIN lib_copies c ON l.copy_id = c.id
   WHERE c.book_id = b.id AND l.returned_on IS NULL) AS on_loan
"""


def find_copy(accession):
    """The copy with this accession number (exact, case-insensitive), with its book."""
    acc = _clean(accession, 40)
    if not acc:
        return None
    return db.fetch_one(
        "SELECT c.id, c.book_id, c.accession_no, c.status, b.title, b.author, b.price "
        "FROM lib_copies c JOIN lib_books b ON c.book_id = b.id WHERE LOWER(c.accession_no) = ?",
        (acc.lower(),))


def copy_open_loan(copy_id):
    return db.fetch_one("SELECT id, due_on FROM lib_loans WHERE copy_id = ? AND returned_on IS NULL", (copy_id,))


def next_accessions(n):
    """n fresh accession numbers (prefix + running number), skipping any already used."""
    s = get_settings()
    prefix, counter = s["accession_prefix"], s["next_accession"]
    out = []
    while len(out) < n:
        acc = f"{prefix}{counter:05d}"
        counter += 1
        if not db.fetch_one("SELECT 1 AS x FROM lib_copies WHERE LOWER(accession_no) = ?", (acc.lower(),)):
            out.append(acc)
    _put_setting("next_accession", counter)
    return out


def add_copies(book_id, count=1, custom=None):
    """Create copies for a book. `custom` is a list of accession numbers to use as they are.
    Returns (created accession numbers, [problems])."""
    problems, created = [], []
    today = _today()
    accs = []
    if custom:
        for raw in custom:
            acc = _clean(raw, 30)
            if not acc:
                continue
            if not ACCESSION_RE.match(acc):
                problems.append(f"'{acc}' isn't a valid accession number (letters, numbers, . _ / - only).")
            elif find_copy(acc):
                problems.append(f"{acc} already exists.")
            elif acc.lower() in {a.lower() for a in accs}:
                problems.append(f"{acc} was typed twice.")
            else:
                accs.append(acc)
    else:
        accs = next_accessions(count)
    for acc in accs:
        try:
            db.execute("INSERT INTO lib_copies (book_id, accession_no, status, added_on) VALUES (?, ?, 'available', ?)",
                       (book_id, acc, today))
            created.append(acc)
        except Exception:   # a duplicate sneaked in between the check and the insert
            problems.append(f"{acc} could not be added (already exists).")
    return created, problems


def _book_fields(form):
    """(fields dict, error) from the add / edit book form."""
    title = _clean(form.get("title"), 250)
    if not title:
        return None, "Enter the book's title."
    price = _num(form.get("price"))
    if price is not None and not 0 <= price <= 1_000_000:
        return None, "The price must be a positive amount."
    return {
        "title": title, "author": _clean(form.get("author"), 200), "isbn": _clean(form.get("isbn"), 30),
        "publisher": _clean(form.get("publisher"), 150), "year": _clean(form.get("year"), 10),
        "edition": _clean(form.get("edition"), 40), "category": _clean(form.get("category"), 80),
        "subject": _clean(form.get("subject"), 80), "shelf": _clean(form.get("shelf"), 40),
        "price": price, "notes": _clean(form.get("notes"), 600),
    }, None


def _categories():
    return [r["category"] for r in db.fetch_all(
        "SELECT DISTINCT category FROM lib_books WHERE active = 1 AND category IS NOT NULL AND category <> '' "
        "ORDER BY category")]


# ---------------------------------------------------------------- circulation logic
def issue_copy(copy, kind, borrower_id, due_on=None, override=False):
    """Lend a copy. Returns (ok, message). `copy` comes from find_copy()."""
    s = get_settings()
    if copy["status"] != "available":
        return False, f"{copy['accession_no']} ({copy['title']}) is marked {STATUS_LABELS.get(copy['status'], copy['status']).lower()} and can't be lent."
    if copy_open_loan(copy["id"]):
        return False, f"{copy['accession_no']} ({copy['title']}) is already on loan. Return it first."
    b = get_borrower(kind, borrower_id)
    if not b:
        return False, "That borrower wasn't found."
    if not b["active"]:
        return False, f"{b['name']} is no longer an active student."

    mine = borrower_loans(kind, borrower_id, open_only=True)
    limit = s["max_loans_student"] if kind == "s" else s["max_loans_teacher"]
    if not override:
        if len(mine) >= limit:
            return False, (f"{b['name']} already has {len(mine)} book{'s' if len(mine) != 1 else ''} "
                           f"(the limit is {limit}). Tick “Allow anyway” to lend another.")
        if s["block_when_overdue"] and any(m["is_overdue"] for m in mine):
            return False, f"{b['name']} has an overdue book. Return it first, or tick “Allow anyway”."
        if s["block_when_fines"]:
            owed = db.fetch_one(
                "SELECT COALESCE(SUM(fine_amount), 0) AS t FROM lib_loans WHERE fine_status = 'unpaid' AND "
                + ("student_id" if kind == "s" else "teacher_id") + " = ?", (borrower_id,))["t"]
            if owed and owed > 0:
                return False, (f"{b['name']} owes a library fine of KES {_libmoney(owed)}. "
                               f"Clear it on the Fines page, or tick “Allow anyway”.")

    today = date.today()
    if due_on:
        due = _parse_date(due_on)
        if not due or due < today:
            return False, "The due date must be today or later."
    else:
        due = today + timedelta(days=s["loan_days_student"] if kind == "s" else s["loan_days_teacher"])
    try:
        db.execute(
            "INSERT INTO lib_loans (copy_id, borrower_type, student_id, teacher_id, borrower_name, borrower_ref, "
            "issued_on, due_on, issued_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (copy["id"], "student" if kind == "s" else "teacher",
             borrower_id if kind == "s" else None, borrower_id if kind == "t" else None,
             b["name"], b["ref"], today.isoformat(), due.isoformat(), _who()))
    except Exception:
        return False, f"{copy['accession_no']} was just lent out from another screen."
    return True, f"Issued {copy['accession_no']} “{copy['title']}” to {b['name']} — due {_libdate(due.isoformat())}."


def return_copy(accession, outcome="returned", fine_override=None, note=""):
    """Take a copy back. Returns (ok, message)."""
    copy = find_copy(accession)
    if not copy:
        return False, f"No book has the accession number “{_clean(accession, 40)}”."
    loan = db.fetch_one(
        _LOAN_SELECT + " WHERE l.copy_id = ? AND l.returned_on IS NULL", (copy["id"],))
    if not loan:
        return False, f"{copy['accession_no']} ({copy['title']}) isn't on loan."
    s = get_settings()
    today = _today()
    overdue_fine, late = calc_fine(loan["due_on"], today, loan["price"], s)
    if outcome == "lost":
        price = loan["price"] or 0
        fine = round(price if price > 0 else s["lost_fee_default"], 2)
    else:
        fine = overdue_fine
    if fine_override is not None:
        fine = round(max(fine_override, 0), 2)
    status = "unpaid" if fine > 0 else "none"
    updated = db.execute(
        "UPDATE lib_loans SET returned_on = ?, outcome = ?, fine_amount = ?, fine_status = ?, fine_note = ?, "
        "returned_to = ? WHERE id = ? AND returned_on IS NULL",
        (today, outcome if outcome in ("lost", "damaged") else "returned", fine, status,
         _clean(note, 300) or None, _who(), loan["id"]))
    if outcome in ("lost", "damaged"):
        db.execute("UPDATE lib_copies SET status = ? WHERE id = ?", (outcome, copy["id"]))
    who = loan["who"]
    msg = f"{copy['accession_no']} “{copy['title']}” returned by {who}"
    if outcome == "lost":
        msg = f"{copy['accession_no']} “{copy['title']}” marked lost by {who}"
    elif outcome == "damaged":
        msg = f"{copy['accession_no']} “{copy['title']}” returned damaged by {who}"
    if late:
        msg += f" ({late} day{'s' if late != 1 else ''} late)"
    if fine > 0:
        msg += f" — fine KES {_libmoney(fine)}"
    return True, msg + "."


def renew_loan(loan_id):
    s = get_settings()
    loan = db.fetch_one(_LOAN_SELECT + " WHERE l.id = ?", (loan_id,))
    if not loan or loan["returned_on"]:
        return False, "That loan is already closed."
    if loan["renewals"] >= s["max_renewals"]:
        return False, f"This book has already been renewed {loan['renewals']} time(s) (the limit is {s['max_renewals']})."
    if loan["due_on"] < _today():
        return False, "An overdue book can't be renewed. Return it first."
    days = s["loan_days_student"] if loan["borrower_type"] == "student" else s["loan_days_teacher"]
    new_due = (date.today() + timedelta(days=days)).isoformat()
    if new_due <= loan["due_on"]:
        new_due = (date.fromisoformat(loan["due_on"]) + timedelta(days=days)).isoformat()
    db.execute("UPDATE lib_loans SET due_on = ?, renewals = renewals + 1 WHERE id = ? AND returned_on IS NULL",
               (new_due, loan_id))
    return True, f"“{loan['title']}” renewed — now due {_libdate(new_due)}."


# ================================================================ PAGES
def _safe_next(default):
    """Where to go after a POST: only ever back to a /library page."""
    nxt = request.form.get("next", "")
    return nxt if nxt.startswith("/library") and "//" not in nxt and "\\" not in nxt else default


def _can_see_borrowers():
    return auth.has_service(g.user, "library")


# ---------------------------------------------------------------- desk (dashboard)
@library_bp.route("/")
def index():
    s = get_settings()
    today = _today()
    soon = (date.today() + timedelta(days=3)).isoformat()
    n = lambda sql, p=(): db.fetch_one(sql, p)["n"]   # noqa: E731
    stats = {
        "titles": n("SELECT COUNT(*) AS n FROM lib_books WHERE active = 1"),
        "copies": n("SELECT COUNT(*) AS n FROM lib_copies WHERE status <> 'withdrawn'"),
        "on_loan": n("SELECT COUNT(*) AS n FROM lib_loans WHERE returned_on IS NULL"),
        "overdue": n("SELECT COUNT(*) AS n FROM lib_loans WHERE returned_on IS NULL AND due_on < ?", (today,)),
        "lost_damaged": n("SELECT COUNT(*) AS n FROM lib_copies WHERE status IN ('lost', 'damaged')"),
    }
    stats["available"] = n(
        "SELECT COUNT(*) AS n FROM lib_copies c WHERE c.status = 'available' AND NOT EXISTS "
        "(SELECT 1 FROM lib_loans l WHERE l.copy_id = c.id AND l.returned_on IS NULL)")
    owed = db.fetch_one("SELECT COALESCE(SUM(fine_amount), 0) AS t FROM lib_loans WHERE fine_status = 'unpaid'")["t"]
    overdue = open_loans("AND l.due_on < ?", (today,))
    due_soon = open_loans("AND l.due_on >= ? AND l.due_on <= ?", (today, soon))
    recent = _decorate(db.fetch_all(_LOAN_SELECT + " ORDER BY l.id DESC LIMIT 10"), s)
    return render_template("library/index.html", stats=stats, owed=owed, overdue=overdue[:10],
                           n_overdue=len(overdue), due_soon=due_soon[:10], recent=recent, s=s,
                           has_books=stats["titles"] > 0)


# ---------------------------------------------------------------- catalogue
def _catalogue_query():
    q = _clean(request.args.get("q"), 80)
    category = _clean(request.args.get("category"), 80)
    avail = request.args.get("avail", "")
    where, params = ["b.active = 1"], []
    if q:
        pat = _like(q)
        where.append("(LOWER(b.title) LIKE ? ESCAPE '\\' OR LOWER(b.author) LIKE ? ESCAPE '\\' "
                     "OR LOWER(b.isbn) LIKE ? ESCAPE '\\' OR LOWER(b.subject) LIKE ? ESCAPE '\\' "
                     "OR LOWER(b.shelf) LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM lib_copies c "
                     "WHERE c.book_id = b.id AND LOWER(c.accession_no) LIKE ? ESCAPE '\\'))")
        params += [pat] * 6
    if category:
        where.append("b.category = ?")
        params.append(category)
    inner = ("SELECT b.id, b.title, b.author, b.isbn, b.category, b.subject, b.shelf, b.price, "
             + _BOOK_STATS + " FROM lib_books b WHERE " + " AND ".join(where))
    outer_where = ""
    if avail == "yes":
        outer_where = " WHERE x.available > 0"
    elif avail == "no":
        outer_where = " WHERE x.available = 0"
    return inner, params, outer_where, q, category, avail


@library_bp.route("/catalogue")
def catalogue():
    inner, params, outer_where, q, category, avail = _catalogue_query()
    total = db.fetch_one(f"SELECT COUNT(*) AS n FROM ({inner}) x{outer_where}", tuple(params))["n"]
    page, pages, offset = _page(total, PAGE_SIZE)
    rows = db.fetch_all(
        f"SELECT * FROM ({inner}) x{outer_where} ORDER BY LOWER(x.title), x.id LIMIT ? OFFSET ?",
        tuple(params) + (PAGE_SIZE, offset))
    return render_template("library/catalogue.html", rows=rows, total=total, page=page, pages=pages,
                           q=q, category=category, avail=avail, categories=_categories(), qs=_qs,
                           is_librarian=_can_see_borrowers())


# ---------------------------------------------------------------- books
@library_bp.route("/books/new", methods=["GET", "POST"])
def book_new():
    if request.method == "GET":
        return render_template("library/book_form.html", book=None, categories=_categories(),
                               form={}, s=get_settings())
    fields, error = _book_fields(request.form)
    if error:
        flash(error, "error")
        return render_template("library/book_form.html", book=None, categories=_categories(),
                               form=request.form, s=get_settings()), 400
    if not request.form.get("force"):
        dup = None
        if fields["isbn"]:
            dup = db.fetch_one("SELECT id, title FROM lib_books WHERE active = 1 AND LOWER(isbn) = ?",
                               (fields["isbn"].lower(),))
        if not dup:
            dup = db.fetch_one(
                "SELECT id, title FROM lib_books WHERE active = 1 AND LOWER(title) = ? AND LOWER(COALESCE(author, '')) = ?",
                (fields["title"].lower(), fields["author"].lower()))
        if dup:
            flash(f"“{dup['title']}” is already in the catalogue. Add more copies to it here instead "
                  f"(tick “Add as a separate title anyway” on the form if it really is a different book).", "error")
            return redirect(url_for("library.book_detail", book_id=dup["id"]))
    book_id = db.execute(
        "INSERT INTO lib_books (title, author, isbn, publisher, year, edition, category, subject, shelf, price, "
        "notes, created_on, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (fields["title"], fields["author"], fields["isbn"], fields["publisher"], fields["year"], fields["edition"],
         fields["category"], fields["subject"], fields["shelf"], fields["price"], fields["notes"], _today()))
    custom = [x for x in re.split(r"[\s,;]+", request.form.get("accessions", "")) if x]
    count = _clamp_int(request.form.get("copies"), 0, MAX_COPIES_AT_ONCE, 1)
    created, problems = add_copies(book_id, count, custom or None) if (custom or count) else ([], [])
    flash(f"Added “{fields['title']}” with {len(created)} cop{'y' if len(created) == 1 else 'ies'}"
          + (f" ({created[0]}{' to ' + created[-1] if len(created) > 1 else ''})." if created else "."), "success")
    for p in problems[:5]:
        flash(p, "error")
    return redirect(url_for("library.book_detail", book_id=book_id))


@library_bp.route("/books/<int:book_id>")
def book_detail(book_id):
    book = db.fetch_one("SELECT b.*, " + _BOOK_STATS + " FROM lib_books b WHERE b.id = ?", (book_id,))
    if not book:
        flash("That book wasn't found.", "error")
        return redirect(url_for("library.catalogue"))
    librarian = _can_see_borrowers()
    copies = [dict(r) for r in db.fetch_all(
        "SELECT c.id, c.accession_no, c.status, c.notes, c.added_on, l.id AS loan_id, l.due_on, "
        "COALESCE(st.name, t.name, l.borrower_name) AS holder, l.student_id, l.teacher_id "
        "FROM lib_copies c LEFT JOIN lib_loans l ON l.copy_id = c.id AND l.returned_on IS NULL "
        "LEFT JOIN students st ON l.student_id = st.id LEFT JOIN teachers t ON l.teacher_id = t.id "
        "WHERE c.book_id = ? ORDER BY c.accession_no", (book_id,))]
    today = _today()
    for c in copies:
        c["on_loan"] = c["loan_id"] is not None
        c["overdue"] = bool(c["on_loan"] and c["due_on"] < today)
        if not librarian:
            c["holder"] = None
    history = []
    if librarian:
        history = _decorate(db.fetch_all(_LOAN_SELECT + " WHERE c.book_id = ? ORDER BY l.id DESC LIMIT 40", (book_id,)))
    return render_template("library/book.html", b=book, copies=copies, history=history,
                           categories=_categories(), statuses=COPY_STATUSES, status_labels=STATUS_LABELS,
                           is_librarian=librarian, s=get_settings())


@library_bp.route("/books/<int:book_id>/edit", methods=["POST"])
def book_edit(book_id):
    if not db.fetch_one("SELECT 1 AS x FROM lib_books WHERE id = ?", (book_id,)):
        return redirect(url_for("library.catalogue"))
    fields, error = _book_fields(request.form)
    if error:
        flash(error, "error")
        return redirect(url_for("library.book_detail", book_id=book_id))
    db.execute(
        "UPDATE lib_books SET title = ?, author = ?, isbn = ?, publisher = ?, year = ?, edition = ?, category = ?, "
        "subject = ?, shelf = ?, price = ?, notes = ? WHERE id = ?",
        (fields["title"], fields["author"], fields["isbn"], fields["publisher"], fields["year"], fields["edition"],
         fields["category"], fields["subject"], fields["shelf"], fields["price"], fields["notes"], book_id))
    flash("Book details saved.", "success")
    return redirect(url_for("library.book_detail", book_id=book_id))


@library_bp.route("/books/<int:book_id>/delete", methods=["POST"])
def book_delete(book_id):
    book = db.fetch_one("SELECT title FROM lib_books WHERE id = ?", (book_id,))
    if not book:
        return redirect(url_for("library.catalogue"))
    ever_lent = db.fetch_one(
        "SELECT COUNT(*) AS n FROM lib_loans l JOIN lib_copies c ON l.copy_id = c.id WHERE c.book_id = ?",
        (book_id,))["n"]
    on_loan = db.fetch_one(
        "SELECT COUNT(*) AS n FROM lib_loans l JOIN lib_copies c ON l.copy_id = c.id "
        "WHERE c.book_id = ? AND l.returned_on IS NULL", (book_id,))["n"]
    if on_loan:
        flash(f"“{book['title']}” has {on_loan} cop{'y' if on_loan == 1 else 'ies'} on loan. Get them back first.", "error")
        return redirect(url_for("library.book_detail", book_id=book_id))
    if ever_lent:
        # keep the loan history; just take it off the shelf
        db.execute("UPDATE lib_books SET active = 0 WHERE id = ?", (book_id,))
        db.execute("UPDATE lib_copies SET status = 'withdrawn' WHERE book_id = ?", (book_id,))
        flash(f"“{book['title']}” has loan history, so it was withdrawn from the catalogue instead of deleted.", "success")
    else:
        db.execute("DELETE FROM lib_books WHERE id = ?", (book_id,))
        flash(f"“{book['title']}” was deleted.", "success")
    return redirect(url_for("library.catalogue"))


@library_bp.route("/books/<int:book_id>/copies", methods=["POST"])
def copies_add(book_id):
    if not db.fetch_one("SELECT 1 AS x FROM lib_books WHERE id = ? AND active = 1", (book_id,)):
        return redirect(url_for("library.catalogue"))
    custom = [x for x in re.split(r"[\s,;]+", request.form.get("accessions", "")) if x]
    count = _clamp_int(request.form.get("count"), 1, MAX_COPIES_AT_ONCE, 1)
    created, problems = add_copies(book_id, count, custom or None)
    if created:
        flash(f"Added {len(created)} cop{'y' if len(created) == 1 else 'ies'}: {created[0]}"
              + (f" to {created[-1]}" if len(created) > 1 else "") + ".", "success")
    for p in problems[:5]:
        flash(p, "error")
    return redirect(url_for("library.book_detail", book_id=book_id))


@library_bp.route("/copies/<int:copy_id>/status", methods=["POST"])
def copy_status(copy_id):
    copy = db.fetch_one("SELECT id, book_id, accession_no FROM lib_copies WHERE id = ?", (copy_id,))
    if not copy:
        return redirect(url_for("library.catalogue"))
    status = request.form.get("status")
    if status not in COPY_STATUSES:
        flash("Choose a valid status.", "error")
    elif copy_open_loan(copy_id):
        flash(f"{copy['accession_no']} is on loan. Return it (as lost or damaged if needed) first.", "error")
    else:
        db.execute("UPDATE lib_copies SET status = ?, notes = ? WHERE id = ?",
                   (status, _clean(request.form.get("notes"), 200) or None, copy_id))
        flash(f"{copy['accession_no']} is now {STATUS_LABELS[status].lower()}.", "success")
    return redirect(url_for("library.book_detail", book_id=copy["book_id"]))


@library_bp.route("/copies/<int:copy_id>/delete", methods=["POST"])
def copy_delete(copy_id):
    copy = db.fetch_one("SELECT id, book_id, accession_no FROM lib_copies WHERE id = ?", (copy_id,))
    if not copy:
        return redirect(url_for("library.catalogue"))
    if db.fetch_one("SELECT 1 AS x FROM lib_loans WHERE copy_id = ?", (copy_id,)):
        flash(f"{copy['accession_no']} has loan history, so it can't be deleted. Set it to “withdrawn” instead.", "error")
    else:
        db.execute("DELETE FROM lib_copies WHERE id = ?", (copy_id,))
        flash(f"{copy['accession_no']} was deleted.", "success")
    return redirect(url_for("library.book_detail", book_id=copy["book_id"]))


# ---------------------------------------------------------------- import
_IMPORT_COLUMNS = [
    ("title", ("title", "booktitle", "bookname", "titleofbook", "name")),
    ("author", ("author", "authors", "writer", "by")),
    ("isbn", ("isbn", "isbnno", "isbnnumber")),
    ("publisher", ("publisher", "publishedby")),
    ("year", ("year", "publicationyear", "yearpublished", "published")),
    ("edition", ("edition", "ed")),
    ("category", ("category", "class", "type", "genre", "section")),
    ("subject", ("subject", "learningarea")),
    ("shelf", ("shelf", "location", "shelfno", "shelflocation", "rack")),
    ("price", ("price", "cost", "value", "replacementcost")),
    ("copies", ("copies", "quantity", "qty", "noofcopies", "numberofcopies", "count")),
    ("accession", ("accession", "accessionno", "accessionnumber", "accessionnumbers", "accnos", "bookno", "barcode")),
    ("notes", ("notes", "remarks", "comments")),
]
_TITLE_ALIASES = set(_IMPORT_COLUMNS[0][1])


def _read_upload_rows():
    """(DictReader rows list, error) from an uploaded CSV / Excel file."""
    f = request.files.get("file")
    if not f or not f.filename:
        return None, "Choose a CSV or Excel file first."
    data = f.read(import_files.MAX_BINARY_UPLOAD_BYTES + 1)
    kind = import_files.sniff_kind(data, f.filename)
    limit = import_files.MAX_BINARY_UPLOAD_BYTES if kind else 2 * 1024 * 1024
    if len(data) > limit:
        return None, f"That file is too large (limit {limit // (1024 * 1024)} MB)."
    if kind in ("pdf", "xls"):
        return None, "Save the sheet as .xlsx or CSV and upload that."
    if kind:
        rows, err = import_files.rows_from_upload(data, kind)
        if err:
            return None, err
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        text = buf.getvalue()
    elif data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, "The file looks empty."
    header_at, delimiter = 0, max((",", ";", "\t"), key=lines[0].count)
    for i, line in enumerate(lines[:15]):
        try:
            cells = next(csv.reader([line], delimiter=max((",", ";", "\t"), key=line.count)))
        except (StopIteration, csv.Error):
            continue
        if any(_norm(c) in _TITLE_ALIASES for c in cells):
            header_at, delimiter = i, max((",", ";", "\t"), key=line.count)
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_at:])), delimiter=delimiter)
    if not reader.fieldnames:
        return None, "The file looks empty."
    cols = {key: _find_column(reader.fieldnames, *aliases) for key, aliases in _IMPORT_COLUMNS}
    if not cols["title"]:
        return None, "I couldn't find a Title column. The first row should have headings like Title, Author, ISBN, Category, Copies."
    rows = []
    for r in reader:
        rows.append({key: (r.get(col) or "").strip() if col else "" for key, col in cols.items()})
        if len(rows) > MAX_IMPORT_ROWS:
            return None, f"That file has more than {MAX_IMPORT_ROWS} rows. Split it into smaller files."
    return rows, None


def _import_books(rows):
    """Add the rows in ONE transaction (all or nothing). Returns a summary dict."""
    s = get_settings()
    prefix, counter = s["accession_prefix"], s["next_accession"]
    conn = db.get_connection()
    cur = conn.cursor()
    summary = {"new_titles": 0, "copies": 0, "merged": 0, "skipped": []}
    try:
        cur.execute("SELECT id, LOWER(title) AS t, LOWER(COALESCE(author, '')) AS a, LOWER(COALESCE(isbn, '')) AS i "
                    "FROM lib_books WHERE active = 1")
        by_isbn, by_title = {}, {}
        for r in cur.fetchall():
            if r["i"]:
                by_isbn[r["i"]] = r["id"]
            by_title[(r["t"], r["a"])] = r["id"]
        cur.execute("SELECT LOWER(accession_no) AS a FROM lib_copies")
        used = {r["a"] for r in cur.fetchall()}
        today = _today()

        for n, row in enumerate(rows, start=2):   # row 1 is the heading
            title = _clean(row["title"], 250)
            if not title:
                continue
            author, isbn = _clean(row["author"], 200), _clean(row["isbn"], 30)
            price = _num(row["price"])
            if price is not None and not 0 <= price <= 1_000_000:
                summary["skipped"].append(f"Row {n} ({title}): price isn't valid.")
                continue
            copies = _clamp_int(_num(row["copies"], 1) or 1, 1, 200, 1) if row["copies"] else 1
            custom = [x for x in re.split(r"[\s,;]+", row["accession"]) if x]
            if custom:
                bad = [x for x in custom if not ACCESSION_RE.match(x) or x.lower() in used]
                if bad:
                    summary["skipped"].append(f"Row {n} ({title}): accession number {bad[0]} is invalid or already used.")
                    continue
                if len({x.lower() for x in custom}) != len(custom):
                    summary["skipped"].append(f"Row {n} ({title}): the same accession number is listed twice.")
                    continue
                accs = custom
            else:
                accs = []
                while len(accs) < copies:
                    acc = f"{prefix}{counter:05d}"
                    counter += 1
                    if acc.lower() not in used:
                        accs.append(acc)
            book_id = by_isbn.get(isbn.lower()) if isbn else None
            if book_id is None:
                book_id = by_title.get((title.lower(), author.lower()))
            if book_id is None:
                book_id = db._insert_and_get_id(
                    cur,
                    "INSERT INTO lib_books (title, author, isbn, publisher, year, edition, category, subject, shelf, "
                    "price, notes, created_on, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (title, author, isbn, _clean(row["publisher"], 150), _clean(row["year"], 10),
                     _clean(row["edition"], 40), _clean(row["category"], 80), _clean(row["subject"], 80),
                     _clean(row["shelf"], 40), price, _clean(row["notes"], 600), today))
                if isbn:
                    by_isbn[isbn.lower()] = book_id
                by_title[(title.lower(), author.lower())] = book_id
                summary["new_titles"] += 1
            else:
                summary["merged"] += 1
            for acc in accs:
                cur.execute(db._adapt("INSERT INTO lib_copies (book_id, accession_no, status, added_on) "
                                      "VALUES (?, ?, 'available', ?)"), (book_id, acc, today))
                used.add(acc.lower())
                summary["copies"] += 1
        cur.execute(db._adapt("INSERT INTO lib_settings (key, value) VALUES ('next_accession', ?) "
                              "ON CONFLICT (key) DO UPDATE SET value = excluded.value"), (str(counter),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return summary


@library_bp.route("/books/import", methods=["GET", "POST"])
def books_import():
    if request.method == "GET":
        return render_template("library/import.html")
    rows, error = _read_upload_rows()
    if error:
        flash(error, "error")
        return redirect(url_for("library.books_import"))
    if not rows:
        flash("The file has headings but no books in it.", "error")
        return redirect(url_for("library.books_import"))
    try:
        summary = _import_books(rows)
    except Exception:
        flash("The import failed and nothing was added. Check the file and try again.", "error")
        return redirect(url_for("library.books_import"))
    flash(f"Imported {summary['new_titles']} new title{'s' if summary['new_titles'] != 1 else ''} and "
          f"{summary['copies']} cop{'y' if summary['copies'] == 1 else 'ies'}"
          + (f" ({summary['merged']} row(s) were added to titles you already had)." if summary["merged"] else "."),
          "success")
    for line in summary["skipped"][:8]:
        flash(line, "error")
    if len(summary["skipped"]) > 8:
        flash(f"…and {len(summary['skipped']) - 8} more rows were skipped.", "error")
    return redirect(url_for("library.catalogue") if summary["copies"] else url_for("library.books_import"))


@library_bp.route("/books/import/template.csv")
def import_template():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([k.capitalize() for k, _ in _IMPORT_COLUMNS])
    w.writerow(["Oxford Secondary Mathematics Form 3", "KLB", "9789966100000", "KLB", "2020", "3rd", "Textbook",
                "Mathematics", "A2", "850", "5", "", "Example row - delete me"])
    w.writerow(["Blossoms of the Savannah", "Henry Ole Kulet", "", "Longhorn", "2008", "", "Fiction",
                "English", "F1", "600", "2", "", ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=library_books_template.csv"})


# ---------------------------------------------------------------- circulation
@library_bp.route("/circulation")
def circulation():
    s = get_settings()
    borrower, loans = None, []
    parsed = parse_borrower(request.args.get("borrower"))
    if parsed:
        borrower = get_borrower(*parsed)
        if borrower:
            loans = borrower_loans(parsed[0], parsed[1], open_only=True)
    default_due = {
        "s": (date.today() + timedelta(days=s["loan_days_student"])).isoformat(),
        "t": (date.today() + timedelta(days=s["loan_days_teacher"])).isoformat(),
    }
    return render_template("library/circulation.html", borrower=borrower, loans=loans, s=s,
                           default_due=default_due, today=_today())


@library_bp.route("/issue", methods=["POST"])
def issue():
    key = request.form.get("borrower", "")
    back = url_for("library.circulation", borrower=key) if parse_borrower(key) else url_for("library.circulation")
    parsed = parse_borrower(key)
    if not parsed:
        flash("Choose who is borrowing first.", "error")
        return redirect(back)
    copy = find_copy(request.form.get("accession"))
    if not copy:
        flash(f"No book has the accession number “{_clean(request.form.get('accession'), 40)}”.", "error")
        return redirect(back)
    ok, msg = issue_copy(copy, parsed[0], parsed[1], due_on=request.form.get("due_on") or None,
                         override=bool(request.form.get("override")))
    flash(msg, "success" if ok else "error")
    return redirect(back)


@library_bp.route("/return", methods=["POST"])
def return_book():
    outcome = request.form.get("outcome", "returned")
    fine_text = (request.form.get("fine") or "").strip()
    fine = _num(fine_text) if fine_text else None
    if fine_text and (fine is None or fine < 0 or fine > 1_000_000):
        flash("The fine must be a number (or leave it blank to use the usual amount).", "error")
    else:
        ok, msg = return_copy(request.form.get("accession"),
                              outcome if outcome in ("returned", "damaged", "lost") else "returned",
                              fine, request.form.get("note", ""))
        flash(msg, "success" if ok else "error")
    key = request.form.get("borrower", "")
    default = url_for("library.circulation", borrower=key) if parse_borrower(key) else url_for("library.circulation")
    return redirect(_safe_next(default))


@library_bp.route("/loans/<int:loan_id>/renew", methods=["POST"])
def renew(loan_id):
    ok, msg = renew_loan(loan_id)
    flash(msg, "success" if ok else "error")
    return redirect(_safe_next(url_for("library.loans")))


# ---------------------------------------------------------------- loans list
def _loans_filter():
    status = request.args.get("status", "open")
    q = _clean(request.args.get("q"), 80)
    start, end = _parse_date(request.args.get("from")), _parse_date(request.args.get("to"))
    where, params = [], []
    if status == "open":
        where.append("l.returned_on IS NULL")
    elif status == "overdue":
        where.append("l.returned_on IS NULL AND l.due_on < ?")
        params.append(_today())
    elif status == "returned":
        where.append("l.returned_on IS NOT NULL")
    elif status == "lost":
        where.append("l.outcome = 'lost'")
    else:
        status = "all"
    if q:
        pat = _like(q)
        where.append("(LOWER(b.title) LIKE ? ESCAPE '\\' OR LOWER(c.accession_no) LIKE ? ESCAPE '\\' "
                     "OR LOWER(COALESCE(st.name, t.name, l.borrower_name, '')) LIKE ? ESCAPE '\\' "
                     "OR LOWER(COALESCE(st.admission_no, l.borrower_ref, '')) LIKE ? ESCAPE '\\')")
        params += [pat] * 4
    if start:
        where.append("l.issued_on >= ?")
        params.append(start.isoformat())
    if end:
        where.append("l.issued_on <= ?")
        params.append(end.isoformat())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return status, q, start, end, clause, params


_LOAN_FROM = _LOAN_SELECT[_LOAN_SELECT.index("FROM lib_loans l"):]


@library_bp.route("/loans")
def loans():
    status, q, start, end, clause, params = _loans_filter()
    total = db.fetch_one("SELECT COUNT(*) AS n " + _LOAN_FROM + clause, tuple(params))["n"]
    page, pages, offset = _page(total, LOANS_PAGE_SIZE)
    rows = _decorate(db.fetch_all(
        _LOAN_SELECT + clause + " ORDER BY l.issued_on DESC, l.id DESC LIMIT ? OFFSET ?",
        tuple(params) + (LOANS_PAGE_SIZE, offset)))
    return render_template("library/loans.html", rows=rows, total=total, page=page, pages=pages, status=status,
                           q=q, start=start.isoformat() if start else "", end=end.isoformat() if end else "", qs=_qs)


@library_bp.route("/loans.csv")
def loans_csv():
    status, q, start, end, clause, params = _loans_filter()
    rows = _decorate(db.fetch_all(_LOAN_SELECT + clause + " ORDER BY l.issued_on DESC, l.id DESC LIMIT 20000",
                                  tuple(params)))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Accession no", "Title", "Author", "Borrower", "Adm no", "Class", "Type", "Issued", "Due",
                "Returned", "Days late", "Outcome", "Fine (KES)", "Fine status", "Issued by", "Received by"])
    for r in rows:
        w.writerow([_csv_safe(x) for x in (
            r["accession_no"], r["title"], r["author"] or "", r["who"], r["ref"], r["class_name"],
            r["borrower_type"], r["issued_on"], r["due_on"], r["returned_on"] or "",
            r["days_late"] if r["is_open"] else "", r["outcome"] or ("on loan" if r["is_open"] else ""),
            r["fine_amount"] if not r["is_open"] else r["fine_now"], r["fine_status"],
            r["issued_by"] or "", r["returned_to"] or "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=library_loans_{_today()}.csv"})


# ---------------------------------------------------------------- borrower page
@library_bp.route("/borrower/<kind>/<int:borrower_id>")
def borrower(kind, borrower_id):
    if kind not in ("s", "t"):
        return redirect(url_for("library.index"))
    b = get_borrower(kind, borrower_id)
    if not b:
        flash("That person wasn't found.", "error")
        return redirect(url_for("library.circulation"))
    s = get_settings()
    everything = borrower_loans(kind, borrower_id)
    current = [l for l in everything if l["is_open"]]
    history = [l for l in everything if not l["is_open"]]
    unpaid = [l for l in history if l["fine_status"] == "unpaid"]
    owed = round(sum(l["fine_amount"] for l in unpaid), 2)
    guardian = _guardians([borrower_id]).get(borrower_id) if kind == "s" else None
    return render_template("library/borrower.html", b=b, current=current, history=history, unpaid=unpaid, owed=owed,
                           guardian=guardian, cleared=not current and not unpaid, s=s,
                           limit=s["max_loans_student"] if kind == "s" else s["max_loans_teacher"])


# ---------------------------------------------------------------- fines
@library_bp.route("/fines")
def fines():
    unpaid = _decorate(db.fetch_all(
        _LOAN_SELECT + " WHERE l.fine_status = 'unpaid' ORDER BY l.returned_on DESC, l.id DESC"))
    accruing = [l for l in open_loans("AND l.due_on < ?", (_today(),)) if l["fine_now"] > 0]
    settled = _decorate(db.fetch_all(
        _LOAN_SELECT + " WHERE l.fine_status IN ('paid', 'waived') ORDER BY l.fine_settled_on DESC, l.id DESC LIMIT 30"))
    return render_template("library/fines.html", unpaid=unpaid, accruing=accruing, settled=settled,
                           total_unpaid=round(sum(l["fine_amount"] for l in unpaid), 2),
                           total_accruing=round(sum(l["fine_now"] for l in accruing), 2))


@library_bp.route("/loans/<int:loan_id>/fine", methods=["POST"])
def fine_settle(loan_id):
    loan = db.fetch_one("SELECT id, fine_status, fine_note FROM lib_loans WHERE id = ?", (loan_id,))
    action = request.form.get("action")
    note = _clean(request.form.get("note"), 200)
    if not loan or loan["fine_status"] != "unpaid":
        flash("That fine isn't waiting to be settled.", "error")
    elif action == "paid":
        db.execute("UPDATE lib_loans SET fine_status = 'paid', fine_settled_on = ?, fine_note = ? WHERE id = ?",
                   (_today(), note or loan["fine_note"], loan_id))
        flash("Fine marked as paid.", "success")
    elif action == "waive":
        if not note:
            flash("Give a short reason for waiving the fine.", "error")
        else:
            db.execute("UPDATE lib_loans SET fine_status = 'waived', fine_settled_on = ?, fine_note = ? WHERE id = ?",
                       (_today(), note, loan_id))
            flash("Fine waived.", "success")
    return redirect(_safe_next(url_for("library.fines")))


# ---------------------------------------------------------------- reports
def _grades_list():
    return db.fetch_all("SELECT id, name FROM grades ORDER BY sort_order, id")


@library_bp.route("/reports")
def reports():
    top_books = db.fetch_all(
        "SELECT b.id, b.title, b.author, COUNT(l.id) AS n FROM lib_loans l JOIN lib_copies c ON l.copy_id = c.id "
        "JOIN lib_books b ON c.book_id = b.id GROUP BY b.id, b.title, b.author ORDER BY n DESC, b.title LIMIT 10")
    top_readers = db.fetch_all(
        "SELECT l.student_id AS id, st.name, st.admission_no, COUNT(*) AS n FROM lib_loans l "
        "JOIN students st ON l.student_id = st.id GROUP BY l.student_id, st.name, st.admission_no "
        "ORDER BY n DESC, st.name LIMIT 10")
    by_class = db.fetch_all(
        "SELECT COALESCE(sm.name, g.name, 'Unassigned') AS cls, COUNT(*) AS n FROM lib_loans l "
        "JOIN students st ON l.student_id = st.id LEFT JOIN grades g ON st.grade_id = g.id "
        "LEFT JOIN streams sm ON st.stream_id = sm.id "
        "GROUP BY COALESCE(sm.name, g.name, 'Unassigned') ORDER BY n DESC, cls")
    by_month = db.fetch_all(
        "SELECT substr(issued_on, 1, 7) AS month, COUNT(*) AS n FROM lib_loans "
        "GROUP BY substr(issued_on, 1, 7) ORDER BY month DESC LIMIT 12")
    by_category = db.fetch_all(
        "SELECT COALESCE(NULLIF(b.category, ''), 'Uncategorised') AS cat, COUNT(DISTINCT b.id) AS titles, "
        "COUNT(c.id) AS copies FROM lib_books b LEFT JOIN lib_copies c ON c.book_id = b.id AND c.status <> 'withdrawn' "
        "WHERE b.active = 1 GROUP BY COALESCE(NULLIF(b.category, ''), 'Uncategorised') ORDER BY copies DESC, cat")
    never = db.fetch_one(
        "SELECT COUNT(*) AS n FROM lib_books b WHERE b.active = 1 AND NOT EXISTS (SELECT 1 FROM lib_loans l "
        "JOIN lib_copies c ON l.copy_id = c.id WHERE c.book_id = b.id)")["n"]
    return render_template("library/reports.html", top_books=top_books, top_readers=top_readers,
                           by_class=by_class, by_month=list(reversed(by_month)), by_category=by_category,
                           never=never, grades=_grades_list(), n_overdue=len(open_loans("AND l.due_on < ?", (_today(),))))


def _overdue_rows():
    rows = open_loans("AND l.due_on < ?", (_today(),), order="l.due_on, who")
    guardians = _guardians([r["student_id"] for r in rows if r["student_id"]])
    for r in rows:
        name, phone = guardians.get(r["student_id"], ("", ""))
        r["guardian"], r["phone"] = name, phone
    return rows


@library_bp.route("/reports/overdue.csv")
def overdue_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Borrower", "Adm no", "Class", "Book", "Accession no", "Issued", "Due", "Days late",
                "Fine so far (KES)", "Guardian", "Guardian phone"])
    for r in _overdue_rows():
        w.writerow([_csv_safe(x) for x in (r["who"], r["ref"], r["class_name"], r["title"], r["accession_no"],
                                           r["issued_on"], r["due_on"], r["days_late"], r["fine_now"],
                                           r["guardian"], r["phone"])])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=library_overdue_{_today()}.csv"})


@library_bp.route("/reports/inventory.csv")
def inventory_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Accession no", "Title", "Author", "ISBN", "Publisher", "Year", "Edition", "Category", "Subject",
                "Shelf", "Price (KES)", "Status", "On loan to"])
    for r in db.fetch_all(
            "SELECT c.accession_no, c.status, b.title, b.author, b.isbn, b.publisher, b.year, b.edition, b.category, "
            "b.subject, b.shelf, b.price, COALESCE(st.name, t.name, l.borrower_name) AS holder "
            "FROM lib_copies c JOIN lib_books b ON c.book_id = b.id "
            "LEFT JOIN lib_loans l ON l.copy_id = c.id AND l.returned_on IS NULL "
            "LEFT JOIN students st ON l.student_id = st.id LEFT JOIN teachers t ON l.teacher_id = t.id "
            "WHERE b.active = 1 ORDER BY LOWER(b.title), c.accession_no"):
        w.writerow([_csv_safe(x if x is not None else "") for x in (
            r["accession_no"], r["title"], r["author"], r["isbn"], r["publisher"], r["year"], r["edition"],
            r["category"], r["subject"], r["shelf"], r["price"], r["status"], r["holder"])])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=library_inventory_{_today()}.csv"})


def _pdf_styles():
    styles = getSampleStyleSheet()
    return (styles,
            ParagraphStyle("lib_cell", parent=styles["Normal"], fontSize=8, leading=9.6),
            ParagraphStyle("lib_head", parent=styles["Normal"], fontSize=8, leading=9.6,
                           textColor=colors.white, fontName="Helvetica-Bold"))


def _pdf_table(head_labels, data_rows, widths, head_style, cell_style):
    data = [[Paragraph(h, head_style) for h in head_labels]]
    for row in data_rows:
        data.append([Paragraph(escape(str(c)), cell_style) if not isinstance(c, Paragraph) else c for c in row])
    tbl = Table(data, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#103524")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cfc6ad")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fbf8f0")]),
    ]))
    return tbl


def _pdf_response(story, filename, page=None):
    page = page or landscape(A4)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    doc.build(story)
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment;filename={filename}"})


@library_bp.route("/reports/overdue.pdf")
def overdue_pdf():
    rows = _overdue_rows()
    styles, cell, head = _pdf_styles()
    school = db.get_school_settings()["name"]
    story = branding.logo_story()
    story.append(Paragraph(escape(f"{school} — Library: overdue books"), styles["Title"]))
    story.append(Paragraph(f"As at {_libdate(_today())} · {len(rows)} overdue book{'s' if len(rows) != 1 else ''}",
                           styles["Normal"]))
    story.append(Spacer(1, 6))
    if not rows:
        story.append(Paragraph("No books are overdue.", styles["Normal"]))
    else:
        data = [[f"{r['who']} ({r['ref']})" if r["ref"] else r["who"], r["class_name"],
                 f"{r['title']} [{r['accession_no']}]", _libdate(r["due_on"]), r["days_late"],
                 _libmoney(r["fine_now"]), " ".join(x for x in (r["guardian"], r["phone"]) if x)] for r in rows]
        story.append(_pdf_table(["Borrower", "Class", "Book", "Due", "Days late", "Fine (KES)", "Guardian"],
                                data, [52 * mm, 28 * mm, 70 * mm, 24 * mm, 16 * mm, 20 * mm, 46 * mm], head, cell))
    return _pdf_response(story, f"library_overdue_{_today()}.pdf")


def _clearance_rows(grade_id=None):
    """Active students who still hold a book or owe a library fine, with the details."""
    where, params = ["st.active = 1"], []
    if grade_id:
        where.append("st.grade_id = ?")
        params.append(grade_id)
    students = db.fetch_all(
        "SELECT st.id, st.name, st.admission_no, COALESCE(sm.name, g.name, '') AS cls FROM students st "
        "LEFT JOIN grades g ON st.grade_id = g.id LEFT JOIN streams sm ON st.stream_id = sm.id "
        "WHERE " + " AND ".join(where) + " AND (EXISTS (SELECT 1 FROM lib_loans l WHERE l.student_id = st.id "
        "AND l.returned_on IS NULL) OR EXISTS (SELECT 1 FROM lib_loans l WHERE l.student_id = st.id "
        "AND l.fine_status = 'unpaid')) ORDER BY g.sort_order, g.id, cls, st.name", tuple(params))
    ids = {r["id"] for r in students}
    held, owed = defaultdict(list), defaultdict(float)
    for l in open_loans("AND l.student_id IS NOT NULL"):
        if l["student_id"] in ids:
            held[l["student_id"]].append(f"{l['title']} [{l['accession_no']}] due {_libdate(l['due_on'])}")
    for r in db.fetch_all("SELECT student_id, fine_amount FROM lib_loans WHERE fine_status = 'unpaid' "
                          "AND student_id IS NOT NULL"):
        if r["student_id"] in ids:
            owed[r["student_id"]] += r["fine_amount"]
    return [{"id": r["id"], "name": r["name"], "adm": r["admission_no"], "cls": r["cls"],
             "books": held[r["id"]], "owed": round(owed[r["id"]], 2)} for r in students]


@library_bp.route("/reports/clearance")
def clearance():
    grade_id = request.args.get("grade", type=int)
    rows = _clearance_rows(grade_id)
    return render_template("library/clearance.html", rows=rows, grades=_grades_list(), grade=grade_id)


@library_bp.route("/reports/clearance.pdf")
def clearance_pdf():
    grade_id = request.args.get("grade", type=int)
    rows = _clearance_rows(grade_id)
    styles, cell, head = _pdf_styles()
    school = db.get_school_settings()["name"]
    grade = next((g["name"] for g in _grades_list() if g["id"] == grade_id), "All grades")
    story = branding.logo_story()
    story.append(Paragraph(escape(f"{school} — Library clearance: not yet cleared"), styles["Title"]))
    story.append(Paragraph(escape(f"{grade} · as at {_libdate(_today())} · {len(rows)} student{'s' if len(rows) != 1 else ''}"),
                           styles["Normal"]))
    story.append(Spacer(1, 6))
    if not rows:
        story.append(Paragraph("Every student is cleared by the library.", styles["Normal"]))
    else:
        data = [[f"{r['name']} ({r['adm']})", r["cls"],
                 Paragraph("<br/>".join(escape(b) for b in r["books"]) or "—", cell),
                 _libmoney(r["owed"]) if r["owed"] else "—"] for r in rows]
        story.append(_pdf_table(["Student", "Class", "Books still held", "Fine owed (KES)"], data,
                                [58 * mm, 30 * mm, 140 * mm, 28 * mm], head, cell))
    return _pdf_response(story, f"library_clearance_{_today()}.pdf")


# ---------------------------------------------------------------- barcode labels
# Code 128 (set B) is built here so labels need nothing beyond reportlab's canvas.
# Each entry is the widths of the bar/space elements that make up one symbol.
CODE128_PATTERNS = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312",
    "132212", "221213", "221312", "231212", "112232", "122132", "122231", "113222",
    "123122", "123221", "223211", "221132", "221231", "213212", "223112", "312131",
    "311222", "321122", "321221", "312212", "322112", "322211", "212123", "212321",
    "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121",
    "313121", "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111", "111224",
    "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112",
    "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113",
    "114311", "411113", "411311", "113141", "114131", "311141", "411131", "211412",
    "211214", "211232", "2331112",
)

def code128_widths(text):
    """The bar/space width string for `text` (printable ASCII), including start, checksum and stop."""
    values = [104] + [ord(ch) - 32 for ch in text]
    checksum = (values[0] + sum(i * v for i, v in enumerate(values[1:], start=1))) % 103
    values += [checksum, 106]
    return "".join(CODE128_PATTERNS[v] for v in values)


def draw_code128(c, text, x, y, width, height):
    seq = code128_widths(text)
    unit = width / sum(int(d) for d in seq)
    cursor = x
    for i, d in enumerate(seq):
        w = int(d) * unit
        if i % 2 == 0:
            c.rect(cursor, y, w, height, stroke=0, fill=1)
        cursor += w


def _label_copies():
    """Copies to print labels for, from ?book=ID, ?ids=1,2,3 or ?recent=N."""
    base = ("SELECT c.id, c.accession_no, b.title FROM lib_copies c JOIN lib_books b ON c.book_id = b.id ")
    if request.args.get("book", type=int):
        return db.fetch_all(base + "WHERE c.book_id = ? AND c.status <> 'withdrawn' ORDER BY c.accession_no",
                            (request.args.get("book", type=int),))
    ids = [int(x) for x in re.findall(r"\d+", request.args.get("ids", ""))][:500]
    if ids:
        marks = ",".join("?" * len(ids))
        return db.fetch_all(base + f"WHERE c.id IN ({marks}) ORDER BY c.accession_no", tuple(ids))
    recent = _clamp_int(request.args.get("recent"), 1, 500, 0)
    if recent:
        rows = db.fetch_all(base + "ORDER BY c.id DESC LIMIT ?", (recent,))
        return sorted(rows, key=lambda r: r["accession_no"])
    return []


@library_bp.route("/labels.pdf")
def labels_pdf():
    copies = _label_copies()
    if not copies:
        flash("Choose a book (or the most recent copies) to print labels for.", "error")
        return redirect(url_for("library.catalogue"))
    school = db.get_school_settings()["name"]
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    cols, rows_n = 3, 8
    margin_x, margin_y = 8 * mm, 10 * mm
    lw = (A4[0] - 2 * margin_x) / cols
    lh = (A4[1] - 2 * margin_y) / rows_n
    for i, copy in enumerate(copies):
        pos = i % (cols * rows_n)
        if i and pos == 0:
            c.showPage()
        col, row = pos % cols, pos // cols
        x = margin_x + col * lw
        y = A4[1] - margin_y - (row + 1) * lh
        c.setStrokeColor(colors.HexColor("#cfc6ad"))
        c.setLineWidth(0.3)
        c.rect(x + 1, y + 1, lw - 2, lh - 2, stroke=1, fill=0)
        c.setFillColor(colors.HexColor("#103524"))
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(x + 4 * mm, y + lh - 5 * mm, school[:44])
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 7)
        title = copy["title"]
        c.drawString(x + 4 * mm, y + lh - 9 * mm, (title[:46] + "…") if len(title) > 47 else title)
        acc = copy["accession_no"]
        draw_code128(c, acc, x + 4 * mm, y + 8 * mm, lw - 8 * mm, lh - 21 * mm)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(x + lw / 2, y + 3.5 * mm, acc)
    c.save()
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": "inline;filename=library_labels.pdf"})


# ---------------------------------------------------------------- settings
@library_bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "GET":
        return render_template("library/settings.html", s=get_settings())
    f = request.form
    problems = []
    prefix = _clean(f.get("accession_prefix"), 12)
    if prefix and not re.fullmatch(r"[A-Za-z0-9._/\-]{0,12}", prefix):
        problems.append("The accession prefix can only use letters, numbers and . _ / -")
    values = {"accession_prefix": prefix}
    for key, low, high in (("loan_days_student", 1, 365), ("loan_days_teacher", 1, 365),
                           ("max_loans_student", 1, 50), ("max_loans_teacher", 1, 100),
                           ("max_renewals", 0, 10), ("fine_grace_days", 0, 60)):
        v = f.get(key, type=int)
        if v is None or not low <= v <= high:
            problems.append(f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.")
        else:
            values[key] = v
    for key in ("fine_per_day", "lost_fee_default"):
        v = _num(f.get(key))
        if v is None or not 0 <= v <= 100000:
            problems.append(f"{key.replace('_', ' ').capitalize()} must be an amount of 0 or more.")
        else:
            values[key] = v
    if problems:
        for p in problems:
            flash(p, "error")
        return redirect(url_for("library.settings"))
    for key in _FLAG_SETTINGS:
        values[key] = "1" if f.get(key) else "0"
    for key, value in values.items():
        _put_setting(key, value)
    flash("Library settings saved.", "success")
    return redirect(url_for("library.settings"))


# ---------------------------------------------------------------- lookups for the issue / return boxes
@library_bp.route("/api/borrowers")
def api_borrowers():
    return jsonify(find_borrowers(request.args.get("q", "")))


@library_bp.route("/api/copies")
def api_copies():
    q = _clean(request.args.get("q"), 60)
    if not q:
        return jsonify([])
    pat = _like(q)
    rows = db.fetch_all(
        "SELECT c.accession_no, c.status, b.title, b.author, "
        "(SELECT COUNT(*) FROM lib_loans l WHERE l.copy_id = c.id AND l.returned_on IS NULL) AS out "
        "FROM lib_copies c JOIN lib_books b ON c.book_id = b.id WHERE b.active = 1 AND "
        "(LOWER(c.accession_no) LIKE ? ESCAPE '\\' OR LOWER(b.title) LIKE ? ESCAPE '\\') "
        "ORDER BY LOWER(b.title), c.accession_no LIMIT 12", (pat, pat))
    return jsonify([{"accession": r["accession_no"], "title": r["title"], "author": r["author"] or "",
                     "state": ("on loan" if r["out"] else STATUS_LABELS.get(r["status"], r["status"]).lower())}
                    for r in rows])
