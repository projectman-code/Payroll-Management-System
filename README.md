# Payroll Management System (Flask + SQLite)

Employee management, daily attendance, automatic salary calculation and
downloadable salary slip PDFs. One admin login (no register page).

## Features
- Employees: add, edit, delete, search (department, designation, basic salary, allowances)
- Attendance: pick a date, mark Present / Absent / Half day / Leave for every employee
- Payroll: pick a month, see gross, deductions and net salary for each employee
- Salary slip: one PDF per employee per month
- Dashboard: net payroll this month, employee count, departments, absent today

## Salary rules (edit the top of app.py to change them)
- HRA = 20% of basic
- Gross = basic + HRA + allowances
- PF = 12% of basic
- Absence deduction = (gross / days in month) x unpaid days
- Unpaid days = absent days + 0.5 x half days. Leave is paid.
- Days with no saved attendance count as Present.
- Net salary = gross - PF - absence deduction

## Security
- Every API call (including the PDF) needs the admin session, checked on the server.
- 5 wrong passwords from one IP -> locked for 5 minutes.
- Admin username and password come from environment variables.

## Run on your PC
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000 and sign in with  admin / admin123  (local testing only).
Your own login (PowerShell):
    $env:ADMIN_USERNAME="myname"; $env:ADMIN_PASSWORD="MyStrongPass"; python app.py
Change the company name on slips:
    $env:COMPANY_NAME="My Company"

## Push to GitHub
    git init
    git add .
    git commit -m "Payroll management system"
    git branch -M main
    git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
    git push -u origin main

## Deploy on Render (free)
1. Render -> New -> Web Service -> pick the repo.
2. Build command: pip install -r requirements.txt
3. Start command: gunicorn app:app
4. Environment tab, add:
       ADMIN_USERNAME = your admin name
       ADMIN_PASSWORD = a strong password (the app will not start without it)
       SECRET_KEY     = any long random text
       COMPANY_NAME   = your company name (optional)
5. Deploy.

Note: on the Render free plan the disk is temporary, so SQLite data resets when
the service restarts. For permanent data, move to MongoDB Atlas or PostgreSQL.
