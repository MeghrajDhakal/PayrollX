"""
PayrollX — Redis Caching & Invalidation Test Suite
Validates custom serialization, TTL jitter, fail-open resilience on Redis outages,
Cache-Aside lazy loading, and O(1) roster version cache invalidation.
"""
import sys
import os
import uuid
import pytest
from datetime import datetime, date
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Ensure src/ is on sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import redis
from app import app
from models import db, Organization, User, Employee, PayrollRun, SalaryStructure
from redis_client import (
    serialize, deserialize, calculate_jittered_ttl,
    cache_get, cache_set, cache_delete, cache_delete_many,
    get_tenant_roster_version, bump_tenant_roster_version,
    set_redis_client_override, reset_redis_client, cache_ping
)
from cache_decorators import (
    build_employee_cache_key,
    build_roster_cache_key,
    build_dashboard_summary_cache_key,
    cached,
    invalidate_cache
)


class InMemoryMockRedis:
    """In-memory mock Redis client with TTL and exception simulation capabilities."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.should_fail = False
        self.failure_exception = redis.ConnectionError("Connection refused")

    def get(self, key):
        if self.should_fail:
            raise self.failure_exception
        return self.store.get(key)

    def set(self, key, value, ex=None):
        if self.should_fail:
            raise self.failure_exception
        self.store[key] = str(value) if not isinstance(value, str) else value
        if ex:
            self.ttls[key] = ex
        return True

    def setex(self, key, time_sec, value):
        if self.should_fail:
            raise self.failure_exception
        self.store[key] = str(value) if not isinstance(value, str) else value
        self.ttls[key] = time_sec
        return True

    def delete(self, *keys):
        if self.should_fail:
            raise self.failure_exception
        deleted_count = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                self.ttls.pop(k, None)
                deleted_count += 1
        return deleted_count

    def incr(self, key, amount=1):
        if self.should_fail:
            raise self.failure_exception
        current = int(self.store.get(key, 0))
        new_val = current + amount
        self.store[key] = str(new_val)
        return new_val

    def expire(self, key, seconds):
        if self.should_fail:
            raise self.failure_exception
        if key in self.store:
            self.ttls[key] = seconds
            return True
        return False

    def ping(self):
        if self.should_fail:
            raise self.failure_exception
        return True


@pytest.fixture
def mock_redis():
    """Provides a fresh InMemoryMockRedis client set as the active Redis override."""
    mock_instance = InMemoryMockRedis()
    set_redis_client_override(mock_instance)
    yield mock_instance
    reset_redis_client()


@pytest.fixture
def test_app():
    """Sets up an isolated SQLite in-memory test database."""
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SECRET_KEY'] = 'test-secret-key-redis-testing-1234567890'
    app.config['JWT_SECRET_KEY'] = 'test-jwt-secret-key-redis-1234567890'
    app.config['WTF_CSRF_ENABLED'] = False

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(test_app):
    return test_app.test_client()


# ────────────────────────────────────────────
# 1. SERIALIZER & DESERIALIZER TESTS
# ────────────────────────────────────────────

def test_safe_json_encoder_types():
    """Verify custom SafeJSONEncoder natively handles Decimal, datetime, date, and UUID."""
    test_data = {
        "decimal_val": Decimal("15000.75"),
        "datetime_val": datetime(2026, 11, 24, 12, 30, 45),
        "date_val": date(2026, 11, 24),
        "uuid_val": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "int_val": 42,
        "str_val": "payrollx"
    }

    serialized = serialize(test_data)
    assert isinstance(serialized, str)

    deserialized = deserialize(serialized)
    assert deserialized["decimal_val"] == 15000.75
    assert deserialized["datetime_val"] == "2026-11-24T12:30:45"
    assert deserialized["date_val"] == "2026-11-24"
    assert deserialized["uuid_val"] == "12345678-1234-5678-1234-567812345678"
    assert deserialized["int_val"] == 42


def test_deserialize_none_or_bytes():
    """Verify deserialize safely handles None and bytes inputs."""
    assert deserialize(None) is None
    bytes_data = b'{"status": "ok"}'
    assert deserialize(bytes_data) == {"status": "ok"}


# ────────────────────────────────────────────
# 2. TTL JITTER TESTS
# ────────────────────────────────────────────

def test_calculate_jittered_ttl():
    """Verify jittered TTL remains within [base * (1 - jitter), base * (1 + jitter)]."""
    base_ttl = 300
    jitter_pct = 0.10
    min_expected = int(base_ttl * (1 - jitter_pct))  # 270
    max_expected = int(base_ttl * (1 + jitter_pct))  # 330

    sampled_ttls = [calculate_jittered_ttl(base_ttl, jitter_pct) for _ in range(50)]
    for ttl in sampled_ttls:
        assert min_expected <= ttl <= max_expected

    # Verify not all values are identical (randomness check)
    assert len(set(sampled_ttls)) > 1


def test_calculate_jittered_ttl_non_positive():
    """Verify non-positive TTLs return unadjusted without error."""
    assert calculate_jittered_ttl(0) == 0
    assert calculate_jittered_ttl(-10) == -10


# ────────────────────────────────────────────
# 3. BASIC CACHE OPERATIONS & ROSTER VERSIONING
# ────────────────────────────────────────────

def test_cache_set_and_get(mock_redis):
    """Verify basic cache set and get operations."""
    key = "payrollx:org:1:test_key"
    val = {"status": "active", "count": 10}

    assert cache_set(key, val, ttl=300) is True
    cached_val = cache_get(key)
    assert cached_val == val


def test_cache_delete_and_delete_many(mock_redis):
    """Verify single and multiple key deletion."""
    k1 = "payrollx:test:1"
    k2 = "payrollx:test:2"
    cache_set(k1, {"a": 1})
    cache_set(k2, {"b": 2})

    assert cache_delete(k1) is True
    assert cache_get(k1) is None
    assert cache_get(k2) is not None

    deleted_count = cache_delete_many(k2, "non_existent_key")
    assert deleted_count == 1
    assert cache_get(k2) is None


def test_tenant_roster_version_increment(mock_redis):
    """Verify tenant roster version initializes to 1 and increments on bump in O(1)."""
    org_id = 99
    # Initial version query initializes to 1
    v1 = get_tenant_roster_version(org_id)
    assert v1 == 1

    # Key builder uses current version
    key_v1 = build_roster_cache_key(org_id, page=1, limit=50)
    assert f":v1:p1:l50" in key_v1

    # Bump version
    v2 = bump_tenant_roster_version(org_id)
    assert v2 == 2

    # New key builder uses updated version
    key_v2 = build_roster_cache_key(org_id, page=1, limit=50)
    assert f":v2:p1:l50" in key_v2
    assert key_v1 != key_v2


# ────────────────────────────────────────────
# 4. FAIL-OPEN RESILIENCE TESTS
# ────────────────────────────────────────────

def test_fail_open_on_redis_connection_error(mock_redis):
    """Verify that when Redis throws ConnectionError, helpers return None/False without crashing."""
    mock_redis.should_fail = True
    mock_redis.failure_exception = redis.ConnectionError("Could not connect to Redis server: Connection refused")

    assert cache_get("any_key") is None
    assert cache_set("any_key", {"data": 123}, ttl=60) is False
    assert cache_delete("any_key") is False
    assert cache_delete_many("key1", "key2") == 0
    assert get_tenant_roster_version(1) == 1
    assert bump_tenant_roster_version(1) == 1
    assert cache_ping() is False


def test_fail_open_on_redis_timeout_error(mock_redis):
    """Verify that when Redis throws TimeoutError, operations fail open gracefully."""
    mock_redis.should_fail = True
    mock_redis.failure_exception = redis.TimeoutError("Socket read timed out (300ms limit)")

    assert cache_get("any_key") is None
    assert cache_set("any_key", {"data": 123}, ttl=60) is False
    assert cache_delete("any_key") is False


def test_fail_open_when_redis_disabled():
    """Verify fail-open behavior when REDIS_ENABLED is false."""
    with patch.dict(os.environ, {"REDIS_ENABLED": "false"}):
        reset_redis_client()
        assert cache_get("any_key") is None
        assert cache_set("any_key", {"data": 123}) is False
        assert cache_ping() is False


# ────────────────────────────────────────────
# 5. CACHE DECORATORS TESTS
# ────────────────────────────────────────────

def test_cached_decorator(mock_redis):
    """Test @cached executes function on miss, stores in Redis, and returns cached on hit."""
    call_count = 0

    @cached(key_builder=lambda x: f"payrollx:test:item:{x}", ttl=120)
    def compute_expensive_data(x):
        nonlocal call_count
        call_count += 1
        return {"result": x * 2}

    # 1. Cache Miss -> Executes function
    res1 = compute_expensive_data(10)
    assert res1 == {"result": 20}
    assert call_count == 1

    # 2. Cache Hit -> Returns cached copy, does NOT execute function
    res2 = compute_expensive_data(10)
    assert res2 == {"result": 20}
    assert call_count == 1  # Function not called again!


def test_invalidate_cache_decorator(mock_redis):
    """Test @invalidate_cache evicts designated keys on successful execution."""
    cache_key = "payrollx:test:mutate:1"
    cache_set(cache_key, {"old": "data"})
    assert cache_get(cache_key) is not None

    @invalidate_cache(keys_builder=lambda item_id: [f"payrollx:test:mutate:{item_id}"])
    def update_item(item_id):
        return True

    update_item(1)
    assert cache_get(cache_key) is None


# ────────────────────────────────────────────
# 6. END-TO-END APPLICATION INTEGRATION TESTS
# ────────────────────────────────────────────

def test_employee_data_cache_hit_and_miss(client, test_app, mock_redis):
    """Verify /api/v1/employee/data populates Redis on miss and serves from Redis on hit."""
    with test_app.app_context():
        org = Organization(name="Cache Test Org")
        db.session.add(org)
        db.session.commit()

        admin = User(username="admin_user", email="admin@test.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("adminpass123")
        emp = Employee(
            first_name="Alice", last_name="Smith", email="alice@test.fake",
            org_id=org.org_id, department="Engineering", designation="Lead Dev"
        )
        db.session.add_all([admin, emp])
        db.session.commit()
        emp_id = emp.emp_id
        org_id = org.org_id

    # Get admin auth token
    token_resp = client.post('/api/v1/auth/token', json={'username': 'admin_user', 'password': 'adminpass123'})
    assert token_resp.status_code == 200
    token = token_resp.get_json()['access_token']

    expected_cache_key = build_employee_cache_key(org_id, emp_id)
    assert mock_redis.get(expected_cache_key) is None

    # First request: Cache Miss -> queries DB and populates cache
    resp1 = client.get(f'/api/v1/employee/data?emp_id={emp_id}', headers={'Authorization': f'Bearer {token}'})
    assert resp1.status_code == 200
    assert resp1.get_json()['full_name'] == 'Alice Smith'

    # Cache should now hold the serialized payload
    assert mock_redis.get(expected_cache_key) is not None

    # Modify the database record directly without invalidating cache
    with test_app.app_context():
        emp_db = db.session.get(Employee, emp_id)
        emp_db.first_name = "ModifiedInDB"
        db.session.commit()

    # Second request: Cache Hit -> returns cached 'Alice Smith' instead of modified DB name
    resp2 = client.get(f'/api/v1/employee/data?emp_id={emp_id}', headers={'Authorization': f'Bearer {token}'})
    assert resp2.status_code == 200
    assert resp2.get_json()['full_name'] == 'Alice Smith'  # Served directly from cache!


def test_employee_creation_invalidates_roster_and_dashboard(client, test_app, mock_redis):
    """Verify adding an employee bumps roster version and evicts dashboard summary cache."""
    with test_app.app_context():
        org = Organization(name="Invalidation Org")
        db.session.add(org)
        db.session.commit()

        admin = User(username="inv_admin", email="inv_admin@test.fake", role="hr_admin", org_id=org.org_id)
        admin.set_password("adminpass123")
        db.session.add(admin)
        db.session.commit()
        org_id = org.org_id

    # Populate initial dashboard cache
    dash_key = build_dashboard_summary_cache_key(org_id)
    cache_set(dash_key, {"total_employees": 0, "pending_runs": 0, "finalized_runs": 0, "total_payroll": 0})
    assert cache_get(dash_key) is not None

    # Initial roster version is 1
    assert get_tenant_roster_version(org_id) == 1

    # Log in as hr_admin
    client.post('/login', data={'username': 'inv_admin', 'password': 'adminpass123', 'login_mode': 'admin'})

    # Create new employee via /people/add
    payload = {
        'first_name': 'Robert',
        'last_name': 'Williams',
        'email': 'robert@test.fake',
        'phone': '9876543210',
        'pan_number': 'ABCDE1234F',
        'aadhaar_number': '123456789012',
        'bank_account': '987654321012',
        'bank_ifsc': 'HDFC0001234',
        'bank_name': 'HDFC Bank',
        'ctc': 800000,
        'department': 'Operations',
        'designation': 'Manager'
    }
    resp = client.post('/people/add', data=payload, follow_redirects=False)
    assert resp.status_code == 302

    # Verify dashboard cache key was evicted
    assert cache_get(dash_key) is None

    # Verify tenant roster version counter was incremented to 2 in O(1)
    assert get_tenant_roster_version(org_id) == 2


def test_fail_open_in_route_when_redis_offline(client, test_app, mock_redis):
    """Verify route continues to return 200 from primary DB even when Redis fails completely."""
    with test_app.app_context():
        org = Organization(name="FailOpen Org")
        db.session.add(org)
        db.session.commit()

        emp_user = User(username="emp_failopen", email="emp@failopen.fake", role="employee", org_id=org.org_id)
        emp_user.set_password("emppass123")
        emp = Employee(
            first_name="FailOpen", last_name="User", email="emp@failopen.fake",
            org_id=org.org_id, department="Support", designation="Agent", user_id=emp_user.id
        )
        db.session.add_all([emp_user, emp])
        db.session.commit()
        emp.user_id = emp_user.id
        db.session.commit()

    # Get JWT token
    token_resp = client.post('/api/v1/auth/token', json={'username': 'emp_failopen', 'password': 'emppass123'})
    assert token_resp.status_code == 200
    token = token_resp.get_json()['access_token']

    # Turn on Redis failure simulation
    mock_redis.should_fail = True
    mock_redis.failure_exception = redis.ConnectionError("Redis connection dropped")

    # Request must not fail with 500 — it must seamlessly fall back to DB query and return 200
    resp = client.get('/api/v1/employee/data', headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['full_name'] == 'FailOpen User'
    assert data['department'] == 'Support'
