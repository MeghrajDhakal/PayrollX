from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response, abort, g
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from config import Config
from models import db, User, Employee, Organization, SalaryStructure, AttendanceLog, StatutoryRate, PayrollRun, Payslip, TaxDeclaration, Reimbursement, AuditLog
from datetime import datetime, date
from functools import wraps

import os
from config import BUNDLE_DIR

app = Flask(__name__,
            template_folder=os.path.join(BUNDLE_DIR, 'templates'),
            static_folder=os.path.join(BUNDLE_DIR, 'static'))
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access payroll features.'
login_manager.login_message_category = 'info'

# ── Register Middleware ──
from middleware import (
    generate_jwt, decode_jwt, jwt_required, jwt_or_session_required,
    org_admin_required, role_api_required, register_audit_interceptor,
    generate_payslip_pdf
)
register_audit_interceptor(app)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ────────────────────────────────────────────
# RBAC MIDDLEWARE DECORATORS (Session-based)
# ────────────────────────────────────────────
def role_required(*allowed_roles):
    """Decorator: restrict a route to specific roles.
    Usage:  @role_required('hr_admin', 'finance')
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                flash('Access denied — insufficient permissions.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def get_org_name():
    """Helper – safely return the current user's organization name."""
    if current_user.is_authenticated and current_user.organization:
        return current_user.organization.name
    return 'Unknown Organization'


def get_employee_for_current_user():
    """Helper – returns the Employee record scoped to the current user + org.
    Enforces Row-Level Security: a user can only get *their own* Employee row."""
    return Employee.query.filter_by(
        user_id=current_user.id,
        org_id=current_user.org_id
    ).first()


# ────────────────────────────────────────────
# PAYROLL CALCULATION ENGINE
# ────────────────────────────────────────────
def calculate_epf(basic_monthly):
    """EPF: 12% of Basic (employee share), capped at ₹15,000 basic"""
    eligible_basic = min(basic_monthly, 15000)
    return round(eligible_basic * 0.12, 2)


def calculate_esi(gross_monthly):
    """ESI: 0.75% employee share if gross ≤ ₹21,000"""
    if gross_monthly <= 21000:
        return round(gross_monthly * 0.0075, 2)
    return 0


def calculate_professional_tax(gross_monthly):
    """Professional Tax (typical Karnataka/Maharashtra slab)"""
    if gross_monthly <= 15000:
        return 0
    elif gross_monthly <= 20000:
        return 150
    else:
        return 200


def calculate_tds_monthly(annual_taxable, regime='new'):
    """Simplified TDS under New Regime (FY 2025-26 slabs)"""
    if regime == 'new':
        slabs = [
            (300000, 0),
            (700000, 0.05),
            (1000000, 0.10),
            (1200000, 0.15),
            (1500000, 0.20),
            (float('inf'), 0.30)
        ]
    else:
        slabs = [
            (250000, 0),
            (500000, 0.05),
            (1000000, 0.20),
            (float('inf'), 0.30)
        ]

    tax = 0
    prev_limit = 0
    for limit, rate in slabs:
        if annual_taxable <= prev_limit:
            break
        taxable_in_slab = min(annual_taxable, limit) - prev_limit
        tax += taxable_in_slab * rate
        prev_limit = limit

    # Add 4% cess
    tax = tax * 1.04
    return round(tax / 12, 2)


def generate_payslip_for_employee(emp, run, month, year):
    """Generate a complete payslip for one employee.

    Pay is proportional to attendance:
      effective_pay = full_monthly_component × (days_present / total_working_days)
    LOP days are deducted on top of absences.
    """
    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, is_current=True).first()
    if not salary:
        return None

    attendance = AttendanceLog.query.filter_by(emp_id=emp.emp_id, month=month, year=year).first()
    working_days = attendance.total_working_days if attendance else 22
    days_present = attendance.days_present if attendance else working_days
    lop_days = attendance.lop_days if attendance else 0

    # Attendance ratio — pay scales with actual days worked
    attendance_ratio = days_present / working_days if working_days > 0 else 1.0

    # Full monthly components (before attendance adjustment)
    full_basic = round(salary.basic_pay / 12, 2)
    full_hra = round(salary.hra / 12, 2)
    full_da = round(salary.da / 12, 2)
    full_special = round(salary.special_allowance / 12, 2)
    full_conveyance = round(salary.conveyance / 12, 2)
    full_medical = round(salary.medical_allowance / 12, 2)

    # Attendance-adjusted components
    basic = round(full_basic * attendance_ratio, 2)
    hra = round(full_hra * attendance_ratio, 2)
    da = round(full_da * attendance_ratio, 2)
    special = round(full_special * attendance_ratio, 2)
    conveyance = round(full_conveyance * attendance_ratio, 2)
    medical = round(full_medical * attendance_ratio, 2)

    gross = basic + hra + da + special + conveyance + medical

    # LOP deduction (additional deduction on top of attendance adjustment)
    full_gross = full_basic + full_hra + full_da + full_special + full_conveyance + full_medical
    per_day = full_gross / working_days if working_days > 0 else 0
    lop_deduction = round(per_day * lop_days, 2)

    gross_after_lop = gross - lop_deduction

    # Statutory deductions (based on actual pay)
    epf = calculate_epf(basic)
    esi = calculate_esi(gross_after_lop)
    pt = calculate_professional_tax(gross_after_lop)

    # TDS
    tax_decl = TaxDeclaration.query.filter_by(emp_id=emp.emp_id).first()
    regime = tax_decl.regime if tax_decl else 'new'
    annual_gross = salary.ctc_annual
    annual_exemptions = 0
    if tax_decl and regime == 'old':
        annual_exemptions = (tax_decl.section_80c + tax_decl.section_80d +
                             tax_decl.section_80e + tax_decl.section_80g +
                             tax_decl.hra_exemption + tax_decl.lta_claimed)
    annual_taxable = max(annual_gross - annual_exemptions - (salary.basic_pay * 0.12), 0)
    tds = calculate_tds_monthly(annual_taxable, regime)

    total_deductions = epf + esi + pt + tds + lop_deduction
    net_pay = round(gross - total_deductions, 2)

    payslip = Payslip(
        run_id=run.run_id,
        emp_id=emp.emp_id,
        org_id=emp.org_id,
        month=month,
        year=year,
        basic_pay=basic,
        hra=hra,
        da=da,
        special_allowance=special,
        conveyance=conveyance,
        medical_allowance=medical,
        gross_pay=gross,
        epf_deduction=epf,
        esi_deduction=esi,
        professional_tax=pt,
        tds=tds,
        lop_deduction=lop_deduction,
        total_deductions=total_deductions,
        net_pay=net_pay
    )
    db.session.add(payslip)
    return payslip


# ────────────────────────────────────────────
# JWT AUTH API ENDPOINT
# ────────────────────────────────────────────
@app.route('/api/v1/auth/token', methods=['POST'])
def api_auth_token():
    """Issue a JWT access token.

    Request body (JSON):
        { "username": "admin", "password": "admin123" }
    OR for employees:
        { "employee_id": "EMP1", "password": "rahul123" }

    Response:
        {
            "access_token": "eyJ...",
            "token_type": "Bearer",
            "role": "hr_admin",
            "org_id": 1,
            "expires_in": 28800
        }
    """
    data = request.get_json(silent=True) or {}
    username = data.get('username') or data.get('employee_id')
    password = data.get('password')

    if not username or not password:
        return jsonify({'error': 'username/employee_id and password are required.'}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'error': 'Invalid credentials.'}), 401

    token = generate_jwt(user)
    expiry_hours = app.config.get('JWT_EXPIRY_HOURS', 8)

    return jsonify({
        'access_token': token,
        'token_type': 'Bearer',
        'user_id': user.id,
        'username': user.username,
        'role': user.role,
        'org_id': user.org_id,
        'org_name': user.organization.name if user.organization else None,
        'expires_in': expiry_hours * 3600
    })


# ────────────────────────────────────────────
# API ROUTES — Multi-Tenant & Data Isolation
# ────────────────────────────────────────────
@app.route('/api/v1/employee/data', methods=['GET'])
@jwt_or_session_required
def get_employee_data():
    """getEmployeeData controller.

    • Employee role  → strict Self-Only policy (user_id + org_id).
    • Admin/Finance  → may optionally pass ?emp_id= but only within own org.

    Supports both JWT (Authorization: Bearer <token>) and session auth.
    """
    user = g.current_api_user
    claims = g.jwt_claims

    if user.role == 'employee':
        # Self-Only: WHERE user_id = sub AND org_id = jwt.org_id
        emp = Employee.query.filter_by(
            user_id=claims['sub'],
            org_id=claims['org_id']
        ).first()
        if not emp:
            return jsonify({"error": "Employee profile not found."}), 404
    else:
        # Admin/finance: optionally look up a specific employee in their org
        target_emp_id = request.args.get('emp_id', type=int)
        if target_emp_id:
            # Row-Level: WHERE emp_id = ? AND org_id = jwt.org_id
            emp = Employee.query.filter_by(
                emp_id=target_emp_id,
                org_id=claims['org_id']
            ).first()
            if not emp:
                return jsonify({"error": "Employee not found in your organization."}), 404
        else:
            return jsonify({"error": "emp_id query parameter required for admin lookup."}), 400

    return jsonify({
        "emp_id": emp.emp_id,
        "full_name": emp.full_name,
        "email": emp.email,
        "phone": emp.phone,
        "department": emp.department,
        "designation": emp.designation,
        "date_of_joining": str(emp.date_of_joining),
        "status": emp.status,
        "organization": emp.organization.name if emp.organization else None
    })


@app.route('/api/v1/receipt/generate', methods=['GET'])
@jwt_or_session_required
def api_generate_receipt():
    """generateReceipt controller — generates a PDF receipt and returns it.

    Security checks:
      1. Employee → can only generate receipts for their own payslips.
      2. Admin    → can generate for any employee within their own org_id.

    Query params:
      - slip_id (required): The payslip ID to generate a receipt for.
      - format (optional): 'pdf' (default) or 'json' for metadata only.
    """
    user = g.current_api_user
    claims = g.jwt_claims

    slip_id = request.args.get('slip_id', type=int)
    output_format = request.args.get('format', 'pdf')

    if not slip_id:
        return jsonify({"error": "slip_id query parameter is required."}), 400

    # Row-Level: scope payslip lookup to the user's org
    slip = Payslip.query.filter_by(slip_id=slip_id, org_id=claims['org_id']).first()
    if not slip:
        return jsonify({"error": "Payslip not found in your organization."}), 404

    emp = Employee.query.get(slip.emp_id)

    # ── Row-Level Security ──
    if user.role == 'employee':
        if emp.user_id != claims['sub']:
            return jsonify({"error": "IDOR Prevention: Cannot access another employee's receipt."}), 403
    else:
        if emp.org_id != claims['org_id']:
            return jsonify({"error": "Tenant Isolation: Employee does not belong to your organization."}), 403

    if output_format == 'json':
        return jsonify({
            "status": "success",
            "message": "Receipt generation authorised.",
            "receipt_details": {
                "slip_id": slip.slip_id,
                "employee": emp.full_name,
                "month": slip.month,
                "year": slip.year,
                "gross_pay": slip.gross_pay,
                "total_deductions": slip.total_deductions,
                "net_pay": slip.net_pay,
                "download_url": url_for('download_payslip_pdf', slip_id=slip.slip_id, _external=True)
            }
        })

    # Generate PDF
    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, is_current=True).first()
    org_name = emp.organization.name if emp.organization else 'Unknown Organization'
    pdf_bytes = generate_payslip_pdf(slip, emp, salary, org_name)

    months = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    month_name = months[slip.month] if 1 <= slip.month <= 12 else str(slip.month)

    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = (
        f'attachment; filename="payslip_{emp.full_name.replace(" ", "_")}_{month_name}_{slip.year}.pdf"'
    )
    return response


@app.route('/api/v1/employees', methods=['GET'])
@org_admin_required
def api_list_employees():
    """List all employees in the current user's organization.
    Admin/Finance only — org-scoped.
    """
    user = g.current_api_user
    employees = Employee.query.filter_by(org_id=user.org_id, status='active').all()
    return jsonify({
        "org_id": user.org_id,
        "count": len(employees),
        "employees": [{
            "emp_id": e.emp_id,
            "full_name": e.full_name,
            "email": e.email,
            "department": e.department,
            "designation": e.designation,
            "status": e.status
        } for e in employees]
    })


# ────────────────────────────────────────────
# PUBLIC ROUTES (No Login Required)
# ────────────────────────────────────────────
@app.route('/')
def home():
    return render_template('home.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        login_mode = request.form.get('login_mode', 'admin')
        password = request.form.get('password')

        if login_mode == 'employee':
            # Employee login — uses Employee ID (e.g. EMP1)
            employee_id = request.form.get('employee_id', '').strip()
            if not employee_id:
                flash('Please enter your Employee ID.', 'error')
                return render_template('login.html')
            user = User.query.filter_by(username=employee_id, role='employee').first()
            if user and user.check_password(password):
                login_user(user, remember=True)
                flash('Welcome back!', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            else:
                flash('Invalid Employee ID or password.', 'error')
        else:
            # Admin/Finance login — uses username
            username = request.form.get('username', '').strip()
            if not username:
                flash('Please enter your username.', 'error')
                return render_template('login.html')
            user = User.query.filter_by(username=username).first()
            # Block employees from using admin login form
            if user and user.role == 'employee':
                flash('Employees must use the Employee Login tab.', 'error')
                return render_template('login.html')
            if user and user.check_password(password):
                login_user(user, remember=True)
                flash('Welcome back!', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            else:
                flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        org_name = request.form.get('organization', 'My Organization')

        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return render_template('register.html')

        # Find-or-create Organisation (tenant)
        org = Organization.query.filter_by(name=org_name).first()
        if not org:
            org = Organization(name=org_name)
            db.session.add(org)
            db.session.flush()

        user = User(username=username, email=email, role='hr_admin', org_id=org.org_id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))


# ────────────────────────────────────────────
# PROTECTED ROUTES (Login Required)
# ────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        recent_slips = Payslip.query.filter_by(emp_id=emp.emp_id).order_by(Payslip.year.desc(), Payslip.month.desc()).limit(5).all() if emp else []
        return render_template('dashboard.html', employee=emp, recent_slips=recent_slips, role='employee')

    # Admin / Finance — scoped to own org_id
    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    recent_runs = PayrollRun.query.filter_by(org_id=current_user.org_id).order_by(PayrollRun.created_at.desc()).limit(5).all()
    total_employees = len(employees)
    pending_runs = PayrollRun.query.filter_by(status='pending', org_id=current_user.org_id).count()
    finalized_runs = PayrollRun.query.filter_by(status='finalized', org_id=current_user.org_id).count()

    # Calculate total payroll cost from latest finalized run
    latest_run = PayrollRun.query.filter_by(status='finalized', org_id=current_user.org_id).order_by(PayrollRun.created_at.desc()).first()
    total_payroll = latest_run.total_net if latest_run else 0

    return render_template('dashboard.html',
                           total_employees=total_employees,
                           pending_runs=pending_runs,
                           finalized_runs=finalized_runs,
                           total_payroll=total_payroll,
                           recent_runs=recent_runs,
                           employees=employees, role='admin')


@app.route('/payslips')
@login_required
def payslips():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        slips = Payslip.query.filter_by(emp_id=emp.emp_id).order_by(Payslip.year.desc(), Payslip.month.desc()).all() if emp else []
        return render_template('payslips.html', payslips=slips, employees=[emp] if emp else [])

    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    emp_ids = [e.emp_id for e in employees]
    slips = Payslip.query.filter(Payslip.emp_id.in_(emp_ids)).order_by(Payslip.year.desc(), Payslip.month.desc()).all() if emp_ids else []
    return render_template('payslips.html', payslips=slips, employees=employees)


@app.route('/payslip/download/<int:slip_id>')
@login_required
def download_payslip(slip_id):
    """Generate a printable HTML payslip for download — with full RBAC checks."""
    slip = Payslip.query.get_or_404(slip_id)
    emp = Employee.query.get(slip.emp_id)

    # ── Row-Level Security ──
    if current_user.role == 'employee':
        if emp.user_id != current_user.id:
            flash("Unauthorized access.", "error")
            return redirect(url_for('dashboard'))
    else:
        if emp.org_id != current_user.org_id:
            flash("Unauthorized access.", "error")
            return redirect(url_for('dashboard'))

    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, is_current=True).first()
    months = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    month_name = months[slip.month] if 1 <= slip.month <= 12 else str(slip.month)
    return render_template('payslip_download.html',
                           slip=slip, emp=emp, salary=salary, month_name=month_name,
                           org=get_org_name())


@app.route('/payslip/pdf/<int:slip_id>')
@login_required
def download_payslip_pdf(slip_id):
    """Generate a true PDF payslip using ReportLab — with full RBAC checks.

    Row-Level Security:
      - Employee: can only download their own payslip
      - Admin: can download any payslip in their organization
    """
    slip = Payslip.query.get_or_404(slip_id)
    emp = Employee.query.get(slip.emp_id)

    # ── Row-Level Security ──
    if current_user.role == 'employee':
        if emp.user_id != current_user.id:
            flash("Unauthorized access.", "error")
            return redirect(url_for('dashboard'))
    else:
        if emp.org_id != current_user.org_id:
            flash("Unauthorized access.", "error")
            return redirect(url_for('dashboard'))

    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, is_current=True).first()
    org_name = get_org_name()

    pdf_bytes = generate_payslip_pdf(slip, emp, salary, org_name)

    months = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    month_name = months[slip.month] if 1 <= slip.month <= 12 else str(slip.month)

    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = (
        f'attachment; filename="payslip_{emp.full_name.replace(" ", "_")}_{month_name}_{slip.year}.pdf"'
    )
    return response


@app.route('/attendance', methods=['GET', 'POST'])
@login_required
def attendance():
    """View and manage attendance records.

    GET: View attendance for a month/year.
    POST (admin only): Add or update attendance for an employee.
    """
    if request.method == 'POST' and current_user.role != 'employee':
        # Admin/Finance: add or update attendance
        emp_id = request.form.get('emp_id', type=int)
        att_month = request.form.get('month', type=int)
        att_year = request.form.get('year', type=int)
        total_working = request.form.get('total_working_days', type=int, default=22)
        days_present = request.form.get('days_present', type=int, default=22)
        days_absent = request.form.get('days_absent', type=int, default=0)
        lop_days = request.form.get('lop_days', type=int, default=0)
        overtime_hours = request.form.get('overtime_hours', type=float, default=0)

        # Validate employee belongs to this org
        emp = Employee.query.filter_by(emp_id=emp_id, org_id=current_user.org_id).first()
        if not emp:
            flash('Employee not found in your organization.', 'error')
            return redirect(url_for('attendance'))

        # Find or create attendance record
        existing = AttendanceLog.query.filter_by(
            emp_id=emp_id, month=att_month, year=att_year
        ).first()

        if existing:
            existing.total_working_days = total_working
            existing.days_present = days_present
            existing.days_absent = days_absent
            existing.lop_days = lop_days
            existing.overtime_hours = overtime_hours
            flash(f'Attendance updated for {emp.full_name} — {att_month}/{att_year}', 'success')
        else:
            new_log = AttendanceLog(
                emp_id=emp_id,
                org_id=current_user.org_id,
                month=att_month,
                year=att_year,
                total_working_days=total_working,
                days_present=days_present,
                days_absent=days_absent,
                lop_days=lop_days,
                overtime_hours=overtime_hours
            )
            db.session.add(new_log)
            flash(f'Attendance added for {emp.full_name} — {att_month}/{att_year}', 'success')

        db.session.commit()
        return redirect(url_for('attendance', month=att_month, year=att_year))

    # GET request
    month = request.args.get('month', type=int, default=datetime.now().month)
    year = request.args.get('year', type=int, default=datetime.now().year)

    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        logs = AttendanceLog.query.filter_by(emp_id=emp.emp_id, month=month, year=year).all() if emp else []
        employees = [emp] if emp else []
    else:
        employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
        emp_ids = [e.emp_id for e in employees]
        logs = AttendanceLog.query.filter(AttendanceLog.emp_id.in_(emp_ids), AttendanceLog.month==month, AttendanceLog.year==year).all() if emp_ids else []

    months = ['January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('attendance.html',
                           logs=logs, employees=employees,
                           selected_month=month, selected_year=year,
                           months=months)


@app.route('/tax-deductions')
@login_required
def tax_deductions():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        declarations = TaxDeclaration.query.filter_by(emp_id=emp.emp_id).all() if emp else []
    else:
        employees = Employee.query.filter_by(org_id=current_user.org_id).all()
        emp_ids = [e.emp_id for e in employees]
        declarations = TaxDeclaration.query.filter(TaxDeclaration.emp_id.in_(emp_ids)).all() if emp_ids else []

    return render_template('tax_deductions.html', declarations=declarations)


@app.route('/reimbursements')
@login_required
def reimbursements():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        claims = Reimbursement.query.filter_by(emp_id=emp.emp_id).order_by(Reimbursement.submitted_at.desc()).all() if emp else []
    else:
        employees = Employee.query.filter_by(org_id=current_user.org_id).all()
        emp_ids = [e.emp_id for e in employees]
        claims = Reimbursement.query.filter(Reimbursement.emp_id.in_(emp_ids)).order_by(Reimbursement.submitted_at.desc()).all() if emp_ids else []

    return render_template('reimbursements.html', claims=claims)


@app.route('/documents')
@login_required
def documents():
    return render_template('documents.html')


@app.route('/people')
@login_required
@role_required('hr_admin', 'finance')
def people():
    employees = Employee.query.filter_by(org_id=current_user.org_id).all()
    return render_template('people.html', employees=employees)


@app.route('/people/add', methods=['POST'])
@login_required
@role_required('hr_admin')
def add_employee():
    emp = Employee(
        first_name=request.form.get('first_name'),
        last_name=request.form.get('last_name'),
        email=request.form.get('email'),
        phone=request.form.get('phone'),
        pan_number=request.form.get('pan_number'),
        aadhaar_number=request.form.get('aadhaar_number'),
        bank_account=request.form.get('bank_account'),
        bank_ifsc=request.form.get('bank_ifsc'),
        bank_name=request.form.get('bank_name'),
        department=request.form.get('department'),
        designation=request.form.get('designation'),
        org_id=current_user.org_id
    )
    db.session.add(emp)
    db.session.flush()

    # Create User account for employee — username is EMP<id>
    emp_user = User(
        username=f"EMP{emp.emp_id}",
        email=emp.email,
        role='employee',
        org_id=current_user.org_id
    )
    emp_user.set_password(f"{emp.first_name.lower()}123")
    db.session.add(emp_user)
    db.session.flush()
    emp.user_id = emp_user.id

    ctc = float(request.form.get('ctc', 600000))
    basic = ctc * 0.40
    hra = basic * 0.50
    da = ctc * 0.05
    special = ctc - basic - hra - da - (ctc * 0.05) - (ctc * 0.03)
    conveyance = ctc * 0.05
    medical = ctc * 0.03

    salary = SalaryStructure(
        emp_id=emp.emp_id,
        org_id=current_user.org_id,
        ctc_annual=ctc,
        basic_pay=basic,
        hra=hra,
        da=da,
        special_allowance=max(special, 0),
        conveyance=conveyance,
        medical_allowance=medical
    )
    db.session.add(salary)
    db.session.commit()
    flash(f'Employee {emp.full_name} added successfully! Login: EMP{emp.emp_id} / {emp.first_name.lower()}123', 'success')
    return redirect(url_for('people'))


@app.route('/run-payroll', methods=['GET', 'POST'])
@login_required
@role_required('hr_admin', 'finance')
def run_payroll():
    if request.method == 'POST':
        month = int(request.form.get('month', datetime.now().month))
        year = int(request.form.get('year', datetime.now().year))

        # Check if payroll already run — scoped to org
        existing = PayrollRun.query.filter_by(month=month, year=year, org_id=current_user.org_id).first()
        if existing and existing.status == 'finalized':
            flash(f'Payroll for {month}/{year} is already finalized.', 'error')
            return redirect(url_for('run_payroll'))

        run = existing or PayrollRun(
            month=month,
            year=year,
            created_by=current_user.id,
            org_id=current_user.org_id
        )
        if not existing:
            db.session.add(run)
            db.session.flush()

        # Clear old payslips for this run
        Payslip.query.filter_by(run_id=run.run_id).delete()

        employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
        total_gross = 0
        total_ded = 0
        total_net = 0
        count = 0

        for emp in employees:
            slip = generate_payslip_for_employee(emp, run, month, year)
            if slip:
                total_gross += slip.gross_pay
                total_ded += slip.total_deductions
                total_net += slip.net_pay
                count += 1

        run.total_gross = round(total_gross, 2)
        run.total_deductions = round(total_ded, 2)
        run.total_net = round(total_net, 2)
        run.employee_count = count
        run.status = 'pending'
        db.session.commit()

        flash(f'Payroll calculated for {count} employees. Total Net: ₹{run.total_net:,.2f}', 'success')
        return redirect(url_for('run_payroll'))

    runs = PayrollRun.query.filter_by(org_id=current_user.org_id).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    return render_template('run_payroll.html', runs=runs, employees=employees)


@app.route('/approve-payroll/<int:run_id>', methods=['POST'])
@login_required
@role_required('hr_admin', 'finance')
def approve_payroll(run_id):
    run = PayrollRun.query.get_or_404(run_id)
    # Org isolation — admins can only approve their own org's runs
    if run.org_id != current_user.org_id:
        flash('Unauthorized — this payroll run does not belong to your organization.', 'error')
        return redirect(url_for('run_payroll'))
    run.status = 'finalized'
    run.approver_id = current_user.id
    run.finalized_at = datetime.utcnow()
    db.session.commit()
    flash(f'Payroll for {run.month}/{run.year} finalized!', 'success')
    return redirect(url_for('run_payroll'))


@app.route('/reports')
@login_required
@role_required('hr_admin', 'finance')
def reports():
    runs = PayrollRun.query.filter_by(status='finalized', org_id=current_user.org_id).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    return render_template('reports.html', runs=runs)


@app.route('/reports/payroll-summary')
@login_required
@role_required('hr_admin', 'finance')
def report_payroll_summary():
    """Detailed payroll summary report with per-employee breakdown"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    run = PayrollRun.query.filter_by(month=month, year=year, status='finalized', org_id=current_user.org_id).first()
    slips = Payslip.query.filter_by(month=month, year=year, run_id=run.run_id).all() if run else []
    runs = PayrollRun.query.filter_by(status='finalized', org_id=current_user.org_id).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_payroll_summary.html',
                           run=run, slips=slips, runs=runs,
                           selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/statutory-compliance')
@login_required
@role_required('hr_admin', 'finance')
def report_statutory():
    """EPF, ESI, PT, TDS filing data for government portals"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    # Scope to org: only show payslips from runs owned by this org
    org_run_ids = [r.run_id for r in PayrollRun.query.filter_by(org_id=current_user.org_id).all()]
    slips = Payslip.query.filter(Payslip.run_id.in_(org_run_ids), Payslip.month==month, Payslip.year==year).all() if org_run_ids else []
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_statutory.html',
                           slips=slips, selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/department-cost')
@login_required
@role_required('hr_admin', 'finance')
def report_dept_cost():
    """Department-wise payroll cost breakdown"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    org_run_ids = [r.run_id for r in PayrollRun.query.filter_by(org_id=current_user.org_id).all()]
    slips = Payslip.query.filter(Payslip.run_id.in_(org_run_ids), Payslip.month==month, Payslip.year==year).all() if org_run_ids else []
    # Group by department
    dept_data = {}
    for slip in slips:
        dept = slip.employee.department
        if dept not in dept_data:
            dept_data[dept] = {'count': 0, 'gross': 0, 'deductions': 0, 'net': 0}
        dept_data[dept]['count'] += 1
        dept_data[dept]['gross'] += slip.gross_pay
        dept_data[dept]['deductions'] += slip.total_deductions
        dept_data[dept]['net'] += slip.net_pay
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_dept_cost.html',
                           dept_data=dept_data, slips=slips,
                           selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/ytd-summary')
@login_required
@role_required('hr_admin', 'finance')
def report_ytd():
    """Year-to-date cumulative earnings per employee"""
    year = request.args.get('year', type=int, default=2026)
    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    ytd_data = []
    for emp in employees:
        slips = Payslip.query.filter_by(emp_id=emp.emp_id, year=year).all()
        if slips:
            ytd_data.append({
                'employee': emp,
                'months': len(slips),
                'total_gross': sum(s.gross_pay for s in slips),
                'total_epf': sum(s.epf_deduction for s in slips),
                'total_esi': sum(s.esi_deduction for s in slips),
                'total_pt': sum(s.professional_tax for s in slips),
                'total_tds': sum(s.tds for s in slips),
                'total_deductions': sum(s.total_deductions for s in slips),
                'total_net': sum(s.net_pay for s in slips),
            })
    return render_template('report_ytd.html', ytd_data=ytd_data, selected_year=year)


@app.route('/reports/bank-transfer')
@login_required
@role_required('hr_admin', 'finance')
def report_bank_transfer():
    """NEFT/RTGS bank transfer file details"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    org_run_ids = [r.run_id for r in PayrollRun.query.filter_by(org_id=current_user.org_id).all()]
    slips = Payslip.query.filter(Payslip.run_id.in_(org_run_ids), Payslip.month==month, Payslip.year==year).all() if org_run_ids else []
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_bank_transfer.html',
                           slips=slips, selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/audit-trail')
@login_required
@role_required('hr_admin', 'finance')
def report_audit():
    """Complete audit log of all payroll actions — scoped to organization"""
    logs = AuditLog.query.filter_by(org_id=current_user.org_id).order_by(AuditLog.timestamp.desc()).limit(200).all()
    runs = PayrollRun.query.filter_by(org_id=current_user.org_id).order_by(PayrollRun.created_at.desc()).all()
    return render_template('report_audit.html', audit_logs=logs, runs=runs)


# ────────────────────────────────────────────
# ADDITIONAL SIDEBAR PAGES
# ────────────────────────────────────────────
@app.route('/help')
@login_required
def help_page():
    return render_template('help.html')


@app.route('/refer-earn')
@login_required
def refer_earn():
    return render_template('refer_earn.html')


@app.route('/pay-contractors')
@login_required
@role_required('hr_admin', 'finance')
def pay_contractors():
    return render_template('pay_contractors.html')


@app.route('/insurance')
@login_required
def insurance():
    return render_template('insurance.html')


@app.route('/company-details')
@login_required
@role_required('hr_admin', 'finance')
def company_details():
    return render_template('company_details.html')


@app.route('/settings')
@login_required
@role_required('hr_admin')
def settings():
    rates = StatutoryRate.query.filter_by(is_active=True).all()
    return render_template('settings.html', rates=rates)


@app.route('/choose-organization')
@login_required
def choose_organization():
    return render_template('choose_organization.html')


# ────────────────────────────────────────────
# SEED DATA
# ────────────────────────────────────────────
def seed_database():
    """Create demo data for testing"""
    if User.query.first():
        return

    # ── Create Organization (Tenant) ──
    org = Organization(name='LEARNTODAY DIGITAL PRIVATE LIMITED')
    db.session.add(org)
    db.session.flush()

    # Admin user
    admin = User(username='admin', email='admin@learntoday.com', role='hr_admin',
                 org_id=org.org_id)
    admin.set_password('admin123')
    db.session.add(admin)

    # Finance user
    finance = User(username='finance', email='finance@learntoday.com', role='finance',
                   org_id=org.org_id)
    finance.set_password('finance123')
    db.session.add(finance)

    db.session.flush()

    # Employees
    emp_data = [
        ('Rahul', 'Sharma', 'rahul@learntoday.com', 'Engineering', 'Senior Developer', 1200000),
        ('Priya', 'Patel', 'priya@learntoday.com', 'Engineering', 'Full Stack Developer', 900000),
        ('Amit', 'Kumar', 'amit@learntoday.com', 'Design', 'UI/UX Designer', 750000),
        ('Sneha', 'Gupta', 'sneha@learntoday.com', 'Marketing', 'Marketing Manager', 850000),
        ('Vikram', 'Singh', 'vikram@learntoday.com', 'HR', 'HR Executive', 600000),
        ('Neha', 'Reddy', 'neha@learntoday.com', 'Finance', 'Accountant', 700000),
        ('Arjun', 'Nair', 'arjun@learntoday.com', 'Engineering', 'Backend Developer', 1000000),
        ('Kavita', 'Joshi', 'kavita@learntoday.com', 'Operations', 'Operations Lead', 800000),
    ]

    for fn, ln, email, dept, desig, ctc in emp_data:
        emp = Employee(
            first_name=fn, last_name=ln, email=email,
            phone='9876543210', pan_number='ABCDE1234F',
            aadhaar_number='123456789012',
            bank_account='1234567890', bank_ifsc='HDFC0001234',
            bank_name='HDFC Bank',
            department=dept, designation=desig,
            date_of_joining=date(2024, 1, 15),
            org_id=org.org_id
        )
        db.session.add(emp)
        db.session.flush()

        emp_user = User(
            username=f"EMP{emp.emp_id}",
            email=emp.email,
            role='employee',
            org_id=org.org_id
        )
        emp_user.set_password(f"{emp.first_name.lower()}123")
        db.session.add(emp_user)
        db.session.flush()
        emp.user_id = emp_user.id

        basic = ctc * 0.40
        hra = basic * 0.50
        da = ctc * 0.05
        special = ctc * 0.22
        conveyance = ctc * 0.05
        medical = ctc * 0.03

        salary = SalaryStructure(
            emp_id=emp.emp_id,
            org_id=org.org_id,
            ctc_annual=ctc,
            basic_pay=basic,
            hra=hra,
            da=da,
            special_allowance=special,
            conveyance=conveyance,
            medical_allowance=medical
        )
        db.session.add(salary)

        # Attendance for recent months
        for m in range(1, 4):
            att = AttendanceLog(
                emp_id=emp.emp_id,
                org_id=org.org_id,
                month=m, year=2026,
                total_working_days=22,
                days_present=21 if m == 2 else 22,
                days_absent=1 if m == 2 else 0,
                lop_days=0
            )
            db.session.add(att)

        # Tax declaration
        tax = TaxDeclaration(
            emp_id=emp.emp_id,
            org_id=org.org_id,
            financial_year='2025-2026',
            regime='new',
            section_80c=150000,
            section_80d=25000
        )
        db.session.add(tax)

    db.session.flush()

    # Reimbursement demo data
    all_emps = Employee.query.filter_by(org_id=org.org_id).all()
    reimbursement_data = [
        (all_emps[0].emp_id, 'travel', 4500, 'Client site visit — Bangalore to Chennai', date(2026, 1, 10), 'approved'),
        (all_emps[0].emp_id, 'food', 1200, 'Team lunch — project celebration', date(2026, 2, 14), 'approved'),
        (all_emps[1].emp_id, 'medical', 8500, 'Annual health check-up at Apollo Hospital', date(2026, 1, 22), 'approved'),
        (all_emps[2].emp_id, 'travel', 3200, 'Design conference — Hyderabad', date(2026, 2, 5), 'pending'),
        (all_emps[3].emp_id, 'other', 15000, 'Online certification course — Google Analytics', date(2026, 1, 18), 'approved'),
        (all_emps[4].emp_id, 'travel', 2800, 'Campus recruitment drive — Pune', date(2026, 3, 1), 'pending'),
        (all_emps[5].emp_id, 'food', 950, 'Working dinner — month-end closing', date(2026, 2, 28), 'approved'),
        (all_emps[6].emp_id, 'other', 6000, 'AWS certification exam fee', date(2026, 2, 20), 'approved'),
        (all_emps[7].emp_id, 'medical', 3500, 'Physiotherapy sessions — WFH ergonomics', date(2026, 3, 5), 'pending'),
    ]
    for eid, cat, amt, desc, rdate, status in reimbursement_data:
        emp_obj = Employee.query.get(eid)
        reimb = Reimbursement(
            emp_id=eid,
            org_id=org.org_id,
            category=cat,
            amount=amt,
            description=desc,
            receipt_date=rdate,
            status=status,
            approved_at=datetime.utcnow() if status == 'approved' else None
        )
        db.session.add(reimb)

    # Statutory rates
    rates = [
        ('EPF', 'Employee Provident Fund - Employee Share', 12.0, 15000),
        ('EPF_EMPLOYER', 'Employer PF Contribution', 12.0, 15000),
        ('ESI_EMPLOYEE', 'ESI Employee Share', 0.75, 21000),
        ('ESI_EMPLOYER', 'ESI Employer Share', 3.25, 21000),
        ('PT', 'Professional Tax', 200, None),
    ]
    for rt, desc, pct, limit in rates:
        rate = StatutoryRate(rate_type=rt, description=desc, percentage=pct, max_limit=limit)
        db.session.add(rate)

    db.session.commit()

    # Generate a payroll run for February
    admin_user = User.query.filter_by(username='admin').first()
    run = PayrollRun(month=2, year=2026, created_by=admin_user.id, status='finalized',
                     finalized_at=datetime.utcnow(), org_id=org.org_id)
    db.session.add(run)
    db.session.flush()

    employees = Employee.query.filter_by(org_id=org.org_id).all()
    t_gross = t_ded = t_net = 0
    for emp in employees:
        slip = generate_payslip_for_employee(emp, run, 2, 2026)
        if slip:
            t_gross += slip.gross_pay
            t_ded += slip.total_deductions
            t_net += slip.net_pay
    run.total_gross = round(t_gross, 2)
    run.total_deductions = round(t_ded, 2)
    run.total_net = round(t_net, 2)
    run.employee_count = len(employees)
    db.session.commit()


# ────────────────────────────────────────────
# APP STARTUP (works both locally and on Vercel)
# ────────────────────────────────────────────
_db_initialized = False


@app.before_request
def initialize_database():
    """Ensure the database is created and seeded on the first request.
    This is necessary for Vercel serverless where __main__ never runs."""
    global _db_initialized
    if not _db_initialized:
        db.create_all()
        seed_database()
        _db_initialized = True


if __name__ == '__main__':
    app.run(debug=True, port=5000)