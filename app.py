import calendar
import hmac
import io
import os
import re
import sqlite3
import time
from datetime import date, datetime

from flask import (Flask, Response, g, jsonify, redirect, render_template,
                   request, session, url_for)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "payroll.db"))
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Ledgerly Pvt. Ltd.")

# ---------- salary rules (change here if your college wants different rules) ----------
HRA_PERCENT = 20      # HRA = 20% of basic
PF_PERCENT = 12       # PF  = 12% of basic
# Absent deduction = (gross / days in month) x unpaid days
# unpaid days = absent days + 0.5 x half days.  Leave (L) is paid leave.
# Days that have no attendance record are counted as Present.

STATUSES = {"P": "Present", "A": "Absent", "H": "Half day", "L": "Leave"}

# ---------- admin login settings ----------
ON_RENDER = bool(os.environ.get("RENDER"))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "" if ON_RENDER else "admin123")
if not ADMIN_PASSWORD:
    raise RuntimeError("Set the ADMIN_PASSWORD environment variable before deploying.")

app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_RENDER,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)

MAX_TRIES = 5
LOCK_SECONDS = 300
failed = {}  # ip -> [count, first_fail_time]


# ---------- database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS employees (
            emp_id      TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            department  TEXT NOT NULL,
            designation TEXT NOT NULL,
            basic       REAL NOT NULL,
            allowances  REAL NOT NULL DEFAULT 0,
            join_date   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attendance (
            emp_id TEXT NOT NULL,
            day    TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('P','A','H','L')),
            PRIMARY KEY (emp_id, day)
        );
        """
    )
    db.commit()
    db.close()


def error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def parse_money(value, allow_zero=False):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v > 1e9 or v < 0 or (v == 0 and not allow_zero):
        return None
    return round(v, 2)


def valid_date(text):
    try:
        datetime.strptime(str(text), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def valid_month(text):
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(text or "")))


def this_month():
    return date.today().strftime("%Y-%m")


# ---------- salary calculation ----------
def calc_salary(emp, month, counts):
    """emp: row/dict, month: 'YYYY-MM', counts: {'P':n,'A':n,'H':n,'L':n} (marked days only)."""
    year, mon = int(month[:4]), int(month[5:])
    days = calendar.monthrange(year, mon)[1]
    absent = counts.get("A", 0)
    half = counts.get("H", 0)
    leave = counts.get("L", 0)
    marked = absent + half + leave + counts.get("P", 0)
    present = counts.get("P", 0) + max(0, days - marked)  # unmarked days = present

    basic = round(emp["basic"], 2)
    hra = round(basic * HRA_PERCENT / 100, 2)
    allowances = round(emp["allowances"], 2)
    gross = round(basic + hra + allowances, 2)
    unpaid = absent + 0.5 * half
    absent_ded = round(gross / days * unpaid, 2)
    pf = round(basic * PF_PERCENT / 100, 2)
    total_ded = round(pf + absent_ded, 2)
    net = round(gross - total_ded, 2)
    return {
        "emp_id": emp["emp_id"], "name": emp["name"],
        "department": emp["department"], "designation": emp["designation"],
        "month": month, "days_in_month": days,
        "present": present, "absent": absent, "half": half, "leave": leave,
        "unpaid_days": unpaid,
        "basic": basic, "hra": hra, "allowances": allowances, "gross": gross,
        "pf": pf, "absent_deduction": absent_ded, "total_deductions": total_ded,
        "net": max(net, 0),
    }


def month_counts(db, month):
    rows = db.execute(
        "SELECT emp_id, status, COUNT(*) AS n FROM attendance WHERE substr(day,1,7)=? GROUP BY emp_id, status",
        (month,),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["emp_id"], {})[r["status"]] = r["n"]
    return out


def employees_joined_by(db, month):
    year, mon = int(month[:4]), int(month[5:])
    last_day = f"{month}-{calendar.monthrange(year, mon)[1]:02d}"
    return db.execute(
        "SELECT * FROM employees WHERE join_date <= ? ORDER BY emp_id", (last_day,)
    ).fetchall()


def payroll_for_month(db, month):
    counts = month_counts(db, month)
    return [calc_salary(e, month, counts.get(e["emp_id"], {})) for e in employees_joined_by(db, month)]


# ---------- login / logout ----------
def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()


def locked_seconds(ip):
    entry = failed.get(ip)
    if not entry:
        return 0
    count, first = entry
    if time.time() - first > LOCK_SECONDS:
        failed.pop(ip, None)
        return 0
    return int(LOCK_SECONDS - (time.time() - first)) if count >= MAX_TRIES else 0


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin"):
        return redirect(url_for("index"))
    message = None
    if request.method == "POST":
        ip = client_ip()
        wait = locked_seconds(ip)
        if wait:
            message = f"Too many wrong attempts. Try again in {wait // 60 + 1} minute(s)."
        else:
            user = request.form.get("username", "")
            pwd = request.form.get("password", "")
            ok_user = hmac.compare_digest(user.encode(), ADMIN_USERNAME.encode())
            ok_pwd = hmac.compare_digest(pwd.encode(), ADMIN_PASSWORD.encode())
            if ok_user and ok_pwd:
                failed.pop(ip, None)
                session.clear()
                session["admin"] = True
                session.permanent = True
                return redirect(url_for("index"))
            count, first = failed.get(ip, [0, time.time()])
            failed[ip] = [count + 1, first]
            message = "Wrong username or password."
    return render_template("login.html", message=message, company=COMPANY_NAME), (401 if message else 200)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.before_request
def require_login():
    if request.path.startswith("/api/") and not session.get("admin"):
        return jsonify({"ok": False, "error": "Please log in."}), 401


@app.route("/")
def index():
    if not session.get("admin"):
        return redirect(url_for("login"))
    return render_template("index.html", company=COMPANY_NAME)


# ---------- employees ----------
def read_employee(data, need_id):
    emp_id = str(data.get("emp_id", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    dept = str(data.get("department", "")).strip()
    desig = str(data.get("designation", "")).strip()
    basic = parse_money(data.get("basic"))
    allowances = parse_money(data.get("allowances", 0) or 0, allow_zero=True)
    join = str(data.get("join_date", "")).strip()
    if need_id and not re.fullmatch(r"[A-Z0-9-]{1,15}", emp_id):
        return None, "Employee ID must be 1-15 letters, digits or hyphen."
    if not name or len(name) > 60:
        return None, "Enter the employee name (up to 60 characters)."
    if not dept or len(dept) > 40:
        return None, "Enter the department."
    if not desig or len(desig) > 40:
        return None, "Enter the designation."
    if basic is None:
        return None, "Enter a valid basic salary greater than 0."
    if allowances is None:
        return None, "Allowances must be 0 or more."
    if not valid_date(join):
        return None, "Enter a valid joining date."
    return {"emp_id": emp_id, "name": name, "department": dept, "designation": desig,
            "basic": basic, "allowances": allowances, "join_date": join}, None


@app.get("/api/employees")
def list_employees():
    rows = get_db().execute("SELECT * FROM employees ORDER BY emp_id").fetchall()
    return jsonify({"ok": True, "employees": [dict(r) for r in rows]})


@app.post("/api/employees")
def add_employee():
    emp, err = read_employee(request.get_json(silent=True) or {}, need_id=True)
    if err:
        return error(err)
    db = get_db()
    if db.execute("SELECT 1 FROM employees WHERE emp_id=?", (emp["emp_id"],)).fetchone():
        return error("This employee ID already exists.", 409)
    db.execute(
        "INSERT INTO employees (emp_id,name,department,designation,basic,allowances,join_date) VALUES (?,?,?,?,?,?,?)",
        (emp["emp_id"], emp["name"], emp["department"], emp["designation"],
         emp["basic"], emp["allowances"], emp["join_date"]),
    )
    db.commit()
    return jsonify({"ok": True, "employee": emp}), 201


@app.put("/api/employees/<emp_id>")
def edit_employee(emp_id):
    emp_id = emp_id.upper()
    emp, err = read_employee(request.get_json(silent=True) or {}, need_id=False)
    if err:
        return error(err)
    db = get_db()
    if not db.execute("SELECT 1 FROM employees WHERE emp_id=?", (emp_id,)).fetchone():
        return error("Employee not found.", 404)
    db.execute(
        "UPDATE employees SET name=?, department=?, designation=?, basic=?, allowances=?, join_date=? WHERE emp_id=?",
        (emp["name"], emp["department"], emp["designation"], emp["basic"],
         emp["allowances"], emp["join_date"], emp_id),
    )
    db.commit()
    emp["emp_id"] = emp_id
    return jsonify({"ok": True, "employee": emp})


@app.delete("/api/employees/<emp_id>")
def delete_employee(emp_id):
    emp_id = emp_id.upper()
    db = get_db()
    if not db.execute("SELECT 1 FROM employees WHERE emp_id=?", (emp_id,)).fetchone():
        return error("Employee not found.", 404)
    db.execute("DELETE FROM attendance WHERE emp_id=?", (emp_id,))
    db.execute("DELETE FROM employees WHERE emp_id=?", (emp_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- attendance ----------
@app.get("/api/attendance")
def get_attendance():
    day = request.args.get("date", "")
    if not valid_date(day):
        return error("Choose a valid date.")
    rows = get_db().execute(
        """SELECT e.emp_id, e.name, e.department, a.status
           FROM employees e LEFT JOIN attendance a ON a.emp_id = e.emp_id AND a.day = ?
           WHERE e.join_date <= ? ORDER BY e.emp_id""",
        (day, day),
    ).fetchall()
    return jsonify({"ok": True, "date": day, "records": [dict(r) for r in rows]})


@app.post("/api/attendance")
def save_attendance():
    data = request.get_json(silent=True) or {}
    day = str(data.get("date", ""))
    records = data.get("records")
    if not valid_date(day):
        return error("Choose a valid date.")
    if not isinstance(records, list) or not records:
        return error("No attendance records to save.")
    db = get_db()
    saved = 0
    for rec in records:
        emp_id = str(rec.get("emp_id", "")).upper()
        status = str(rec.get("status", "")).upper()
        if status not in STATUSES:
            return error(f"Invalid status for {emp_id}.")
        if not db.execute("SELECT 1 FROM employees WHERE emp_id=?", (emp_id,)).fetchone():
            return error(f"Employee {emp_id} not found.", 404)
        db.execute(
            "INSERT INTO attendance (emp_id, day, status) VALUES (?,?,?) "
            "ON CONFLICT(emp_id, day) DO UPDATE SET status=excluded.status",
            (emp_id, day, status),
        )
        saved += 1
    db.commit()
    return jsonify({"ok": True, "saved": saved})


# ---------- payroll / stats ----------
@app.get("/api/payroll")
def payroll():
    month = request.args.get("month", this_month())
    if not valid_month(month):
        return error("Choose a valid month.")
    rows = payroll_for_month(get_db(), month)
    return jsonify({
        "ok": True, "month": month, "rows": rows,
        "total_gross": round(sum(r["gross"] for r in rows), 2),
        "total_deductions": round(sum(r["total_deductions"] for r in rows), 2),
        "total_net": round(sum(r["net"] for r in rows), 2),
    })


@app.get("/api/stats")
def stats():
    db = get_db()
    today = date.today().isoformat()
    total = db.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    depts = db.execute("SELECT COUNT(DISTINCT department) FROM employees").fetchone()[0]
    absent_today = db.execute(
        "SELECT COUNT(*) FROM attendance WHERE day=? AND status='A'", (today,)).fetchone()[0]
    rows = payroll_for_month(db, this_month())
    return jsonify({
        "ok": True, "employees": total, "departments": depts, "absent_today": absent_today,
        "month": this_month(), "payroll_net": round(sum(r["net"] for r in rows), 2),
    })


# ---------- salary slip PDF ----------
def rs(n):
    return f"Rs. {n:,.2f}"


def build_slip_pdf(r):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Salary slip {r['emp_id']} {r['month']}")
    styles = getSampleStyleSheet()
    green = colors.HexColor("#0f3d2e")
    gold = colors.HexColor("#c8962e")
    title = styles["Title"]
    title.textColor = green
    story = [
        Paragraph(COMPANY_NAME, title),
        Paragraph(f"<para align='center'>Salary slip for {calendar.month_name[int(r['month'][5:])]} {r['month'][:4]}</para>", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    info = [
        ["Employee ID", r["emp_id"], "Department", r["department"]],
        ["Name", r["name"], "Designation", r["designation"]],
        ["Days in month", str(r["days_in_month"]), "Days present", f"{r['present']:g}"],
        ["Absent days", f"{r['absent']:g}", "Half days / Leave", f"{r['half']:g} / {r['leave']:g}"],
    ]
    t = Table(info, colWidths=[32 * mm, 52 * mm, 36 * mm, 46 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8d2c0")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#faf7ee")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [t, Spacer(1, 8 * mm)]

    earn = [["Earnings", "Amount"], ["Basic pay", rs(r["basic"])],
            [f"HRA ({HRA_PERCENT}% of basic)", rs(r["hra"])], ["Allowances", rs(r["allowances"])],
            ["Gross salary", rs(r["gross"])]]
    ded = [["Deductions", "Amount"], [f"Provident fund ({PF_PERCENT}%)", rs(r["pf"])],
           [f"Absence ({r['unpaid_days']:g} unpaid day)", rs(r["absent_deduction"])], ["", ""],
           ["Total deductions", rs(r["total_deductions"])]]
    both = Table([[Table(earn, colWidths=[45 * mm, 33 * mm]), Table(ded, colWidths=[45 * mm, 33 * mm])]],
                 colWidths=[84 * mm, 84 * mm])
    for sub in both._cellvalues[0]:
        sub.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), green), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, green),
            ("GRID", (0, 0), (-1, -2), 0.3, colors.HexColor("#d8d2c0")),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
    story += [both, Spacer(1, 8 * mm)]

    net = Table([["NET SALARY PAYABLE", rs(r["net"])]], colWidths=[100 * mm, 68 * mm])
    net.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), gold), ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#2a1d00")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 13),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story += [net, Spacer(1, 12 * mm),
              Paragraph("This is a computer-generated salary slip and does not need a signature.",
                        styles["Italic"])]
    doc.build(story)
    return buf.getvalue()


@app.get("/api/payslip/<emp_id>")
def payslip(emp_id):
    month = request.args.get("month", this_month())
    if not valid_month(month):
        return error("Choose a valid month.")
    db = get_db()
    emp = db.execute("SELECT * FROM employees WHERE emp_id=?", (emp_id.upper(),)).fetchone()
    if not emp:
        return error("Employee not found.", 404)
    counts = month_counts(db, month).get(emp["emp_id"], {})
    pdf = build_slip_pdf(calc_salary(emp, month, counts))
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename=salary-slip-{emp['emp_id']}-{month}.pdf"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
