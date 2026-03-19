from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from config import Config
from models import db, User, Employee, SalaryStructure, AttendanceLog, StatutoryRate, PayrollRun, Payslip, TaxDeclaration, Reimbursement, AuditLog
from datetime import datetime, date
from functools import wraps

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access payroll features.'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
    """Generate a complete payslip for one employee"""
    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, is_current=True).first()
    if not salary:
        return None

    attendance = AttendanceLog.query.filter_by(emp_id=emp.emp_id, month=month, year=year).first()
    lop_days = attendance.lop_days if attendance else 0
    working_days = attendance.total_working_days if attendance else 22

    # Monthly components
    basic = round(salary.basic_pay / 12, 2)
    hra = round(salary.hra / 12, 2)
    da = round(salary.da / 12, 2)
    special = round(salary.special_allowance / 12, 2)
    conveyance = round(salary.conveyance / 12, 2)
    medical = round(salary.medical_allowance / 12, 2)

    gross = basic + hra + da + special + conveyance + medical

    # LOP deduction
    per_day = gross / working_days if working_days > 0 else 0
    lop_deduction = round(per_day * lop_days, 2)

    gross_after_lop = gross - lop_deduction

    # Statutory deductions
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
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
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
        org = request.form.get('organization', 'My Organization')

        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return render_template('register.html')

        user = User(username=username, email=email, role='hr_admin', organization=org)
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
    employees = Employee.query.filter_by(status='active').all()
    recent_runs = PayrollRun.query.order_by(PayrollRun.created_at.desc()).limit(5).all()
    total_employees = len(employees)
    pending_runs = PayrollRun.query.filter_by(status='pending').count()
    finalized_runs = PayrollRun.query.filter_by(status='finalized').count()

    # Calculate total payroll cost from latest finalized run
    latest_run = PayrollRun.query.filter_by(status='finalized').order_by(PayrollRun.created_at.desc()).first()
    total_payroll = latest_run.total_net if latest_run else 0

    return render_template('dashboard.html',
                           total_employees=total_employees,
                           pending_runs=pending_runs,
                           finalized_runs=finalized_runs,
                           total_payroll=total_payroll,
                           recent_runs=recent_runs,
                           employees=employees)


@app.route('/payslips')
@login_required
def payslips():
    slips = Payslip.query.order_by(Payslip.year.desc(), Payslip.month.desc()).all()
    employees = Employee.query.filter_by(status='active').all()
    return render_template('payslips.html', payslips=slips, employees=employees)


@app.route('/payslip/download/<int:slip_id>')
@login_required
def download_payslip(slip_id):
    """Generate a printable HTML payslip for download"""
    slip = Payslip.query.get_or_404(slip_id)
    emp = Employee.query.get(slip.emp_id)
    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, is_current=True).first()
    months = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    month_name = months[slip.month] if 1 <= slip.month <= 12 else str(slip.month)
    return render_template('payslip_download.html',
                           slip=slip, emp=emp, salary=salary, month_name=month_name,
                           org=current_user.organization)


@app.route('/attendance')
@login_required
def attendance():
    """View attendance records for all employees"""
    month = request.args.get('month', type=int, default=datetime.now().month)
    year = request.args.get('year', type=int, default=datetime.now().year)
    logs = AttendanceLog.query.filter_by(month=month, year=year).all()
    employees = Employee.query.filter_by(status='active').all()
    months = ['January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('attendance.html',
                           logs=logs, employees=employees,
                           selected_month=month, selected_year=year,
                           months=months)


@app.route('/tax-deductions')
@login_required
def tax_deductions():
    declarations = TaxDeclaration.query.all()
    return render_template('tax_deductions.html', declarations=declarations)


@app.route('/reimbursements')
@login_required
def reimbursements():
    claims = Reimbursement.query.order_by(Reimbursement.submitted_at.desc()).all()
    return render_template('reimbursements.html', claims=claims)


@app.route('/documents')
@login_required
def documents():
    return render_template('documents.html')


@app.route('/people')
@login_required
def people():
    employees = Employee.query.all()
    return render_template('people.html', employees=employees)


@app.route('/people/add', methods=['POST'])
@login_required
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
    )
    db.session.add(emp)
    db.session.flush()

    ctc = float(request.form.get('ctc', 600000))
    basic = ctc * 0.40
    hra = basic * 0.50
    da = ctc * 0.05
    special = ctc - basic - hra - da - (ctc * 0.05) - (ctc * 0.03)
    conveyance = ctc * 0.05
    medical = ctc * 0.03

    salary = SalaryStructure(
        emp_id=emp.emp_id,
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
    flash(f'Employee {emp.full_name} added successfully!', 'success')
    return redirect(url_for('people'))


@app.route('/run-payroll', methods=['GET', 'POST'])
@login_required
def run_payroll():
    if request.method == 'POST':
        month = int(request.form.get('month', datetime.now().month))
        year = int(request.form.get('year', datetime.now().year))

        # Check if payroll already run
        existing = PayrollRun.query.filter_by(month=month, year=year).first()
        if existing and existing.status == 'finalized':
            flash(f'Payroll for {month}/{year} is already finalized.', 'error')
            return redirect(url_for('run_payroll'))

        run = existing or PayrollRun(
            month=month,
            year=year,
            created_by=current_user.id
        )
        if not existing:
            db.session.add(run)
            db.session.flush()

        # Clear old payslips for this run
        Payslip.query.filter_by(run_id=run.run_id).delete()

        employees = Employee.query.filter_by(status='active').all()
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

    runs = PayrollRun.query.order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    employees = Employee.query.filter_by(status='active').all()
    return render_template('run_payroll.html', runs=runs, employees=employees)


@app.route('/approve-payroll/<int:run_id>', methods=['POST'])
@login_required
def approve_payroll(run_id):
    run = PayrollRun.query.get_or_404(run_id)
    run.status = 'finalized'
    run.approver_id = current_user.id
    run.finalized_at = datetime.utcnow()
    db.session.commit()
    flash(f'Payroll for {run.month}/{run.year} finalized!', 'success')
    return redirect(url_for('run_payroll'))


@app.route('/reports')
@login_required
def reports():
    runs = PayrollRun.query.filter_by(status='finalized').order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    return render_template('reports.html', runs=runs)


@app.route('/reports/payroll-summary')
@login_required
def report_payroll_summary():
    """Detailed payroll summary report with per-employee breakdown"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    run = PayrollRun.query.filter_by(month=month, year=year, status='finalized').first()
    slips = Payslip.query.filter_by(month=month, year=year).all() if run else []
    runs = PayrollRun.query.filter_by(status='finalized').order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_payroll_summary.html',
                           run=run, slips=slips, runs=runs,
                           selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/statutory-compliance')
@login_required
def report_statutory():
    """EPF, ESI, PT, TDS filing data for government portals"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    slips = Payslip.query.filter_by(month=month, year=year).all()
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_statutory.html',
                           slips=slips, selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/department-cost')
@login_required
def report_dept_cost():
    """Department-wise payroll cost breakdown"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    slips = Payslip.query.filter_by(month=month, year=year).all()
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
def report_ytd():
    """Year-to-date cumulative earnings per employee"""
    year = request.args.get('year', type=int, default=2026)
    employees = Employee.query.filter_by(status='active').all()
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
def report_bank_transfer():
    """NEFT/RTGS bank transfer file details"""
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    slips = Payslip.query.filter_by(month=month, year=year).all()
    months_list = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
    return render_template('report_bank_transfer.html',
                           slips=slips, selected_month=month, selected_year=year,
                           months=months_list)


@app.route('/reports/audit-trail')
@login_required
def report_audit():
    """Complete audit log of all payroll actions"""
    logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).all()
    # Also generate some synthetic audit entries from payroll runs
    runs = PayrollRun.query.order_by(PayrollRun.created_at.desc()).all()
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
def pay_contractors():
    return render_template('pay_contractors.html')


@app.route('/insurance')
@login_required
def insurance():
    return render_template('insurance.html')


@app.route('/company-details')
@login_required
def company_details():
    return render_template('company_details.html')


@app.route('/settings')
@login_required
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

    # Admin user
    admin = User(username='admin', email='admin@learntoday.com', role='hr_admin',
                 organization='LEARNTODAY DIGITAL PRIVATE LIMITED')
    admin.set_password('admin123')
    db.session.add(admin)

    # Finance user
    finance = User(username='finance', email='finance@learntoday.com', role='finance',
                   organization='LEARNTODAY DIGITAL PRIVATE LIMITED')
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
            date_of_joining=date(2024, 1, 15)
        )
        db.session.add(emp)
        db.session.flush()

        basic = ctc * 0.40
        hra = basic * 0.50
        da = ctc * 0.05
        special = ctc * 0.22
        conveyance = ctc * 0.05
        medical = ctc * 0.03

        salary = SalaryStructure(
            emp_id=emp.emp_id,
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
            financial_year='2025-2026',
            regime='new',
            section_80c=150000,
            section_80d=25000
        )
        db.session.add(tax)

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
                     finalized_at=datetime.utcnow())
    db.session.add(run)
    db.session.flush()

    employees = Employee.query.all()
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

