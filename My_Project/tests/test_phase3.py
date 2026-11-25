import sys
import os
import pytest

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from app import app
from constants import MONTH_NAMES, get_month_name
from models import db, Organization, User, Employee
import gunicorn_config


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

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        limiter.reset()


@pytest.fixture
def client(test_app):
    return test_app.test_client()


def test_health_check_endpoint(client):
    """Test /health returns 200 and healthy status with database connectivity"""
    resp = client.get('/health')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'healthy'
    assert data['database'] == 'connected'
    assert 'request_id' in data
    assert 'X-Request-ID' in resp.headers


def test_unhandled_exception_does_not_leak_stack_trace(client, test_app):
    """Trigger a controlled error and verify generic response without stack trace exposure"""
    resp = client.get('/api/v1/test/error-trigger', headers={'Accept': 'application/json'})
    assert resp.status_code == 500
    data = resp.get_json()
    assert "secret_api_key_xyz" not in json_str(data)
    assert "internal server error" in data.get('error', '').lower()
    assert 'request_id' in data


def test_pagination_bounds(client, test_app):
    """Test list endpoints apply pagination and cap per_page limit"""
    with test_app.app_context():
        org = Organization(name="Pagination Org")
        db.session.add(org)
        db.session.commit()

        admin = User(username="pag_admin", email="pag_admin@test.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("pass123")
        db.session.add(admin)

        # Create 15 employees
        for i in range(15):
            e = Employee(
                first_name=f"Emp{i}", last_name="Test", email=f"emp{i}@pag.fake",
                org_id=org.org_id, department="Engineering", designation="Dev"
            )
            db.session.add(e)
        db.session.commit()

    token_resp = client.post('/api/v1/auth/token', json={'username': 'pag_admin', 'password': 'pass123'})
    token = token_resp.get_json()['access_token']

    # Request with per_page=5
    resp = client.get('/api/v1/employees?page=1&per_page=5', headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data['employees']) == 5
    assert data['total'] == 15
    assert data['page'] == 1

    # Request with oversized per_page (should be capped at 50)
    resp2 = client.get('/api/v1/employees?per_page=9999', headers={'Authorization': f'Bearer {token}'})
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert len(data2['employees']) <= 50


def test_gunicorn_config():
    """Verify Gunicorn production settings are correctly configured"""
    assert gunicorn_config.worker_class == "gthread"
    assert gunicorn_config.timeout >= 30
    assert gunicorn_config.keepalive >= 2
    assert gunicorn_config.max_requests >= 500


def test_month_constants_and_employee_to_dict(test_app):
    """Verify immutable MONTH_NAMES constant and Employee.to_dict() serialization."""
    assert isinstance(MONTH_NAMES, tuple)
    assert len(MONTH_NAMES) == 12
    assert get_month_name(1) == "January"
    assert get_month_name(12) == "December"
    assert get_month_name(13) == "13"

    with test_app.app_context():
        org = Organization(name="Serialization Org")
        db.session.add(org)
        db.session.commit()

        emp = Employee(
            first_name="Jane", last_name="Doe", email="jane@ser.fake",
            phone="+919999999999", org_id=org.org_id,
            department="Finance", designation="Analyst"
        )
        db.session.add(emp)
        db.session.commit()

        summary = emp.to_dict()
        assert summary == {
            "emp_id": emp.emp_id,
            "full_name": "Jane Doe",
            "email": "jane@ser.fake",
            "department": "Finance",
            "designation": "Analyst",
            "status": "active",
        }

        detailed = emp.to_dict(include_details=True)
        assert detailed["phone"] == "+919999999999"
        assert detailed["organization"] == "Serialization Org"
        assert "date_of_joining" in detailed


def json_str(obj):
    import json
    return json.dumps(obj)
