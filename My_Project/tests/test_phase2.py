import sys
import os
import pytest
import json
import time

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from app import app
from models import db, Organization, User, Employee, Payslip, PayrollRun, SalaryStructure
from logging_config import mask_pii
from config import validate_production_secrets


@pytest.fixture
def test_app():
    from app import limiter
    limiter.enabled = False
    limiter.reset()

    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SECRET_KEY'] = 'test-secret-key-12345678901234567890'
    app.config['JWT_SECRET_KEY'] = 'test-jwt-secret-key-12345678901234567890'
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


def test_password_hashing_bcrypt(test_app):
    """Verify passwords are saved with bcrypt format ($2b$) and verify correctly"""
    with test_app.app_context():
        user = User(username="bctest", email="bctest@test.fake", role="employee")
        user.set_password("MySuperSecret123")
        assert user.password_hash.startswith("$2b$")
        assert user.check_password("MySuperSecret123") is True
        assert user.check_password("WrongPassword") is False


def test_session_invalidation_on_logout(client, test_app):
    """Server-side session invalidation: copying session cookie, calling logout, and replaying old cookie fails"""
    with test_app.app_context():
        org = Organization(name="Logout Org")
        db.session.add(org)
        db.session.commit()

        user = User(username="logout_user", email="logout@test.fake", role="hr_admin", org_id=org.org_id)
        user.set_password("validpassword123")
        db.session.add(user)
        db.session.commit()

    # Log in
    resp = client.post('/login', data={'username': 'logout_user', 'password': 'validpassword123', 'login_mode': 'admin'}, follow_redirects=False)
    assert resp.status_code == 302
    cookie = client.get_cookie('session')
    assert cookie is not None

    # Access protected route with active session
    dash_resp = client.get('/dashboard')
    assert dash_resp.status_code == 200

    # Call logout
    logout_resp = client.get('/logout', follow_redirects=False)
    assert logout_resp.status_code == 302

    # Replay copied session cookie
    client.set_cookie('session', cookie.value)
    replayed_resp = client.get('/dashboard', follow_redirects=False)
    # Must redirect to login because session_token was invalidated on the server
    assert replayed_resp.status_code == 302
    assert '/login' in replayed_resp.headers['Location']


def test_cross_tenant_isolation(client, test_app):
    """Tenant A admin cannot access Tenant B employee data via API or web"""
    with test_app.app_context():
        orgA = Organization(name="Tenant A")
        orgB = Organization(name="Tenant B")
        db.session.add_all([orgA, orgB])
        db.session.commit()

        adminA = User(username="adminA", email="adminA@test.fake", role="hr_admin", org_id=orgA.org_id)
        adminA.set_password("passA123")

        empB = Employee(
            first_name="Bob", last_name="Builder", email="bob@tenantb.fake",
            org_id=orgB.org_id, department="Engineering", designation="Dev"
        )
        db.session.add_all([adminA, empB])
        db.session.commit()
        empB_id = empB.emp_id

    # Get JWT token for Admin A
    token_resp = client.post('/api/v1/auth/token', json={'username': 'adminA', 'password': 'passA123'})
    assert token_resp.status_code == 200
    tokenA = token_resp.get_json()['access_token']

    # Admin A attempts to fetch Employee B data
    hack_resp = client.get(
        f'/api/v1/employee/data?emp_id={empB_id}',
        headers={'Authorization': f'Bearer {tokenA}'}
    )
    # Must return 404 (scoped to org) or 403
    assert hack_resp.status_code in (403, 404)
    data = hack_resp.get_json()
    assert "not found" in data.get('error', '').lower() or "denied" in data.get('error', '').lower()


def test_input_validation_rejection(client, test_app):
    """Test schema validation rejects negative salary, invalid PAN, and unexpected fields"""
    with test_app.app_context():
        org = Organization(name="Input Validation Org")
        db.session.add(org)
        db.session.commit()

        admin = User(username="val_admin", email="val_admin@test.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("adminpass123")
        db.session.add(admin)
        db.session.commit()

    # Log in
    client.post('/login', data={'username': 'val_admin', 'password': 'adminpass123', 'login_mode': 'admin'})

    # Negative CTC
    bad_payload = {
        'first_name': 'Test',
        'last_name': 'User',
        'email': 'badctc@test.fake',
        'ctc': -500000,
        'department': 'Engineering',
        'designation': 'QA'
    }
    resp = client.post('/people/add', data=bad_payload, follow_redirects=False)
    assert resp.status_code == 400

    # Invalid PAN format
    bad_pan = {
        'first_name': 'Test',
        'last_name': 'User',
        'email': 'badpan@test.fake',
        'pan_number': '12345ABCDE',  # Invalid format
        'ctc': 600000,
        'department': 'Engineering',
        'designation': 'QA'
    }
    resp2 = client.post('/people/add', data=bad_pan, follow_redirects=False)
    assert resp2.status_code == 400


def test_pii_masking():
    """Verify log PII masking filters PAN, Aadhaar, Bank account, passwords, and tokens"""
    raw_log = (
        "User EMP1 with PAN ABCDE1234F and Aadhaar 123456789012 and "
        "bank account 987654321012 logged in with password='secretPassword123' "
        "and email test.user@example.com using Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz"
    )
    masked = mask_pii(raw_log)

    assert "ABCDE1234F" not in masked
    assert "*****1234F" in masked
    assert "123456789012" not in masked
    assert "********9012" in masked
    assert "secretPassword123" not in masked
    assert "******" in masked
    assert "t***@example.com" in masked
    assert "[REDACTED_TOKEN]" in masked


def test_secrets_management_fail_fast():
    """Verify that in production mode, missing or default secrets fail fast at startup"""
    os.environ['PAYROLLX_ENV'] = 'production'
    os.environ['SECRET_KEY'] = 'payroll-secret-key-2026-razorpay-clone'  # Hardcoded default

    with pytest.raises(ValueError) as excinfo:
        validate_production_secrets()
    assert "SECRET_KEY" in str(excinfo.value)

    # Clean up environment
    os.environ['PAYROLLX_ENV'] = 'development'
    os.environ.pop('SECRET_KEY', None)


def test_rate_limiting_on_login(client, test_app):
    """Verify that rate limiter blocks rapid brute force login attempts (5 per minute)"""
    from app import limiter
    limiter.enabled = True
    try:
        status_codes = []
        for _ in range(7):
            resp = client.post(
                '/login',
                data={'username': 'baduser', 'password': 'badpassword', 'login_mode': 'admin'}
            )
            status_codes.append(resp.status_code)
        assert 429 in status_codes
    finally:
        limiter.enabled = False
        limiter.reset()


def test_register_via_html_form(client, test_app):
    """Verify GET /register renders 200, and POST /register redirects to /login on success."""
    get_resp = client.get('/register')
    assert get_resp.status_code == 200
    assert "Create your organization admin account" in get_resp.data.decode('utf-8')

    # Valid form submission
    post_resp = client.post('/register', data={
        'username': 'unique_admin_1',
        'email': 'unique1@company.com',
        'organization': 'Unique Enterprise 1',
        'password': 'SecurePassword123'
    }, follow_redirects=False)
    assert post_resp.status_code == 302
    assert '/login' in post_resp.headers['Location']

    # Duplicate username check
    dup_resp = client.post('/register', data={
        'username': 'unique_admin_1',
        'email': 'another@company.com',
        'organization': 'Another Org',
        'password': 'SecurePassword123'
    }, follow_redirects=False)
    assert dup_resp.status_code == 400
    assert "Username already exists" in dup_resp.data.decode('utf-8')


def test_register_via_json_api(client, test_app):
    """Verify /register and /api/v1/auth/register support JSON payloads and return 201."""
    json_payload = {
        'username': 'api_admin.corp',
        'email': 'api_admin@company.com',
        'organization': 'API Test Corp',
        'password': 'Password123!'
    }
    resp = client.post('/register', json=json_payload)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'success'
    assert data['user']['username'] == 'api_admin.corp'
    assert data['user']['role'] == 'hr_admin'

    # Test alias route /api/v1/auth/register
    resp2 = client.post('/api/v1/auth/register', json={
        'username': 'api_admin2',
        'email': 'api_admin2@company.com',
        'organization': 'API Test Corp 2',
        'password': 'Password123!'
    })
    assert resp2.status_code == 201
    assert resp2.get_json()['status'] == 'success'
