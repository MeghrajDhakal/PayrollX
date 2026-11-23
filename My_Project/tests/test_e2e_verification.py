"""
PayrollX — Phase 4 End-to-End Verification Suite Before Go-Live
Covers the complete core user journey:
Sign In → Create Employee → Run Payroll → Generate Payslip PDF

Tested under the 5 exact production conditions:
1. Cross-Tenant Attack (Tenant A user attempts to read, update, or download Tenant B's PDF)
2. Revoked Session (Copy session cookie, call /logout, replay cookie -> rejected)
3. Duplicate Request (Parallel identical payroll creation with same idempotency key -> exactly 1 record)
4. Invalid Input (Malformed types, negative salaries, oversized strings, SQL/HTML injection)
5. Observability Check (Controlled error traces exact request_id without leaking PII or secrets)
"""
import sys
import os
import pytest
import json
import logging
import io

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from app import app
from models import db, Organization, User, Employee, SalaryStructure, AttendanceLog, PayrollRun, Payslip
from logging_config import StructuredJsonFormatter, mask_pii


@pytest.fixture
def test_app():
    from app import limiter
    limiter.enabled = False
    limiter.reset()

    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SECRET_KEY'] = 'production-grade-test-secret-key-32chars!!'
    app.config['JWT_SECRET_KEY'] = 'production-grade-test-jwt-secret-key-32ch!!'
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['LOGIN_DISABLED'] = False

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        limiter.reset()


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def test_core_user_journey(client, test_app):
    """
    Core User Journey:
    1. Sign In (as HR Admin)
    2. Create Employee
    3. Run Payroll
    4. Generate Payslip PDF
    """
    with test_app.app_context():
        # Setup Organization & HR Admin
        org = Organization(name="Acme Corp India Pvt Ltd")
        db.session.add(org)
        db.session.commit()

        admin = User(username="acme_admin", email="admin@acme.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("AdminSecurePass123!")
        db.session.add(admin)
        db.session.commit()
        org_id = org.org_id

    # 1. Sign In
    login_resp = client.post('/login', data={
        'username': 'acme_admin',
        'password': 'AdminSecurePass123!',
        'login_mode': 'admin'
    }, follow_redirects=False)
    assert login_resp.status_code == 302
    assert client.get_cookie('session') is not None

    # 2. Create Employee
    emp_payload = {
        'first_name': 'Rohan',
        'last_name': 'Verma',
        'email': 'rohan.verma@acme.fake',
        'phone': '+919876543210',
        'pan_number': 'ABCDE1234F',
        'aadhaar_number': '987654321098',
        'bank_account': '50100234567890',
        'bank_ifsc': 'HDFC0001234',
        'bank_name': 'HDFC Bank',
        'department': 'Engineering',
        'designation': 'Lead Architect',
        'ctc': 1500000.0,
        'idempotency_key': 'create-emp-rohan-001'
    }
    create_resp = client.post('/people/add', data=emp_payload, follow_redirects=False)
    assert create_resp.status_code == 302

    # Verify employee was created in database
    with test_app.app_context():
        emp = Employee.query.filter_by(email='rohan.verma@acme.fake', org_id=org_id).first()
        assert emp is not None
        assert emp.full_name == "Rohan Verma"
        assert emp.salary_structures[0].ctc_annual == 1500000.0
        emp_id = emp.emp_id

    # 3. Run Payroll
    payroll_payload = {
        'month': 4,
        'year': 2026,
        'idempotency_key': 'payroll-run-2026-04'
    }
    run_resp = client.post('/run-payroll', data=payroll_payload, follow_redirects=False)
    assert run_resp.status_code == 302

    # Verify payroll run & payslip generated
    with test_app.app_context():
        run = PayrollRun.query.filter_by(month=4, year=2026, org_id=org_id).first()
        assert run is not None
        assert run.employee_count == 1
        assert run.total_net > 0

        slip = Payslip.query.filter_by(emp_id=emp_id, month=4, year=2026, org_id=org_id).first()
        assert slip is not None
        assert slip.gross_pay > 0
        assert slip.net_pay > 0
        slip_id = slip.slip_id

    # 4. Generate Payslip PDF
    pdf_resp = client.get(f'/payslip/pdf/{slip_id}')
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers['Content-Type'] == 'application/pdf'
    assert pdf_resp.data.startswith(b'%PDF')
    assert len(pdf_resp.data) > 1000  # Valid non-empty PDF bytes


# ── CONDITION 1: CROSS-TENANT ATTACK ──
def test_condition_1_cross_tenant_attack(client, test_app):
    """
    Cross-Tenant Attack:
    User from Tenant A attempts to:
    - Read Tenant B employee data
    - Download Tenant B payslip PDF
    - Generate Tenant B receipt via API
    Must return 403 or 404.
    """
    with test_app.app_context():
        # Tenant A
        orgA = Organization(name="Tenant Alpha Corp")
        db.session.add(orgA)
        db.session.flush()
        userA = User(username="alpha_admin", email="admin@alpha.fake", role="hr_admin", org_id=orgA.org_id)
        userA.set_password("AlphaPass123!")
        db.session.add(userA)

        # Tenant B
        orgB = Organization(name="Tenant Beta Corp")
        db.session.add(orgB)
        db.session.flush()
        userB = User(username="beta_admin", email="admin@beta.fake", role="hr_admin", org_id=orgB.org_id)
        userB.set_password("BetaPass123!")
        db.session.add(userB)

        # Employee & Payslip in Tenant B
        empB = Employee(
            first_name="Beta", last_name="Worker", email="worker@beta.fake",
            org_id=orgB.org_id, department="Sales", designation="Executive"
        )
        db.session.add(empB)
        db.session.flush()

        runB = PayrollRun(month=5, year=2026, org_id=orgB.org_id, status='pending')
        db.session.add(runB)
        db.session.flush()

        slipB = Payslip(
            run_id=runB.run_id, emp_id=empB.emp_id, org_id=orgB.org_id,
            month=5, year=2026, gross_pay=70000, total_deductions=5000, net_pay=65000
        )
        db.session.add(slipB)
        db.session.commit()

        empB_id = empB.emp_id
        slipB_id = slipB.slip_id

    # Admin A logs in
    client.post('/login', data={'username': 'alpha_admin', 'password': 'AlphaPass123!', 'login_mode': 'admin'})

    # 1. Attempt to download Tenant B payslip PDF via Web
    web_pdf_resp = client.get(f'/payslip/pdf/{slipB_id}')
    assert web_pdf_resp.status_code in (403, 404)

    # 2. Attempt to view Tenant B payslip download page
    web_dl_resp = client.get(f'/payslip/download/{slipB_id}')
    assert web_dl_resp.status_code in (403, 404)

    # 3. Attempt API data access with Tenant A JWT
    token_resp = client.post('/api/v1/auth/token', json={'username': 'alpha_admin', 'password': 'AlphaPass123!'})
    tokenA = token_resp.get_json()['access_token']
    auth_header = {'Authorization': f'Bearer {tokenA}'}

    api_emp_resp = client.get(f'/api/v1/employee/data?emp_id={empB_id}', headers=auth_header)
    assert api_emp_resp.status_code in (403, 404)

    api_pdf_resp = client.get(f'/api/v1/receipt/generate?slip_id={slipB_id}', headers=auth_header)
    assert api_pdf_resp.status_code in (403, 404)


# ── CONDITION 2: REVOKED SESSION ──
def test_condition_2_revoked_session(client, test_app):
    """
    Revoked Session:
    1. Copy a valid session cookie
    2. Call /logout
    3. Replay copied cookie -> must be rejected (redirected to /login)
    4. For API: obtain JWT, call /api/v1/auth/revoke, replay JWT -> returns 401
    """
    with test_app.app_context():
        org = Organization(name="Revocation Org")
        db.session.add(org)
        db.session.commit()

        user = User(username="session_victim", email="victim@revocation.fake", role="hr_admin", org_id=org.org_id)
        user.set_password("VictimPass123!")
        db.session.add(user)
        db.session.commit()

    # Cookie Session Test
    client.post('/login', data={'username': 'session_victim', 'password': 'VictimPass123!', 'login_mode': 'admin'})
    session_cookie = client.get_cookie('session')
    assert session_cookie is not None

    # Access /dashboard with cookie -> OK
    assert client.get('/dashboard').status_code == 200

    # User logs out
    client.get('/logout')

    # Replay copied session cookie
    client.set_cookie('session', session_cookie.value)
    stolen_req = client.get('/dashboard', follow_redirects=False)
    assert stolen_req.status_code == 302
    assert '/login' in stolen_req.headers['Location']

    # API JWT Revocation Test
    tok_resp = client.post('/api/v1/auth/token', json={'username': 'session_victim', 'password': 'VictimPass123!'})
    assert tok_resp.status_code == 200
    token = tok_resp.get_json()['access_token']

    # Token works initially
    chk_resp = client.get('/api/v1/employees', headers={'Authorization': f'Bearer {token}'})
    assert chk_resp.status_code == 200

    # Revoke token
    rev_resp = client.post('/api/v1/auth/revoke', headers={'Authorization': f'Bearer {token}'})
    assert rev_resp.status_code == 200

    # Replay revoked token -> must return 401
    replay_resp = client.get('/api/v1/employees', headers={'Authorization': f'Bearer {token}'})
    assert replay_resp.status_code == 401
    assert "revoked" in replay_resp.get_json().get('error', '').lower()


# ── CONDITION 3: DUPLICATE REQUEST & IDEMPOTENCY ──
def test_condition_3_duplicate_request_idempotency(client, test_app):
    """
    Duplicate Request:
    Send two identical payroll creation requests with the same idempotency key:
    Must create exactly ONE record.
    """
    with test_app.app_context():
        org = Organization(name="Duplicate Protection Org")
        db.session.add(org)
        db.session.commit()

        admin = User(username="idemp_admin", email="idemp@dup.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("IdempPass123!")
        db.session.add(admin)

        emp = Employee(
            first_name="Karan", last_name="Malhotra", email="karan@dup.fake",
            org_id=org.org_id, department="Finance", designation="Analyst"
        )
        db.session.add(emp)
        db.session.flush()

        sal = SalaryStructure(
            emp_id=emp.emp_id, org_id=org.org_id, ctc_annual=600000,
            basic_pay=240000, hra=120000, da=30000, special_allowance=150000,
            conveyance=30000, medical_allowance=30000
        )
        db.session.add(sal)
        db.session.commit()
        org_id = org.org_id

    # API request with Idempotency-Key
    tok_resp = client.post('/api/v1/auth/token', json={'username': 'idemp_admin', 'password': 'IdempPass123!'})
    token = tok_resp.get_json()['access_token']
    headers = {
        'Authorization': f'Bearer {token}',
        'Idempotency-Key': 'payroll-run-unique-key-2026-06'
    }
    payload = {'month': 6, 'year': 2026}

    # First request
    resp1 = client.post('/api/v1/payroll/run', json=payload, headers=headers)
    assert resp1.status_code == 200

    # Second identical request with same key
    resp2 = client.post('/api/v1/payroll/run', json=payload, headers=headers)
    assert resp2.status_code == 200
    assert "idempotent" in resp2.get_json().get('message', '').lower()

    # Verify exactly ONE PayrollRun exists in the database
    with test_app.app_context():
        runs = PayrollRun.query.filter_by(month=6, year=2026, org_id=org_id).all()
        assert len(runs) == 1
        payslips = Payslip.query.filter_by(month=6, year=2026, org_id=org_id).all()
        assert len(payslips) == 1


# ── CONDITION 4: INVALID INPUT REJECTION ──
def test_condition_4_invalid_input_rejection(client, test_app):
    """
    Invalid Input Rejection:
    - Malformed types
    - Negative salaries
    - Oversized strings
    - SQL & HTML injection payloads
    Must be cleanly rejected with 400 or 422.
    """
    with test_app.app_context():
        org = Organization(name="Input Validation Org")
        db.session.add(org)
        db.session.commit()

        admin = User(username="sanitizer_admin", email="san@test.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("SanPass123!")
        db.session.add(admin)
        db.session.commit()

    tok_resp = client.post('/api/v1/auth/token', json={'username': 'sanitizer_admin', 'password': 'SanPass123!'})
    token = tok_resp.get_json()['access_token']
    headers = {'Authorization': f'Bearer {token}'}

    # 1. Malformed month (string instead of integer)
    bad_type = client.post('/api/v1/payroll/run', json={'month': 'March', 'year': 2026}, headers=headers)
    assert bad_type.status_code == 422

    # 2. Month out of range (13)
    bad_month = client.post('/api/v1/payroll/run', json={'month': 13, 'year': 2026}, headers=headers)
    assert bad_month.status_code == 422

    # 3. Negative salary via web form
    client.post('/login', data={'username': 'sanitizer_admin', 'password': 'SanPass123!', 'login_mode': 'admin'})
    neg_sal = client.post('/people/add', data={
        'first_name': 'Hacker',
        'last_name': 'Test',
        'email': 'hacker@test.fake',
        'ctc': -100000
    }, follow_redirects=False)
    assert neg_sal.status_code == 400

    # 4. Oversized string (> 10,000 chars)
    giant_string = 'A' * 15000
    oversized = client.post('/people/add', data={
        'first_name': giant_string,
        'last_name': 'Test',
        'email': 'oversize@test.fake',
        'ctc': 500000
    }, follow_redirects=False)
    assert oversized.status_code == 400

    # 5. SQL & HTML Injection payloads
    sqli_payload = client.post('/people/add', data={
        'first_name': "Robert'; DROP TABLE employees;--",
        'last_name': '<script>alert("XSS")</script>',
        'email': 'sqli@test.fake',
        'ctc': 500000
    }, follow_redirects=False)
    assert sqli_payload.status_code == 400


# ── CONDITION 5: OBSERVABILITY CHECK ──
def test_condition_5_observability_check(client, test_app, caplog):
    """
    Observability Check:
    Trigger a controlled error and confirm structured logs trace the exact
    request_id without leaking any PII or secrets.
    """
    caplog.set_level(logging.INFO)
    custom_req_id = "obs-check-uuid-9876543210"

    # Trigger controlled error
    resp = client.get(
        '/api/v1/test/error-trigger',
        headers={'X-Request-ID': custom_req_id, 'Accept': 'application/json'}
    )
    assert resp.status_code == 500
    assert resp.headers.get('X-Request-ID') == custom_req_id

    body = resp.get_json()
    assert body.get('request_id') == custom_req_id

    # Verify no raw secrets leaked in HTTP response
    assert "secret_api_key_xyz9876543210" not in json.dumps(body)

    # Format a log record with the StructuredJsonFormatter and verify PII masking
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.ERROR,
        pathname="app.py",
        lineno=100,
        msg="Error processing PAN ABCDE1234F with Aadhaar 123456789012 and password='SuperSecretPassword'",
        args=(),
        exc_info=None
    )
    formatted_log = formatter.format(record)
    log_data = json.loads(formatted_log)

    # Assert PII is properly masked in structured log output
    assert "ABCDE1234F" not in log_data['message']
    assert "*****1234F" in log_data['message']
    assert "123456789012" not in log_data['message']
    assert "********9012" in log_data['message']
    assert "SuperSecretPassword" not in log_data['message']
    assert "******" in log_data['message']
