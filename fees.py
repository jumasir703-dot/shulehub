"""
fees.py — School Management: fee structures, invoices, payments, receipts.

    /fees/                       overview: billed, collected, outstanding, by grade
    /fees/structure              fee items per grade, per term (copy from last term)
    /fees/generate  (POST)       create invoices for every student without one
    /fees/student/<id>           one student's statement + record payment / charge / discount
    /fees/payments               payments register (+ CSV)
    /fees/balances               who owes what (+ CSV)
    /fees/mpesa                  import a Paybill statement CSV (matches on admission no.)
    /fees/payment/<id>/receipt.pdf, /fees/student/<id>/statement.pdf

How the money works
-------------------
* An invoice is one student × one term. Its lines are the grade's fee items
  for that term; extra charges and discounts/bursaries are more lines
  (discounts are negative).
* Payments belong to the student, not to one invoice. Balance =
  everything billed − every payment that hasn't been voided. Positive
  means the family owes; negative means they're in credit.
* Payments are never deleted. A mistake is voided with a reason, so the
  audit trail stays intact.
* M-Pesa: parents pay to the school's Paybill using the admission number as
  the account number. Download the Paybill statement as CSV and import it
  here — rows are matched to students by that account number and the
  transaction code is unique, so re-importing the same file adds nothing.
"""

import csv
import io
import re
from datetime import date, datetime
from xml.sax.saxutils import escape

from flask import (
    Blueprint, Response, flash, g, redirect, render_template, request, url_for,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import branding
import database as db
from analytics import _csv_safe, _find_column, _norm, _safe_filename

fees_bp = Blueprint("fees", __name__, url_prefix="/fees")

METHODS = ["M-Pesa", "Cash", "Bank", "Cheque", "Other"]
NEEDS_REFERENCE = {"M-Pesa", "Bank", "Cheque"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------- formatting
@fees_bp.app_template_filter("kes")
def _kes(v):
    if v is None:
        return "—"
    v = round(float(v), 2)
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


def _r2(v):
    return round(float(v or 0) + 0.0, 2)


def _who():
    return g.user["username"] if getattr(g, "user", None) else None


def _iso(value, allow_future=False):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(value, fmt).date()
            return d.isoformat() if (allow_future or d <= date.today()) else None
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- settings
def _setting(key, default=""):
    row = db.fetch_one("SELECT value FROM generation_settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] is not None else default


def current_term_year():
    today = date.today()
    guess_term = 1 if today.month <= 4 else 2 if today.month <= 8 else 3
    try:
        term = int(_setting("fees_term", guess_term))
        year = int(_setting("fees_year", today.year))
    except ValueError:
        term, year = guess_term, today.year
    return (term if term in (1, 2, 3) else guess_term), year


def _term_year_from_request():
    term, year = current_term_year()
    t = request.values.get("term", type=int)
    y = request.values.get("year", type=int)
    return (t if t in (1, 2, 3) else term), (y if y and 2000 <= y <= 2100 else year)


# ---------------------------------------------------------------- ledger
_BILLED_SQL = ("COALESCE((SELECT SUM(l.amount) FROM invoice_lines l JOIN invoices i ON l.invoice_id = i.id "
               "WHERE i.student_id = st.id), 0)")
_PAID_SQL = "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.student_id = st.id AND p.voided = 0), 0)"


def student_balance(student_id):
    row = db.fetch_one(f"SELECT {_BILLED_SQL} AS billed, {_PAID_SQL} AS paid FROM students st WHERE st.id=?", (student_id,))
    return _r2(row["billed"] - row["paid"]) if row else 0.0


def student_fee_summary(student_id):
    invoices = [dict(r) for r in db.fetch_all(
        "SELECT id, term, year, created_on FROM invoices WHERE student_id=? ORDER BY year, term", (student_id,))]
    lines = db.fetch_all(
        "SELECT l.id, l.invoice_id, l.description, l.amount, l.kind FROM invoice_lines l "
        "JOIN invoices i ON l.invoice_id = i.id WHERE i.student_id=? ORDER BY l.id", (student_id,))
    by_inv = {}
    for l in lines:
        by_inv.setdefault(l["invoice_id"], []).append(dict(l))
    for inv in invoices:
        inv["lines"] = by_inv.get(inv["id"], [])
        inv["total"] = _r2(sum(l["amount"] for l in inv["lines"]))
    payments = [dict(r) for r in db.fetch_all(
        "SELECT * FROM payments WHERE student_id=? ORDER BY paid_on DESC, id DESC", (student_id,))]

    entries = []
    for inv in invoices:
        entries.append({"date": inv["created_on"] or "", "order": 0, "id": inv["id"],
                        "desc": f"Term {inv['term']} {inv['year']} invoice", "charge": inv["total"], "payment": 0.0})
    for p in payments:
        if not p["voided"]:
            ref = f" · {p['reference']}" if p["reference"] else ""
            entries.append({"date": p["paid_on"], "order": 1, "id": p["id"],
                            "desc": f"Payment ({p['method']}{ref}) — {p['receipt_no'] or ''}".strip(" —"),
                            "charge": 0.0, "payment": _r2(p["amount"])})
    entries.sort(key=lambda e: (e["date"], e["order"], e["id"]))
    running = 0.0
    for e in entries:
        running = _r2(running + e["charge"] - e["payment"])
        e["balance"] = running
    billed = _r2(sum(i["total"] for i in invoices))
    paid = _r2(sum(p["amount"] for p in payments if not p["voided"]))
    return {"invoices": invoices, "payments": payments, "ledger": entries,
            "billed": billed, "paid": paid, "balance": _r2(billed - paid)}


def _balance_rows(grade_id=None, stream_id=None, view="owing", min_amount=0.0, q=""):
    sql = (
        "SELECT st.id, st.admission_no, st.name, st.status, g.name AS grade_name, s.name AS stream_name, "
        f"{_BILLED_SQL} AS billed, {_PAID_SQL} AS paid, "
        "(SELECT gu.phone FROM guardians gu WHERE gu.student_id = st.id ORDER BY gu.is_primary DESC, gu.id LIMIT 1) AS phone, "
        "(SELECT gu.name FROM guardians gu WHERE gu.student_id = st.id ORDER BY gu.is_primary DESC, gu.id LIMIT 1) AS guardian "
        "FROM students st JOIN grades g ON st.grade_id = g.id LEFT JOIN streams s ON st.stream_id = s.id WHERE 1=1")
    params = []
    if grade_id:
        sql += " AND st.grade_id = ?"
        params.append(grade_id)
    if stream_id:
        sql += " AND st.stream_id = ?"
        params.append(stream_id)
    if q:
        sql += " AND (LOWER(st.name) LIKE ? OR LOWER(st.admission_no) LIKE ?)"
        params += [f"%{q.lower()}%"] * 2
    sql += " ORDER BY g.sort_order, s.name, st.name"
    rows = []
    for r in db.fetch_all(sql, tuple(params)):
        d = dict(r)
        d["balance"] = _r2(d["billed"] - d["paid"])
        if view == "owing" and (d["balance"] <= 0.004 or d["balance"] < min_amount):
            continue
        if view == "credit" and d["balance"] >= -0.004:
            continue
        if view == "cleared" and not (d["billed"] > 0 and abs(d["balance"]) < 0.005):
            continue
        rows.append(d)
    return rows


def outstanding_total():
    """Total KES currently owed by families (for the home page)."""
    return _r2(sum(r["balance"] for r in _balance_rows(view="owing")))


# ---------------------------------------------------------------- payments helpers
def _insert_payment(cur, student_id, amount, method, reference, paid_on, received_by, note=None):
    """Insert one payment on an open cursor and give it its receipt number."""
    pid = db._insert_and_get_id(
        cur,
        "INSERT INTO payments (student_id, amount, method, reference, paid_on, received_by, note, voided) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (student_id, _r2(amount), method, reference or None, paid_on, received_by, note or None))
    cur.execute(db._adapt("UPDATE payments SET receipt_no=? WHERE id=?"), (f"RCT-{pid:05d}", pid))
    return pid


def _ensure_invoice(cur, student_id, term, year):
    cur.execute(db._adapt("SELECT id FROM invoices WHERE student_id=? AND term=? AND year=?"), (student_id, term, year))
    row = cur.fetchone()
    if row:
        return row["id"]
    return db._insert_and_get_id(
        cur, "INSERT INTO invoices (student_id, term, year, created_on) VALUES (?, ?, ?, ?)",
        (student_id, term, year, date.today().isoformat()))


# ---------------------------------------------------------------- overview
@fees_bp.route("/")
def index():
    term, year = current_term_year()
    totals = db.fetch_one(
        "SELECT COALESCE((SELECT SUM(l.amount) FROM invoice_lines l), 0) AS billed, "
        "COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.voided = 0), 0) AS paid")
    month_start = date.today().replace(day=1).isoformat()
    month = db.fetch_one(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE voided=0 AND paid_on >= ?", (month_start,))["s"]
    rows = _balance_rows(view="all")
    owing = [r for r in rows if r["balance"] > 0]
    by_grade = {}
    for r in rows:
        x = by_grade.setdefault(r["grade_name"], {"grade": r["grade_name"], "billed": 0.0, "paid": 0.0, "owing": 0.0, "n_owing": 0})
        x["billed"] += r["billed"]
        x["paid"] += r["paid"]
        if r["balance"] > 0:
            x["owing"] += r["balance"]
            x["n_owing"] += 1
    recent = db.fetch_all(
        "SELECT p.id, p.amount, p.method, p.paid_on, p.receipt_no, p.voided, st.name, st.admission_no, st.id AS sid "
        "FROM payments p JOIN students st ON p.student_id = st.id ORDER BY p.id DESC LIMIT 8")
    has_items = db.fetch_one("SELECT COUNT(*) AS c FROM fee_items")["c"] > 0
    return render_template(
        "fees/index.html", term=term, year=year, billed=_r2(totals["billed"]), paid=_r2(totals["paid"]),
        outstanding=_r2(sum(r["balance"] for r in owing)), n_owing=len(owing), month=_r2(month),
        by_grade=list(by_grade.values()), recent=recent, has_items=has_items,
        paybill=_setting("fees_paybill"), footer=_setting("fees_note"))


@fees_bp.route("/settings", methods=["POST"])
def settings_save():
    term = request.form.get("term", type=int)
    year = request.form.get("year", type=int)
    if term in (1, 2, 3):
        db.set_generation_setting("fees_term", str(term))
    if year and 2000 <= year <= 2100:
        db.set_generation_setting("fees_year", str(year))
    db.set_generation_setting("fees_paybill", request.form.get("paybill", "").strip()[:40])
    db.set_generation_setting("fees_note", request.form.get("note", "").strip()[:300])
    flash("Fee settings saved.", "success")
    return redirect(url_for("fees.index"))


# ---------------------------------------------------------------- structure
@fees_bp.route("/structure")
def structure():
    term, year = _term_year_from_request()
    grades = db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id")
    items = db.fetch_all(
        "SELECT * FROM fee_items WHERE term=? AND year=? ORDER BY grade_id, id", (term, year))
    by_grade = {}
    for it in items:
        by_grade.setdefault(it["grade_id"], []).append(it)
    totals = {gid: _r2(sum(i["amount"] for i in lst)) for gid, lst in by_grade.items()}
    return render_template("fees/structure.html", term=term, year=year, grades=grades,
                           by_grade=by_grade, totals=totals)


@fees_bp.route("/structure/add", methods=["POST"])
def structure_add():
    term, year = _term_year_from_request()
    name = request.form.get("name", "").strip()
    grade_id = request.form.get("grade_id", type=int)  # 0/None = every grade
    try:
        amount = float(request.form.get("amount", "").replace(",", ""))
    except ValueError:
        amount = None
    back = url_for("fees.structure", term=term, year=year)
    if not name or amount is None or amount < 0:
        flash("Enter an item name and an amount (0 or more).", "error")
        return redirect(back)
    targets = [r["id"] for r in db.fetch_all("SELECT id FROM grades")] if not grade_id else [grade_id]
    db.execute_many([(
        "INSERT INTO fee_items (grade_id, term, year, name, amount) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (grade_id, term, year, name) DO UPDATE SET amount = excluded.amount",
        (gid, term, year, name, _r2(amount))) for gid in targets])
    flash(f"“{name}” saved for {'all grades' if not grade_id else 'the grade'}.", "success")
    return redirect(back)


@fees_bp.route("/structure/<int:item_id>/delete", methods=["POST"])
def structure_delete(item_id):
    it = db.fetch_one("SELECT term, year FROM fee_items WHERE id=?", (item_id,))
    db.execute("DELETE FROM fee_items WHERE id=?", (item_id,))
    flash("Fee item removed. Invoices that already include it are not changed.", "success")
    return redirect(url_for("fees.structure", term=it["term"], year=it["year"]) if it else url_for("fees.structure"))


@fees_bp.route("/structure/copy", methods=["POST"])
def structure_copy():
    term, year = _term_year_from_request()
    from_term = request.form.get("from_term", type=int)
    from_year = request.form.get("from_year", type=int)
    if from_term not in (1, 2, 3) or not from_year or (from_term, from_year) == (term, year):
        flash("Choose a different term to copy from.", "error")
    else:
        db.execute_many([(
            "INSERT INTO fee_items (grade_id, term, year, name, amount) "
            "SELECT grade_id, ?, ?, name, amount FROM fee_items WHERE term=? AND year=? "
            "ON CONFLICT (grade_id, term, year, name) DO NOTHING",
            (term, year, from_term, from_year))])
        flash(f"Copied Term {from_term} {from_year} fees into Term {term} {year}.", "success")
    return redirect(url_for("fees.structure", term=term, year=year))


@fees_bp.route("/generate", methods=["POST"])
def generate():
    term, year = _term_year_from_request()
    grade_id = request.form.get("grade_id", type=int)
    items = {}
    for it in db.fetch_all("SELECT grade_id, name, amount FROM fee_items WHERE term=? AND year=? ORDER BY id", (term, year)):
        items.setdefault(it["grade_id"], []).append(it)
    sql = ("SELECT st.id, st.grade_id FROM students st WHERE st.status='active' "
           "AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.student_id = st.id AND i.term=? AND i.year=?)")
    params = [term, year]
    if grade_id:
        sql += " AND st.grade_id=?"
        params.append(grade_id)
    students = db.fetch_all(sql, tuple(params))
    created = no_fees = 0
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        for st in students:
            lines = items.get(st["grade_id"])
            if not lines:
                no_fees += 1
                continue
            iid = db._insert_and_get_id(
                cur, "INSERT INTO invoices (student_id, term, year, created_on) VALUES (?, ?, ?, ?)",
                (st["id"], term, year, date.today().isoformat()))
            for ln in lines:
                cur.execute(db._adapt(
                    "INSERT INTO invoice_lines (invoice_id, description, amount, kind) VALUES (?, ?, ?, 'fee')"),
                    (iid, ln["name"], ln["amount"]))
            created += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    msg = f"Created {created} invoice(s) for Term {term} {year}."
    if no_fees:
        msg += f" {no_fees} student(s) skipped because their grade has no fee items for this term."
    if not students:
        msg = f"Every active student already has a Term {term} {year} invoice."
    flash(msg, "success" if created else "error")
    return redirect(url_for("fees.structure", term=term, year=year))


# ---------------------------------------------------------------- student ledger
def _student_or_none(student_id):
    return db.fetch_one(
        "SELECT st.id, st.admission_no, st.name, st.status, g.name AS grade_name, s.name AS stream_name "
        "FROM students st JOIN grades g ON st.grade_id = g.id LEFT JOIN streams s ON st.stream_id = s.id "
        "WHERE st.id=?", (student_id,))


@fees_bp.route("/student/<int:student_id>")
def student(student_id):
    st = _student_or_none(student_id)
    if not st:
        flash("Student not found.", "error")
        return redirect(url_for("fees.balances"))
    term, year = current_term_year()
    guardian = db.fetch_one(
        "SELECT name, phone FROM guardians WHERE student_id=? ORDER BY is_primary DESC, id LIMIT 1", (student_id,))
    return render_template(
        "fees/student.html", st=st, s=student_fee_summary(student_id), methods=METHODS, term=term, year=year,
        today=date.today().isoformat(), guardian=guardian, receipt_id=request.args.get("receipt", type=int))


@fees_bp.route("/student/<int:student_id>/pay", methods=["POST"])
def pay(student_id):
    st = _student_or_none(student_id)
    back = url_for("fees.student", student_id=student_id)
    if not st:
        return redirect(url_for("fees.balances"))
    f = request.form
    try:
        amount = float(f.get("amount", "").replace(",", ""))
    except ValueError:
        amount = 0
    method = f.get("method", "")
    reference = f.get("reference", "").strip().upper() or None
    paid_on = _iso(f.get("paid_on"))
    err = None
    if not (0 < amount < 100_000_000):
        err = "Enter the amount paid."
    elif method not in METHODS:
        err = "Choose how it was paid."
    elif not paid_on:
        err = "Enter a valid payment date (not in the future)."
    elif method in NEEDS_REFERENCE and not reference:
        err = f"Enter the {method} transaction code or reference."
    if err:
        flash(err, "error")
        return redirect(back)
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        pid = _insert_payment(cur, student_id, amount, method, reference, paid_on, _who(), f.get("note", "").strip())
        conn.commit()
    except Exception:
        conn.rollback()
        flash(f"The reference {reference} has already been recorded on another payment.", "error")
        return redirect(back)
    finally:
        conn.close()
    flash(f"Payment of KES {_kes(amount)} recorded.", "success")
    return redirect(url_for("fees.student", student_id=student_id, receipt=pid))


@fees_bp.route("/student/<int:student_id>/line", methods=["POST"])
def line_add(student_id):
    back = url_for("fees.student", student_id=student_id)
    if not _student_or_none(student_id):
        return redirect(url_for("fees.balances"))
    f = request.form
    desc = f.get("description", "").strip()
    kind = f.get("kind", "charge")
    term, year = _term_year_from_request()
    try:
        amount = abs(float(f.get("amount", "").replace(",", "")))
    except ValueError:
        amount = 0
    if not desc or amount <= 0 or kind not in ("charge", "discount"):
        flash("Enter a description and an amount.", "error")
        return redirect(back)
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        iid = _ensure_invoice(cur, student_id, term, year)
        cur.execute(db._adapt("INSERT INTO invoice_lines (invoice_id, description, amount, kind) VALUES (?, ?, ?, ?)"),
                    (iid, desc, _r2(amount if kind == "charge" else -amount), kind))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(("Charge" if kind == "charge" else "Discount") + f" added to Term {term} {year}.", "success")
    return redirect(back)


@fees_bp.route("/line/<int:line_id>/delete", methods=["POST"])
def line_delete(line_id):
    row = db.fetch_one(
        "SELECT i.student_id FROM invoice_lines l JOIN invoices i ON l.invoice_id = i.id WHERE l.id=?", (line_id,))
    if row:
        db.execute("DELETE FROM invoice_lines WHERE id=?", (line_id,))
        flash("Line removed.", "success")
        return redirect(url_for("fees.student", student_id=row["student_id"]))
    return redirect(url_for("fees.balances"))


@fees_bp.route("/payment/<int:payment_id>/void", methods=["POST"])
def void(payment_id):
    p = db.fetch_one("SELECT student_id, voided FROM payments WHERE id=?", (payment_id,))
    if not p:
        return redirect(url_for("fees.balances"))
    back = url_for("fees.student", student_id=p["student_id"])
    reason = request.form.get("reason", "").strip()
    if p["voided"]:
        return redirect(back)
    if not reason:
        flash("Give a reason for voiding the payment.", "error")
        return redirect(back)
    db.execute("UPDATE payments SET voided=1, voided_reason=? WHERE id=?", (f"{reason} (by {_who()})", payment_id))
    flash("Payment voided. It no longer counts toward the balance.", "success")
    return redirect(back)


# ---------------------------------------------------------------- registers
@fees_bp.route("/balances")
def balances():
    f = request.args
    grade_id, stream_id = f.get("grade_id", type=int), f.get("stream_id", type=int)
    view = f.get("view", "owing")
    min_amount = max(0.0, f.get("min", 0, type=float) or 0.0)
    q = f.get("q", "").strip()
    rows = _balance_rows(grade_id, stream_id, view, min_amount, q)
    return render_template(
        "fees/balances.html", rows=rows[:500], total=len(rows),
        owed=_r2(sum(r["balance"] for r in rows if r["balance"] > 0)),
        grades=db.fetch_all("SELECT * FROM grades ORDER BY sort_order, id"),
        streams=db.fetch_all("SELECT id, name, grade_id FROM streams ORDER BY name"),
        grade_id=grade_id, stream_id=stream_id, view=view, min_amount=min_amount, q=q)


@fees_bp.route("/balances.csv")
def balances_csv():
    f = request.args
    rows = _balance_rows(f.get("grade_id", type=int), f.get("stream_id", type=int), f.get("view", "owing"),
                         max(0.0, f.get("min", 0, type=float) or 0.0), f.get("q", "").strip())
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["admission_no", "name", "grade", "stream", "billed", "paid", "balance", "guardian", "phone"])
    for r in rows:
        w.writerow([_csv_safe(x) for x in (r["admission_no"], r["name"], r["grade_name"], r["stream_name"] or "",
                                           r["billed"], r["paid"], r["balance"], r["guardian"] or "", r["phone"] or "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=fee_balances.csv"})


def _payment_filters():
    f = request.args
    today = date.today()
    start = _iso(f.get("from"), True) or today.replace(day=1).isoformat()
    end = _iso(f.get("to"), True) or today.isoformat()
    method = f.get("method", "")
    sql = ("SELECT p.*, st.name, st.admission_no, g.name AS grade_name FROM payments p "
           "JOIN students st ON p.student_id = st.id JOIN grades g ON st.grade_id = g.id "
           "WHERE p.paid_on >= ? AND p.paid_on <= ?")
    params = [start, end]
    if method in METHODS:
        sql += " AND p.method = ?"
        params.append(method)
    sql += " ORDER BY p.paid_on DESC, p.id DESC"
    return db.fetch_all(sql, tuple(params)), start, end, method


@fees_bp.route("/payments")
def payments():
    rows, start, end, method = _payment_filters()
    live = [r for r in rows if not r["voided"]]
    by_method = {}
    for r in live:
        by_method[r["method"]] = _r2(by_method.get(r["method"], 0) + r["amount"])
    return render_template(
        "fees/payments.html", rows=rows[:500], total=len(rows), start=start, end=end, method=method,
        methods=METHODS, collected=_r2(sum(r["amount"] for r in live)), by_method=by_method)


@fees_bp.route("/payments.csv")
def payments_csv():
    rows, start, end, _ = _payment_filters()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "receipt", "admission_no", "name", "grade", "method", "reference", "amount", "status", "received_by"])
    for r in rows:
        w.writerow([_csv_safe(x) for x in (r["paid_on"], r["receipt_no"], r["admission_no"], r["name"], r["grade_name"],
                                           r["method"], r["reference"] or "", r["amount"],
                                           "VOID" if r["voided"] else "ok", r["received_by"] or "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=payments_{start}_to_{end}.csv"})


# ---------------------------------------------------------------- M-Pesa statement import
_RECEIPT_COLS = ("receiptno", "receipt", "transid", "transactionid", "transactioncode", "mpesacode", "code", "reference")
_AMOUNT_COLS = ("paidin", "amount", "transamount", "creditamount", "credit")
_ACCOUNT_COLS = ("acno", "accountno", "accountnumber", "account", "billrefnumber", "billref", "ac", "accno")
_DATE_COLS = ("completiontime", "transtime", "transactiondate", "date", "time", "initiationtime")
_NAME_COLS = ("otherpartyinfo", "payer", "name", "customername", "firstname", "msisdn")
_STATUS_COLS = ("transactionstatus", "status")
_DETAIL_COLS = ("details", "description", "narrative")
_DATE_FORMATS = ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                 "%Y%m%d%H%M%S", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")


def _parse_when(raw):
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(raw, fmt).date()
            return d.isoformat() if d <= date.today() else date.today().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()


def _parse_mpesa(text):
    """Return (rows, problem). Each row: code, amount, date, account, payer, status, student."""
    lines = text.splitlines()
    header_at = None
    for i, line in enumerate(lines[:40]):
        try:
            cells = [_norm(c) for c in next(csv.reader([line]))]
        except StopIteration:
            continue
        if any(c in _RECEIPT_COLS for c in cells) and any(c in _AMOUNT_COLS for c in cells):
            header_at = i
            break
    if header_at is None:
        return [], ("Couldn't find the columns. The file needs a transaction code column (e.g. Receipt No) "
                    "and an amount column (e.g. Paid In), plus an account number column.")
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_at:])))
    names = reader.fieldnames or []
    col = lambda aliases: _find_column(names, *aliases)  # noqa: E731
    c_code, c_amt = col(_RECEIPT_COLS), col(_AMOUNT_COLS)
    c_acc, c_date, c_name = col(_ACCOUNT_COLS), col(_DATE_COLS), col(_NAME_COLS)
    c_stat, c_det = col(_STATUS_COLS), col(_DETAIL_COLS)
    if not (c_acc or c_det):
        return [], "The file has no account number column (e.g. A/C No, BillRefNumber), so payments can't be matched to students."

    students = {r["admission_no"].strip().lower(): r for r in db.fetch_all(
        "SELECT st.id, st.admission_no, st.name, g.name AS grade_name FROM students st JOIN grades g ON st.grade_id = g.id")}
    known = {r["reference"] for r in db.fetch_all("SELECT reference FROM payments WHERE voided=0 AND reference IS NOT NULL")}
    seen, rows = set(), []
    for raw in reader:
        code = (raw.get(c_code) or "").strip().upper()
        if not code:
            continue
        if c_stat and (raw.get(c_stat) or "").strip() and (raw.get(c_stat) or "").strip().lower() != "completed":
            continue
        try:
            amount = float((raw.get(c_amt) or "").replace(",", "").strip() or 0)
        except ValueError:
            continue
        if amount <= 0:
            continue
        account = (raw.get(c_acc) or "").strip() if c_acc else ""
        if not account and c_det:
            m = re.search(r"acc(?:ount)?\.?\s*(?:no\.?)?\s*[:#-]?\s*([A-Za-z0-9/\-]+)\s*$", raw.get(c_det) or "", re.I)
            account = m.group(1) if m else ""
        stu = students.get(re.sub(r"\s+", "", account).lower()) or students.get(account.strip().lower())
        if code in known or code in seen:
            status = "duplicate"
        elif not stu:
            status = "unmatched"
        else:
            status = "ok"
        seen.add(code)
        rows.append({"code": code, "amount": _r2(amount), "date": _parse_when(raw.get(c_date) if c_date else ""),
                     "account": account, "payer": (raw.get(c_name) or "").strip() if c_name else "",
                     "status": status, "student": dict(stu) if stu else None})
    return rows, None


@fees_bp.route("/mpesa")
def mpesa():
    return render_template("fees/mpesa.html", rows=None, paybill=_setting("fees_paybill"))


@fees_bp.route("/mpesa/preview", methods=["POST"])
def mpesa_preview():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose the M-Pesa statement CSV first.", "error")
        return redirect(url_for("fees.mpesa"))
    data = f.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        flash("That file is too large (limit 2 MB).", "error")
        return redirect(url_for("fees.mpesa"))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    rows, problem = _parse_mpesa(text)
    if problem:
        flash(problem, "error")
        return redirect(url_for("fees.mpesa"))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("ok", "duplicate", "unmatched")}
    return render_template("fees/mpesa.html", rows=rows, counts=counts, csv_text=text,
                           total_ok=_r2(sum(r["amount"] for r in rows if r["status"] == "ok")),
                           paybill=_setting("fees_paybill"))


@fees_bp.route("/mpesa/confirm", methods=["POST"])
def mpesa_confirm():
    text = request.form.get("csv_text", "")
    rows, problem = _parse_mpesa(text[:MAX_UPLOAD_BYTES * 2])  # re-parsed on the server: the preview isn't trusted
    if problem:
        flash(problem, "error")
        return redirect(url_for("fees.mpesa"))
    todo = [r for r in rows if r["status"] == "ok"]
    if not todo:
        flash("Nothing new to import.", "error")
        return redirect(url_for("fees.mpesa"))
    conn = db.get_connection()
    cur = conn.cursor()
    try:
        for r in todo:
            _insert_payment(cur, r["student"]["id"], r["amount"], "M-Pesa", r["code"], r["date"],
                            f"M-Pesa import ({_who()})", r["payer"])
        conn.commit()
    except Exception:
        conn.rollback()
        flash("Import stopped because a transaction code was recorded in the meantime. Nothing was saved — upload the file again.", "error")
        return redirect(url_for("fees.mpesa"))
    finally:
        conn.close()
    flash(f"Imported {len(todo)} M-Pesa payment(s) totalling KES {_kes(sum(r['amount'] for r in todo))}.", "success")
    return redirect(url_for("fees.payments"))


# ---------------------------------------------------------------- PDFs
def _school():
    s = db.get_school_settings()
    return s["name"] if s else "School"


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], fontSize=15, leading=18, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontSize=9, textColor=colors.HexColor("#555555"), alignment=1),
        "body": ParagraphStyle("b", parent=ss["Normal"], fontSize=10, leading=13),
        "small": ParagraphStyle("sm", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#555555")),
    }


def _pdf_response(buf, title):
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment;filename={_safe_filename(title, 'pdf')}"})


@fees_bp.route("/payment/<int:payment_id>/receipt.pdf")
def receipt_pdf(payment_id):
    p = db.fetch_one("SELECT * FROM payments WHERE id=?", (payment_id,))
    if not p:
        return "Payment not found.", 404
    st = _student_or_none(p["student_id"])
    st_sum = student_fee_summary(p["student_id"])
    ss = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Receipt {p['receipt_no']}")
    story = branding.logo_story() + [Paragraph(escape(_school()), ss["title"]), Paragraph("OFFICIAL RECEIPT", ss["sub"]), Spacer(1, 8)]
    if p["voided"]:
        story.append(Paragraph(f"<font color='#a3372c'><b>VOIDED</b> — {escape(p['voided_reason'] or '')}</font>", ss["body"]))
        story.append(Spacer(1, 6))
    cls = st["grade_name"] + (f" · {st['stream_name']}" if st["stream_name"] else "")
    rows = [
        ["Receipt no.", p["receipt_no"] or ""], ["Date", p["paid_on"]],
        ["Received from", f"{st['name']} (Adm. {st['admission_no']})"], ["Class", cls],
        ["Method", p["method"] + (f" — {p['reference']}" if p["reference"] else "")],
        ["Amount paid", f"KES {_kes(p['amount'])}"],
        ["Balance now", f"KES {_kes(st_sum['balance'])}" + (" (credit)" if st_sum["balance"] < 0 else "")],
        ["Received by", p["received_by"] or ""],
    ]
    t = Table([[Paragraph(f"<b>{escape(a)}</b>", ss["body"]), Paragraph(escape(str(b)), ss["body"])] for a, b in rows],
              colWidths=[34 * mm, 78 * mm])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [t, Spacer(1, 10)]
    note = _setting("fees_note")
    if note:
        story.append(Paragraph(escape(note), ss["small"]))
    doc.build(story)
    return _pdf_response(buf, f"Receipt {p['receipt_no']}")


@fees_bp.route("/student/<int:student_id>/statement.pdf")
def statement_pdf(student_id):
    st = _student_or_none(student_id)
    if not st:
        return "Student not found.", 404
    s = student_fee_summary(student_id)
    ss = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Fee statement — {st['name']}")
    cls = st["grade_name"] + (f" · {st['stream_name']}" if st["stream_name"] else "")
    story = branding.logo_story() + [Paragraph(escape(_school()), ss["title"]), Paragraph("FEE STATEMENT", ss["sub"]), Spacer(1, 10),
             Paragraph(f"<b>{escape(st['name'])}</b> — Adm. {escape(st['admission_no'])} — {escape(cls)}", ss["body"]),
             Paragraph(f"Printed {date.today().isoformat()}", ss["small"]), Spacer(1, 10)]
    data = [["Date", "Description", "Charge", "Paid", "Balance"]]
    for e in s["ledger"]:
        data.append([e["date"], Paragraph(escape(e["desc"]), ss["body"]),
                     _kes(e["charge"]) if e["charge"] else "", _kes(e["payment"]) if e["payment"] else "", _kes(e["balance"])])
    data.append(["", Paragraph("<b>Balance due</b>" if s["balance"] >= 0 else "<b>Credit</b>", ss["body"]),
                 _kes(s["billed"]), _kes(s["paid"]), _kes(abs(s["balance"]))])
    t = Table(data, colWidths=[24 * mm, 76 * mm, 25 * mm, 25 * mm, 26 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4e6c6")), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
    story.append(t)
    paybill = _setting("fees_paybill")
    if paybill:
        story += [Spacer(1, 12), Paragraph(
            f"Pay by M-Pesa: Paybill <b>{escape(paybill)}</b>, account number <b>{escape(st['admission_no'])}</b>.", ss["body"])]
    if _setting("fees_note"):
        story += [Spacer(1, 6), Paragraph(escape(_setting("fees_note")), ss["small"])]
    doc.build(story)
    return _pdf_response(buf, f"Fee statement {st['name']}")
