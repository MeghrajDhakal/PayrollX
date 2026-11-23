"""
PayrollX — Multi-Tenant Demo Data Seeder (PostgreSQL / SQLite)
Populates the database with realistic fake data for at least 2 distinct tenants.
Idempotent: Safe to re-run multiple times without creating duplicates.

Usage:
    python3 scripts/seed_demo.py
"""
import os
import sys
from datetime import datetime, date

# Calculate directory paths
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, '..'))
PROJECT_DIR = os.path.join(ROOT_DIR, 'My_Project')
SRC_DIR = os.path.join(PROJECT_DIR, 'src')

for p in [SRC_DIR, PROJECT_DIR, ROOT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from app import app, db
from models import (
    Organization, User, Employee, SalaryStructure,
    AttendanceLog, StatutoryRate, PayrollRun, Payslip, TaxDeclaration
)


def seed_tenant_payroll_run(org_id, admin_user_id, month=2, year=2026):
    """Generate or retrieve a finalized demo payroll run for the tenant."""
    run = PayrollRun.query.filter_by(org_id=org_id, month=month, year=year).first()
    if not run:
        run = PayrollRun(
            month=month,
            year=year,
            status='finalized',
            created_by=admin_user_id,
            org_id=org_id,
            finalized_at=datetime.utcnow()
        )
        db.session.add(run)
        db.session.flush()

    # Generate payslips for each employee in this org
    employees = Employee.query.filter_by(org_id=org_id).all()
    t_gross = 0.0
    t_ded = 0.0
    t_net = 0.0

    for emp in employees:
        existing_slip = Payslip.query.filter_by(
            org_id=org_id, emp_id=emp.emp_id, month=month, year=year
        ).first()

        salary = SalaryStructure.query.filter_by(
            emp_id=emp.emp_id, org_id=org_id, is_current=True
        ).first()

        if not salary:
            continue

        basic = round(salary.basic_pay / 12, 2)
        hra = round(salary.hra / 12, 2)
        da = round(salary.da / 12, 2)
        special = round(salary.special_allowance / 12, 2)
        conveyance = round(salary.conveyance / 12, 2)
        medical = round(salary.medical_allowance / 12, 2)
        gross = basic + hra + da + special + conveyance + medical

        # Standard statutory deductions (EPF 12%, ESI 0.75%, PT ₹200)
        epf = round(min(basic, 15000) * 0.12, 2)
        esi = round(gross * 0.0075, 2) if gross <= 21000 else 0.0
        pt = 200.0 if gross > 15000 else 0.0
        tds = round(gross * 0.05, 2) if (salary.ctc_annual or 0) > 700000 else 0.0
        total_ded = epf + esi + pt + tds
        net = round(gross - total_ded, 2)

        if not existing_slip:
            slip = Payslip(
                run_id=run.run_id,
                emp_id=emp.emp_id,
                org_id=org_id,
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
                lop_deduction=0.0,
                total_deductions=total_ded,
                net_pay=net,
                generated_at=datetime.utcnow()
            )
            db.session.add(slip)
        else:
            gross = existing_slip.gross_pay
            total_ded = existing_slip.total_deductions
            net = existing_slip.net_pay

        t_gross += gross
        t_ded += total_ded
        t_net += net

    run.total_gross = round(t_gross, 2)
    run.total_deductions = round(t_ded, 2)
    run.total_net = round(t_net, 2)
    run.employee_count = len(employees)
    db.session.commit()
    return run


def seed_demo_data():
    """Populate database with realistic multi-tenant demo data."""
    print("=" * 60)
    print("PayrollX — Seeding Multi-Tenant Demo Data")
    print("=" * 60)

    # ────────────────────────────────────────────────────────────
    # 1. Statutory Compliance Rates (Global Reference Data)
    # ────────────────────────────────────────────────────────────
    rates = [
        ('EPF', 'Employee Provident Fund - Employee Share', 12.0, 15000),
        ('EPF_EMPLOYER', 'Employer PF Contribution', 12.0, 15000),
        ('ESI_EMPLOYEE', 'ESI Employee Share', 0.75, 21000),
        ('ESI_EMPLOYER', 'ESI Employer Share', 3.25, 21000),
        ('PT', 'Professional Tax', 200, None),
    ]
    for rt, desc, pct, limit in rates:
        existing_rate = StatutoryRate.query.filter_by(rate_type=rt).first()
        if not existing_rate:
            db.session.add(StatutoryRate(
                rate_type=rt, description=desc, percentage=pct, max_limit=limit, is_active=True
            ))
    db.session.commit()
    print("[+] Statutory compliance rates verified.")

    # ────────────────────────────────────────────────────────────
    # 2. TENANT 1: LEARNTODAY DIGITAL PRIVATE LIMITED
    # ────────────────────────────────────────────────────────────
    org1_name = 'LEARNTODAY DIGITAL PRIVATE LIMITED'
    org1 = Organization.query.filter_by(name=org1_name).first()
    if not org1:
        org1 = Organization(name=org1_name)
        db.session.add(org1)
        db.session.flush()
    print(f"[+] Tenant 1 verified: {org1.name} (Org ID: {org1.org_id})")

    # Tenant 1 Admin
    admin1 = User.query.filter_by(username='admin').first()
    if not admin1:
        admin1 = User(
            username='admin',
            email='admin@learntoday.fake',
            role='hr_admin',
            org_id=org1.org_id
        )
        admin1.set_password('admin123')
        db.session.add(admin1)
        db.session.flush()

    # Tenant 1 Finance
    finance1 = User.query.filter_by(username='finance').first()
    if not finance1:
        finance1 = User(
            username='finance',
            email='finance@learntoday.fake',
            role='finance',
            org_id=org1.org_id
        )
        finance1.set_password('finance123')
        db.session.add(finance1)
        db.session.flush()

    # Tenant 1 Employees
    emp1_records = [
        ('Rahul', 'Sharma', 'rahul@learntoday.fake', 'Engineering', 'Senior Developer', 1200000, 'ABCDE1234F', '123456789012', '123456789012', 'HDFC0001234'),
        ('Priya', 'Patel', 'priya@learntoday.fake', 'Engineering', 'Full Stack Developer', 900000, 'BCDEF2345G', '234567890123', '234567890123', 'HDFC0001234'),
        ('Amit', 'Kumar', 'amit@learntoday.fake', 'Design', 'UI/UX Designer', 750000, 'CDEFG3456H', '345678901234', '345678901234', 'ICIC0002345'),
        ('Sneha', 'Gupta', 'sneha@learntoday.fake', 'Marketing', 'Marketing Manager', 850000, 'DEFGH4567I', '456789012345', '456789012345', 'SBIN0003456'),
        ('Vikram', 'Singh', 'vikram@learntoday.fake', 'HR', 'HR Executive', 600000, 'EFGHI5678J', '567890123456', '567890123456', 'HDFC0001234'),
        ('Neha', 'Reddy', 'neha@learntoday.fake', 'Finance', 'Accountant', 700000, 'FGHIJ6789K', '678901234567', '678901234567', 'ICIC0002345'),
        ('Arjun', 'Nair', 'arjun@learntoday.fake', 'Engineering', 'Backend Developer', 1000000, 'GHIJK7890L', '789012345678', '789012345678', 'SBIN0003456'),
        ('Kavita', 'Joshi', 'kavita@learntoday.fake', 'Operations', 'Operations Lead', 800000, 'HIJKL8901M', '890123456789', '890123456789', 'HDFC0001234'),
    ]

    for idx, (fn, ln, email, dept, desig, ctc, pan, aadhaar, acc, ifsc) in enumerate(emp1_records, start=1):
        emp = Employee.query.filter_by(org_id=org1.org_id, email=email).first()
        if not emp:
            emp = Employee(
                first_name=fn, last_name=ln, email=email,
                phone='+919876543210', pan_number=pan,
                aadhaar_number=aadhaar, bank_account=acc, bank_ifsc=ifsc,
                bank_name='HDFC Bank', department=dept, designation=desig,
                date_of_joining=date(2024, 1, 15), org_id=org1.org_id
            )
            db.session.add(emp)
            db.session.flush()

        username = f"EMP{emp.emp_id}"
        emp_user = User.query.filter_by(username=username).first()
        if not emp_user:
            emp_user = User(
                username=username, email=email, role='employee', org_id=org1.org_id
            )
            emp_user.set_password(f"{fn.lower()}123")
            db.session.add(emp_user)
            db.session.flush()
        emp.user_id = emp_user.id

        # Salary Structure
        salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, org_id=org1.org_id).first()
        if not salary:
            basic = round(ctc * 0.40, 2)
            hra = round(basic * 0.50, 2)
            da = round(ctc * 0.05, 2)
            conveyance = round(ctc * 0.05, 2)
            medical = round(ctc * 0.03, 2)
            special = round(max(0, ctc - basic - hra - da - conveyance - medical), 2)
            salary = SalaryStructure(
                emp_id=emp.emp_id, org_id=org1.org_id, ctc_annual=ctc,
                basic_pay=basic, hra=hra, da=da, special_allowance=special,
                conveyance=conveyance, medical_allowance=medical, is_current=True
            )
            db.session.add(salary)

        # Attendance Logs
        for m in (1, 2, 3):
            att = AttendanceLog.query.filter_by(
                emp_id=emp.emp_id, org_id=org1.org_id, month=m, year=2026
            ).first()
            if not att:
                db.session.add(AttendanceLog(
                    emp_id=emp.emp_id, org_id=org1.org_id, month=m, year=2026,
                    total_working_days=22, days_present=21 if m == 2 else 22,
                    days_absent=1 if m == 2 else 0, lop_days=0
                ))

        # Tax Declaration
        tax = TaxDeclaration.query.filter_by(
            emp_id=emp.emp_id, org_id=org1.org_id, financial_year='2025-2026'
        ).first()
        if not tax:
            db.session.add(TaxDeclaration(
                emp_id=emp.emp_id, org_id=org1.org_id, financial_year='2025-2026',
                regime='new', section_80c=150000, section_80d=25000
            ))

    db.session.commit()
    seed_tenant_payroll_run(org1.org_id, admin1.id, month=2, year=2026)
    print(f"[+] Tenant 1 setup complete with {len(emp1_records)} employees and finalized payroll.")

    # ────────────────────────────────────────────────────────────
    # 3. TENANT 2: TECHCORP SOLUTIONS PRIVATE LIMITED
    # ────────────────────────────────────────────────────────────
    org2_name = 'TECHCORP SOLUTIONS PRIVATE LIMITED'
    org2 = Organization.query.filter_by(name=org2_name).first()
    if not org2:
        org2 = Organization(name=org2_name)
        db.session.add(org2)
        db.session.flush()
    print(f"[+] Tenant 2 verified: {org2.name} (Org ID: {org2.org_id})")

    # Tenant 2 Admin
    admin2 = User.query.filter_by(username='techcorp_admin').first()
    if not admin2:
        admin2 = User(
            username='techcorp_admin',
            email='admin@techcorp.fake',
            role='hr_admin',
            org_id=org2.org_id
        )
        admin2.set_password('Admin@techcorp123')
        db.session.add(admin2)
        db.session.flush()

    # Tenant 2 Finance
    finance2 = User.query.filter_by(username='techcorp_finance').first()
    if not finance2:
        finance2 = User(
            username='techcorp_finance',
            email='finance@techcorp.fake',
            role='finance',
            org_id=org2.org_id
        )
        finance2.set_password('Finance@techcorp123')
        db.session.add(finance2)
        db.session.flush()

    # Tenant 2 Employees
    emp2_records = [
        ('Vikas', 'Mehta', 'vikas@techcorp.fake', 'Product', 'Principal Engineer', 2400000, 'JKLMN5678O', '456789012345', '456789012345', 'HDFC0001234'),
        ('Ananya', 'Iyer', 'ananya@techcorp.fake', 'Engineering', 'Frontend Lead', 1800000, 'KLMNO6789P', '567890123456', '567890123456', 'ICIC0002345'),
        ('Kunal', 'Roy', 'kunal@techcorp.fake', 'Quality', 'QA Automation Lead', 1400000, 'LMNOP7890Q', '678901234567', '678901234567', 'SBIN0003456'),
        ('Divya', 'Rao', 'divya@techcorp.fake', 'Design', 'Senior Product Designer', 1500000, 'MNOPQ8901R', '789012345678', '789012345678', 'HDFC0001234'),
    ]

    for fn, ln, email, dept, desig, ctc, pan, aadhaar, acc, ifsc in emp2_records:
        emp = Employee.query.filter_by(org_id=org2.org_id, email=email).first()
        if not emp:
            emp = Employee(
                first_name=fn, last_name=ln, email=email,
                phone='+919876543299', pan_number=pan,
                aadhaar_number=aadhaar, bank_account=acc, bank_ifsc=ifsc,
                bank_name='ICICI Bank', department=dept, designation=desig,
                date_of_joining=date(2024, 3, 1), org_id=org2.org_id
            )
            db.session.add(emp)
            db.session.flush()

        username = f"TC_EMP{emp.emp_id}"
        emp_user = User.query.filter_by(username=username).first()
        if not emp_user:
            emp_user = User(
                username=username, email=email, role='employee', org_id=org2.org_id
            )
            emp_user.set_password(f"{fn.lower()}123")
            db.session.add(emp_user)
            db.session.flush()
        emp.user_id = emp_user.id

        # Salary Structure
        salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, org_id=org2.org_id).first()
        if not salary:
            basic = round(ctc * 0.40, 2)
            hra = round(basic * 0.50, 2)
            da = round(ctc * 0.05, 2)
            conveyance = round(ctc * 0.05, 2)
            medical = round(ctc * 0.03, 2)
            special = round(max(0, ctc - basic - hra - da - conveyance - medical), 2)
            salary = SalaryStructure(
                emp_id=emp.emp_id, org_id=org2.org_id, ctc_annual=ctc,
                basic_pay=basic, hra=hra, da=da, special_allowance=special,
                conveyance=conveyance, medical_allowance=medical, is_current=True
            )
            db.session.add(salary)

        # Attendance Logs
        for m in (1, 2, 3):
            att = AttendanceLog.query.filter_by(
                emp_id=emp.emp_id, org_id=org2.org_id, month=m, year=2026
            ).first()
            if not att:
                db.session.add(AttendanceLog(
                    emp_id=emp.emp_id, org_id=org2.org_id, month=m, year=2026,
                    total_working_days=22, days_present=22, days_absent=0, lop_days=0
                ))

        # Tax Declaration
        tax = TaxDeclaration.query.filter_by(
            emp_id=emp.emp_id, org_id=org2.org_id, financial_year='2025-2026'
        ).first()
        if not tax:
            db.session.add(TaxDeclaration(
                emp_id=emp.emp_id, org_id=org2.org_id, financial_year='2025-2026',
                regime='new', section_80c=150000, section_80d=25000
            ))

    db.session.commit()
    seed_tenant_payroll_run(org2.org_id, admin2.id, month=2, year=2026)
    print(f"[+] Tenant 2 setup complete with {len(emp2_records)} employees and finalized payroll.")

    print("=" * 60)
    print("[+] All tenants and demo credentials seeded successfully!")
    print("=" * 60)


if __name__ == '__main__':
    with app.app_context():
        seed_demo_data()
