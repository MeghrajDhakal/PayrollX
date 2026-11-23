from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, date
import bcrypt
from sqlalchemy import event, CheckConstraint, UniqueConstraint, Index
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Ensure SQLite enforces Foreign Key and Check constraints (no-op on PostgreSQL)."""
    if dbapi_connection.__class__.__module__.startswith('sqlite3'):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        except Exception:
            pass


class Organization(db.Model):
    """Tenant/Organization Master Data"""
    __tablename__ = 'organizations'
    org_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    users = db.relationship('User', backref='organization', lazy=True, cascade='all, delete-orphan')
    employees = db.relationship('Employee', backref='organization', lazy=True, cascade='all, delete-orphan')
    payroll_runs = db.relationship('PayrollRun', backref='organization', lazy=True, cascade='all, delete-orphan')


class User(UserMixin, db.Model):
    """Login/Authentication user model with Bcrypt and server-side session tracking"""
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='employee', nullable=False)  # employee, hr_admin, finance
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=True, index=True)
    session_token = db.Column(db.String(64), nullable=True, index=True)  # Server-side invalidation token

    __table_args__ = (
        CheckConstraint("role IN ('employee', 'hr_admin', 'finance')", name='ck_user_role'),
    )

    def set_password(self, password):
        """Hash password using bcrypt (12 rounds)"""
        if not password:
            raise ValueError("Password cannot be empty")
        salt = bcrypt.gensalt(rounds=12)
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    def check_password(self, password):
        """Verify password using bcrypt, with fallback to legacy hashes"""
        if not self.password_hash or not password:
            return False
        # If bcrypt hash
        if self.password_hash.startswith(('$2b$', '$2a$', '$2y$')):
            try:
                return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))
            except Exception:
                return False
        # Fallback to Werkzeug hash for legacy compatibility & transparent upgrade
        try:
            from werkzeug.security import check_password_hash
            if check_password_hash(self.password_hash, password):
                # Upgrade hash to bcrypt
                self.set_password(password)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return True
        except Exception:
            pass
        return False


class Employee(db.Model):
    """Employee Master Data — stores PAN, Aadhaar, Bank Details with Tenant Isolation"""
    __tablename__ = 'employees'
    emp_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=True)
    pan_number = db.Column(db.String(10), nullable=True)
    aadhaar_number = db.Column(db.String(12), nullable=True)
    bank_account = db.Column(db.String(30), nullable=True)
    bank_ifsc = db.Column(db.String(11), nullable=True)
    bank_name = db.Column(db.String(100), nullable=True)
    department = db.Column(db.String(80), nullable=False, default='General')
    designation = db.Column(db.String(80), nullable=False, default='Staff')
    date_of_joining = db.Column(db.Date, default=date.today, nullable=False)
    date_of_exit = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default='active', nullable=False)  # active, inactive, exited
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive', 'exited')", name='ck_employee_status'),
        Index('ix_emp_org_user', 'org_id', 'user_id'),
    )

    salary_structures = db.relationship('SalaryStructure', backref='employee', lazy=True, cascade='all, delete-orphan')
    attendance_logs = db.relationship('AttendanceLog', backref='employee', lazy=True, cascade='all, delete-orphan')
    payslips = db.relationship('Payslip', backref='employee', lazy=True, cascade='all, delete-orphan')
    tax_declaration = db.relationship('TaxDeclaration', backref='employee', uselist=False, cascade='all, delete-orphan')

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class SalaryStructure(db.Model):
    """Breakdown of Basic, HRA, DA, and custom allowances"""
    __tablename__ = 'salary_structures'
    struct_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id', ondelete='CASCADE'), nullable=False, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    ctc_annual = db.Column(db.Float, nullable=False)
    basic_pay = db.Column(db.Float, nullable=False)
    hra = db.Column(db.Float, nullable=False)
    da = db.Column(db.Float, default=0, nullable=False)
    special_allowance = db.Column(db.Float, default=0, nullable=False)
    conveyance = db.Column(db.Float, default=0, nullable=False)
    medical_allowance = db.Column(db.Float, default=0, nullable=False)
    effective_from = db.Column(db.Date, default=date.today, nullable=False)
    effective_to = db.Column(db.Date, nullable=True)
    is_current = db.Column(db.Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint('ctc_annual >= 0', name='ck_salary_ctc_nonneg'),
        CheckConstraint('basic_pay >= 0', name='ck_salary_basic_nonneg'),
        CheckConstraint('hra >= 0', name='ck_salary_hra_nonneg'),
        CheckConstraint('da >= 0', name='ck_salary_da_nonneg'),
        CheckConstraint('special_allowance >= 0', name='ck_salary_special_nonneg'),
        CheckConstraint('conveyance >= 0', name='ck_salary_conveyance_nonneg'),
        CheckConstraint('medical_allowance >= 0', name='ck_salary_medical_nonneg'),
        Index('ix_salary_emp_current', 'emp_id', 'is_current'),
    )


class AttendanceLog(db.Model):
    """Monthly synced data for LOP and Overtime calculations"""
    __tablename__ = 'attendance_logs'
    log_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id', ondelete='CASCADE'), nullable=False, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    total_working_days = db.Column(db.Integer, default=22, nullable=False)
    days_present = db.Column(db.Integer, default=22, nullable=False)
    days_absent = db.Column(db.Integer, default=0, nullable=False)
    lop_days = db.Column(db.Integer, default=0, nullable=False)
    overtime_hours = db.Column(db.Float, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'emp_id', 'month', 'year', name='uq_attendance_org_emp_period'),
        CheckConstraint('month >= 1 AND month <= 12', name='ck_att_month_range'),
        CheckConstraint('year >= 2000 AND year <= 2100', name='ck_att_year_range'),
        CheckConstraint('total_working_days >= 0', name='ck_att_working_days_nonneg'),
        CheckConstraint('days_present >= 0', name='ck_att_days_present_nonneg'),
        CheckConstraint('days_absent >= 0', name='ck_att_days_absent_nonneg'),
        CheckConstraint('lop_days >= 0', name='ck_att_lop_days_nonneg'),
        CheckConstraint('overtime_hours >= 0', name='ck_att_ot_hours_nonneg'),
    )


class StatutoryRate(db.Model):
    """Configurable table for EPF (12%), ESI, Tax slabs"""
    __tablename__ = 'statutory_rates'
    rate_id = db.Column(db.Integer, primary_key=True)
    rate_type = db.Column(db.String(50), nullable=False, index=True)  # EPF, ESI, PT, TDS
    description = db.Column(db.String(200))
    percentage = db.Column(db.Float, nullable=False)
    max_limit = db.Column(db.Float, nullable=True)
    effective_from = db.Column(db.Date, default=date.today, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint('percentage >= 0', name='ck_statutory_pct_nonneg'),
    )


class PayrollRun(db.Model):
    """Tracks lifecycle: Draft → Pending → Finalized with Tenant Isolation"""
    __tablename__ = 'payroll_runs'
    run_id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default='draft', nullable=False)  # draft, pending, approved, finalized
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    approver_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    total_gross = db.Column(db.Float, default=0, nullable=False)
    total_deductions = db.Column(db.Float, default=0, nullable=False)
    total_net = db.Column(db.Float, default=0, nullable=False)
    employee_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finalized_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('org_id', 'month', 'year', name='uq_payroll_run_org_period'),
        CheckConstraint('month >= 1 AND month <= 12', name='ck_payroll_month_range'),
        CheckConstraint('year >= 2000 AND year <= 2100', name='ck_payroll_year_range'),
        CheckConstraint('total_gross >= 0', name='ck_payroll_gross_nonneg'),
        CheckConstraint('total_deductions >= 0', name='ck_payroll_deductions_nonneg'),
        CheckConstraint('total_net >= 0', name='ck_payroll_net_nonneg'),
        CheckConstraint('employee_count >= 0', name='ck_payroll_emp_count_nonneg'),
        CheckConstraint("status IN ('draft', 'pending', 'approved', 'finalized')", name='ck_payroll_status'),
    )

    payslips = db.relationship('Payslip', backref='payroll_run', lazy=True, cascade='all, delete-orphan')


class Payslip(db.Model):
    """Per-employee payslip with gross, deductions, net pay"""
    __tablename__ = 'payslips'
    slip_id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('payroll_runs.run_id', ondelete='CASCADE'), nullable=False, index=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id', ondelete='CASCADE'), nullable=False, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    basic_pay = db.Column(db.Float, default=0, nullable=False)
    hra = db.Column(db.Float, default=0, nullable=False)
    da = db.Column(db.Float, default=0, nullable=False)
    special_allowance = db.Column(db.Float, default=0, nullable=False)
    conveyance = db.Column(db.Float, default=0, nullable=False)
    medical_allowance = db.Column(db.Float, default=0, nullable=False)
    gross_pay = db.Column(db.Float, default=0, nullable=False)
    epf_deduction = db.Column(db.Float, default=0, nullable=False)
    esi_deduction = db.Column(db.Float, default=0, nullable=False)
    professional_tax = db.Column(db.Float, default=0, nullable=False)
    tds = db.Column(db.Float, default=0, nullable=False)
    lop_deduction = db.Column(db.Float, default=0, nullable=False)
    total_deductions = db.Column(db.Float, default=0, nullable=False)
    net_pay = db.Column(db.Float, default=0, nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'emp_id', 'month', 'year', name='uq_payslip_org_emp_period'),
        CheckConstraint('month >= 1 AND month <= 12', name='ck_payslip_month_range'),
        CheckConstraint('year >= 2000 AND year <= 2100', name='ck_payslip_year_range'),
        CheckConstraint('gross_pay >= 0', name='ck_payslip_gross_nonneg'),
        CheckConstraint('total_deductions >= 0', name='ck_payslip_deductions_nonneg'),
        CheckConstraint('net_pay >= 0', name='ck_payslip_net_nonneg'),
    )


class TaxDeclaration(db.Model):
    """Investment declarations under 80C, 80D for TDS computation"""
    __tablename__ = 'tax_declarations'
    declaration_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id', ondelete='CASCADE'), nullable=False, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    financial_year = db.Column(db.String(9), nullable=False)  # e.g. 2025-2026
    regime = db.Column(db.String(10), default='new', nullable=False)  # old, new
    section_80c = db.Column(db.Float, default=0, nullable=False)  # PPF, ELSS, LIC etc.
    section_80d = db.Column(db.Float, default=0, nullable=False)  # Health Insurance
    section_80e = db.Column(db.Float, default=0, nullable=False)  # Education Loan
    section_80g = db.Column(db.Float, default=0, nullable=False)  # Donations
    hra_exemption = db.Column(db.Float, default=0, nullable=False)
    lta_claimed = db.Column(db.Float, default=0, nullable=False)
    other_income = db.Column(db.Float, default=0, nullable=False)
    status = db.Column(db.String(20), default='draft', nullable=False)  # draft, submitted, verified
    submitted_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('org_id', 'emp_id', 'financial_year', name='uq_tax_decl_org_emp_fy'),
        CheckConstraint('section_80c >= 0', name='ck_tax_80c_nonneg'),
        CheckConstraint('section_80d >= 0', name='ck_tax_80d_nonneg'),
    )


class Reimbursement(db.Model):
    """Employee reimbursement claims"""
    __tablename__ = 'reimbursements'
    claim_id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.Integer, db.ForeignKey('employees.emp_id', ondelete='CASCADE'), nullable=False, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False, index=True)
    category = db.Column(db.String(50), nullable=False)  # travel, medical, food, other
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(300))
    receipt_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False)  # pending, approved, rejected, paid
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint('amount >= 0', name='ck_reimbursement_amt_nonneg'),
        CheckConstraint("status IN ('pending', 'approved', 'rejected', 'paid')", name='ck_reimbursement_status'),
    )


class AuditLog(db.Model):
    """Tamper-proof record of all actions with timestamps"""
    __tablename__ = 'audit_logs'
    audit_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    org_id = db.Column(db.Integer, db.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=True, index=True)
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class IdempotencyRecord(db.Model):
    """Atomic idempotency key registry for duplicate request prevention"""
    __tablename__ = 'idempotency_records'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(128), nullable=False, index=True)
    org_id = db.Column(db.Integer, nullable=False, index=True)
    endpoint = db.Column(db.String(128), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    response_code = db.Column(db.Integer, nullable=False)
    response_body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'key', name='uq_idempotency_org_key'),
    )


class RevokedToken(db.Model):
    """Registry of revoked JWT tokens (by JTI) for immediate server-side revocation"""
    __tablename__ = 'revoked_tokens'
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(64), unique=True, index=True, nullable=False)
    revoked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
