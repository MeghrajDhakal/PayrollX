from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """Login/Authentication user model"""
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='employee')  # employee, hr_admin, finance
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    organization = db.Column(db.String(200), default='LEARNTODAY DIGITAL PRIVATE LIMITED')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Employee(db.Model):
    """Employee Master Data — stores PAN, Aadhaar, Bank Details"""
    __tablename__ = 'employees'
    emp_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(15))
    pan_number = db.Column(db.String(10))
    aadhaar_number = db.Column(db.String(12))
    bank_account = db.Column(db.String(20))
    bank_ifsc = db.Column(db.String(11))
    bank_name = db.Column(db.String(100))
    department = db.Column(db.String(80))
    designation = db.Column(db.String(80))
    date_of_joining = db.Column(db.Date, default=date.today)
    date_of_exit = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default='active')  # active, inactive, exited
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    salary_structures = db.relationship('SalaryStructure', backref='employee', lazy=True)
    attendance_logs = db.relationship('AttendanceLog', backref='employee', lazy=True)
    payslips = db.relationship('Payslip', backref='employee', lazy=True)
    tax_declaration = db.relationship('TaxDeclaration', backref='employee', uselist=False)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class SalaryStructure(db.Model):
    """Breakdown of Basic, HRA, DA, and custom allowances"""
    __tablename__ = 'salary_structures'
    struct_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id'), nullable=False)
    ctc_annual = db.Column(db.Float, nullable=False)
    basic_pay = db.Column(db.Float, nullable=False)
    hra = db.Column(db.Float, nullable=False)
    da = db.Column(db.Float, default=0)
    special_allowance = db.Column(db.Float, default=0)
    conveyance = db.Column(db.Float, default=0)
    medical_allowance = db.Column(db.Float, default=0)
    effective_from = db.Column(db.Date, default=date.today)
    effective_to = db.Column(db.Date, nullable=True)
    is_current = db.Column(db.Boolean, default=True)


class AttendanceLog(db.Model):
    """Monthly synced data for LOP and Overtime calculations"""
    __tablename__ = 'attendance_logs'
    log_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id'), nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    total_working_days = db.Column(db.Integer, default=22)
    days_present = db.Column(db.Integer, default=22)
    days_absent = db.Column(db.Integer, default=0)
    lop_days = db.Column(db.Integer, default=0)
    overtime_hours = db.Column(db.Float, default=0)


class StatutoryRate(db.Model):
    """Configurable table for EPF (12%), ESI, Tax slabs"""
    __tablename__ = 'statutory_rates'
    rate_id = db.Column(db.Integer, primary_key=True)
    rate_type = db.Column(db.String(50), nullable=False)  # EPF, ESI, PT, TDS
    description = db.Column(db.String(200))
    percentage = db.Column(db.Float, nullable=False)
    max_limit = db.Column(db.Float, nullable=True)
    effective_from = db.Column(db.Date, default=date.today)
    is_active = db.Column(db.Boolean, default=True)


class PayrollRun(db.Model):
    """Tracks lifecycle: Draft → Pending → Finalized"""
    __tablename__ = 'payroll_runs'
    run_id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default='draft')  # draft, pending, approved, finalized
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    approver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    total_gross = db.Column(db.Float, default=0)
    total_deductions = db.Column(db.Float, default=0)
    total_net = db.Column(db.Float, default=0)
    employee_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finalized_at = db.Column(db.DateTime, nullable=True)

    payslips = db.relationship('Payslip', backref='payroll_run', lazy=True)


class Payslip(db.Model):
    """Per-employee payslip with gross, deductions, net pay"""
    __tablename__ = 'payslips'
    slip_id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('payroll_runs.run_id'), nullable=False)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id'), nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    basic_pay = db.Column(db.Float, default=0)
    hra = db.Column(db.Float, default=0)
    da = db.Column(db.Float, default=0)
    special_allowance = db.Column(db.Float, default=0)
    conveyance = db.Column(db.Float, default=0)
    medical_allowance = db.Column(db.Float, default=0)
    gross_pay = db.Column(db.Float, default=0)
    epf_deduction = db.Column(db.Float, default=0)
    esi_deduction = db.Column(db.Float, default=0)
    professional_tax = db.Column(db.Float, default=0)
    tds = db.Column(db.Float, default=0)
    lop_deduction = db.Column(db.Float, default=0)
    total_deductions = db.Column(db.Float, default=0)
    net_pay = db.Column(db.Float, default=0)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaxDeclaration(db.Model):
    """Investment declarations under 80C, 80D for TDS computation"""
    __tablename__ = 'tax_declarations'
    declaration_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id'), nullable=False)
    financial_year = db.Column(db.String(9), nullable=False)  # e.g. 2025-2026
    regime = db.Column(db.String(10), default='new')  # old, new
    section_80c = db.Column(db.Float, default=0)  # PPF, ELSS, LIC etc.
    section_80d = db.Column(db.Float, default=0)  # Health Insurance
    section_80e = db.Column(db.Float, default=0)  # Education Loan
    section_80g = db.Column(db.Float, default=0)  # Donations
    hra_exemption = db.Column(db.Float, default=0)
    lta_claimed = db.Column(db.Float, default=0)
    other_income = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='draft')  # draft, submitted, verified
    submitted_at = db.Column(db.DateTime, nullable=True)


class Reimbursement(db.Model):
    """Employee reimbursement claims"""
    __tablename__ = 'reimbursements'
    claim_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id'), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # travel, medical, food, other
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(300))
    receipt_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected, paid
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)


class AuditLog(db.Model):
    """Tamper-proof record of all actions with timestamps"""
    __tablename__ = 'audit_logs'
    audit_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
