import sys
import os
import pytest
from datetime import date

# Set paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from app import app
from models import db, Organization, User, Employee, SalaryStructure, AttendanceLog, PayrollRun, Payslip, IdempotencyRecord
from idempotency import get_idempotency_key, save_idempotent_response, get_idempotent_response
from resilience import safe_external_call


@pytest.fixture
def test_app():
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SECRET_KEY'] = 'test-secret-key-12345678901234567890'
    app.config['JWT_SECRET_KEY'] = 'test-jwt-secret-key-12345678901234567890'
    app.config['WTF_CSRF_ENABLED'] = False

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def test_database_check_constraint_negative_salary(test_app):
    """Test CHECK constraint on negative salaries"""
    with test_app.app_context():
        org = Organization(name="Test Org")
        db.session.add(org)
        db.session.commit()

        emp = Employee(
            first_name="John", last_name="Doe", email="john@test.com",
            org_id=org.org_id, department="Engineering", designation="Engineer"
        )
        db.session.add(emp)
        db.session.commit()

        # Try inserting negative ctc_annual
        bad_salary = SalaryStructure(
            emp_id=emp.emp_id,
            org_id=org.org_id,
            ctc_annual=-50000,
            basic_pay=20000,
            hra=10000
        )
        db.session.add(bad_salary)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_database_check_constraint_invalid_month(test_app):
    """Test CHECK constraint on invalid month (e.g. month 13)"""
    with test_app.app_context():
        org = Organization(name="Month Test Org")
        db.session.add(org)
        db.session.commit()

        bad_run = PayrollRun(
            month=13, year=2026, org_id=org.org_id, status='draft'
        )
        db.session.add(bad_run)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_unique_constraint_payslip_duplicate(test_app):
    """Test UNIQUE constraint: one payslip per employee per pay period in an org"""
    with test_app.app_context():
        org = Organization(name="Unique Test Org")
        db.session.add(org)
        db.session.commit()

        emp = Employee(
            first_name="Alice", last_name="Smith", email="alice@test.com",
            org_id=org.org_id
        )
        db.session.add(emp)
        db.session.commit()

        run = PayrollRun(month=3, year=2026, org_id=org.org_id, status='pending')
        db.session.add(run)
        db.session.commit()

        slip1 = Payslip(
            run_id=run.run_id, emp_id=emp.emp_id, org_id=org.org_id,
            month=3, year=2026, basic_pay=50000, gross_pay=50000, net_pay=45000
        )
        db.session.add(slip1)
        db.session.commit()

        # Duplicate slip for same period & employee
        slip2 = Payslip(
            run_id=run.run_id, emp_id=emp.emp_id, org_id=org.org_id,
            month=3, year=2026, basic_pay=50000, gross_pay=50000, net_pay=45000
        )
        db.session.add(slip2)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_idempotency_key_storage_and_lookup(test_app):
    """Test atomic idempotency storage and lookup"""
    with test_app.app_context():
        org = Organization(name="Idempotency Org")
        db.session.add(org)
        db.session.commit()

        key = "idemp-test-uuid-12345"
        endpoint = "/run-payroll"
        save_idempotent_response(
            key=key, org_id=org.org_id, endpoint=endpoint,
            request_hash="dummyhash123", response_code=200, response_body='{"status": "ok"}'
        )

        res = get_idempotent_response(key, org.org_id)
        assert res is not None
        code, body = res
        assert code == 200
        assert "ok" in body

        # Duplicate key for same org raises Unique constraint error on direct insert
        dup = IdempotencyRecord(
            key=key, org_id=org.org_id, endpoint=endpoint,
            request_hash="hash2", response_code=200, response_body='{}'
        )
        db.session.add(dup)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_external_call_resilience():
    """Test external call utility fails gracefully and has timeouts configured"""
    # Unreachable or non-existent port should return gracefully without crashing
    result = safe_external_call("http://127.0.0.1:59999/dummy-webhook", timeout=(0.2, 0.2))
    assert result.success is False
    assert result.error is not None
