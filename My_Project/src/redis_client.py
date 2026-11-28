"""
PayrollX — Resilient Redis Caching Client
Provides connection pooling, strict latency timeouts, custom serialization,
TTL jitter, and fail-open resilience against Redis outages.
"""
import json
import logging
import os
import random
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import redis

logger = logging.getLogger("payrollx.cache")

# Global pool and client singletons
_pool: redis.ConnectionPool | None = None
_client: redis.Redis | None = None
_override_client: Any | None = None
_circuit_open_until: float = 0.0
_CIRCUIT_COOLDOWN_SECONDS: float = 30.0
_REDIS_NETWORK_ERRORS = (redis.ConnectionError, redis.TimeoutError, redis.RedisError)
_FAIL_OPEN_ERRORS = (ValueError, TypeError, OSError, RuntimeError, ArithmeticError, AttributeError)


def _trip_circuit_if_real() -> None:
    """Open short circuit-breaker cooldown after a real Redis network failure."""
    global _circuit_open_until
    if _override_client is None:
        _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS


class SafeJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder supporting datetime, date, Decimal, and UUID objects."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime | date):
            return o.isoformat()
        elif isinstance(o, Decimal):
            return float(o)
        elif isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


def serialize(data: Any) -> str:
    """Serialize data into a JSON string using SafeJSONEncoder."""
    return json.dumps(data, cls=SafeJSONEncoder)


def deserialize(raw: str | bytes | None) -> Any:
    """Deserialize a JSON string or bytes into native Python objects."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def calculate_jittered_ttl(base_ttl: int, jitter_pct: float = 0.10) -> int:
    """
    Apply uniform random jitter to a base TTL to prevent cache stampedes (dogpiling).
    Example: 300s ± 10% -> [270s, 330s].
    """
    if base_ttl <= 0:
        return base_ttl
    jitter = base_ttl * random.uniform(-jitter_pct, jitter_pct)
    return max(1, int(base_ttl + jitter))


def get_redis_client() -> redis.Redis | None:
    """
    Return a pooled Redis client singleton with explicit connect/read/write timeouts.
    If Redis is disabled or fails to initialize, returns None (fail-open).
    """
    global _pool, _client
    if _override_client is not None:
        return _override_client

    if _circuit_open_until and time.monotonic() < _circuit_open_until:
        return None

    redis_enabled = os.environ.get("REDIS_ENABLED", "true").lower() in ("true", "1", "yes")
    raw_url = os.environ.get("REDIS_URL")
    is_serverless = bool(os.environ.get("VERCEL") == "1" or os.environ.get("VERCEL_ENV") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    # Avoid dialing localhost:6379 in serverless deployments unless REDIS_URL is configured
    if is_serverless and not raw_url:
        return None
    redis_url = raw_url or "redis://localhost:6379/0"

    if not redis_enabled or not redis_url:
        return None

    if _client is not None:
        return _client

    try:
        default_max_connections = 10 if is_serverless else 20
        max_connections = int(os.environ.get("REDIS_MAX_CONNECTIONS", str(default_max_connections)))
        connect_timeout = float(os.environ.get("REDIS_CONNECT_TIMEOUT", "0.2"))  # 200ms
        socket_timeout = float(os.environ.get("REDIS_SOCKET_TIMEOUT", "0.3"))    # 300ms

        _pool = redis.ConnectionPool.from_url(
            redis_url,
            max_connections=max_connections,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            retry_on_timeout=False,
            decode_responses=True
        )
        _client = redis.Redis(connection_pool=_pool)
        return _client
    except (*_REDIS_NETWORK_ERRORS, *_FAIL_OPEN_ERRORS) as exc:
        logger.warning(
            "Redis client initialization failed; continuing in fail-open mode",
            extra={"error": str(exc)}
        )
        return None


def set_redis_client_override(client: Any | None) -> None:
    """Set an override client instance for testing or mocking."""
    global _override_client, _circuit_open_until
    _override_client = client
    _circuit_open_until = 0.0


def reset_redis_client() -> None:
    """Reset the singleton client and pool connections."""
    global _pool, _client, _override_client, _circuit_open_until
    _override_client = None
    _circuit_open_until = 0.0
    if _client is not None:
        try:
            _client.close()
        except (*_REDIS_NETWORK_ERRORS, *_FAIL_OPEN_ERRORS) as exc:
            logger.debug("Ignored Redis client close error during reset: %s", exc)
        _client = None
    if _pool is not None:
        try:
            _pool.disconnect()
        except (*_REDIS_NETWORK_ERRORS, *_FAIL_OPEN_ERRORS) as exc:
            logger.debug("Ignored Redis pool disconnect error during reset: %s", exc)
        _pool = None


def cache_get(key: str) -> Any | None:
    """
    Fetch and deserialize a key from Redis.
    Fails open by returning None on Redis error or connection timeout.
    """
    client = get_redis_client()
    if client is None:
        return None

    try:
        raw = cast("str | bytes | None", client.get(key))
        if raw is None:
            return None
        return deserialize(raw)
    except _REDIS_NETWORK_ERRORS as exc:
        _trip_circuit_if_real()
        logger.warning(
            "Redis cache_get failed; falling back to primary database",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return None
    except _FAIL_OPEN_ERRORS as exc:
        logger.warning(
            "Unexpected error in cache_get; falling back to primary database",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return None


def cache_set(key: str, value: Any, ttl: int | None = None, jitter_pct: float = 0.10) -> bool:
    """
    Serialize and store a value in Redis with optional jittered TTL.
    Fails open by returning False without raising exceptions on failure.
    """
    client = get_redis_client()
    if client is None:
        return False

    try:
        payload = serialize(value)
        effective_ttl = calculate_jittered_ttl(ttl, jitter_pct) if ttl else None
        if effective_ttl:
            client.setex(key, effective_ttl, payload)
        else:
            client.set(key, payload)
        return True
    except _REDIS_NETWORK_ERRORS as exc:
        _trip_circuit_if_real()
        logger.warning(
            "Redis cache_set failed; cache population skipped",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return False
    except _FAIL_OPEN_ERRORS as exc:
        logger.warning(
            "Unexpected error in cache_set; cache population skipped",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return False


def cache_delete(key: str) -> bool:
    """
    Evict a single cache key from Redis.
    Fails open by returning False on error.
    """
    client = get_redis_client()
    if client is None:
        return False

    try:
        client.delete(key)
        return True
    except _REDIS_NETWORK_ERRORS as exc:
        logger.warning(
            "Redis cache_delete failed",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return False
    except _FAIL_OPEN_ERRORS as exc:
        logger.warning(
            "Unexpected error in cache_delete",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return False


def cache_delete_many(*keys: str) -> int:
    """
    Evict multiple cache keys in a single round-trip.
    Fails open by returning 0 on error.
    """
    valid_keys = [k for k in keys if k]
    if not valid_keys:
        return 0

    client = get_redis_client()
    if client is None:
        return 0

    try:
        return cast("int", client.delete(*valid_keys))
    except _REDIS_NETWORK_ERRORS as exc:
        logger.warning(
            "Redis cache_delete_many failed",
            extra={"keys_count": len(valid_keys), "error_type": type(exc).__name__, "error": str(exc)}
        )
        return 0
    except _FAIL_OPEN_ERRORS as exc:
        logger.warning(
            "Unexpected error in cache_delete_many",
            extra={"keys_count": len(valid_keys), "error_type": type(exc).__name__, "error": str(exc)}
        )
        return 0


def get_tenant_roster_version(org_id: int) -> int:
    """
    Return the current roster version for a tenant.
    Used for O(1) cache key construction for paginated rosters.
    Defaults to 1 if not set or Redis is unavailable.
    """
    client = get_redis_client()
    if client is None:
        return 1

    key = f"payrollx:org:{org_id}:roster_version"
    try:
        val = cast("str | bytes | int | None", client.get(key))
        if val is None:
            # Initialize version to 1 with 7-day TTL
            client.set(key, "1", ex=604800)
            return 1
        return int(val)
    except _REDIS_NETWORK_ERRORS as exc:
        logger.warning(
            "Failed to retrieve tenant roster version from Redis; defaulting to 1",
            extra={"org_id": org_id, "error": str(exc)}
        )
        return 1
    except _FAIL_OPEN_ERRORS as exc:
        logger.warning(
            "Unexpected error retrieving tenant roster version; defaulting to 1",
            extra={"org_id": org_id, "error": str(exc)}
        )
        return 1


def bump_tenant_roster_version(org_id: int) -> int:
    """
    Increment the tenant's roster version counter in O(1) time.
    Instantly invalidates all previously cached roster pages without SCAN/KEYS sweeps.
    """
    client = get_redis_client()
    if client is None:
        return 1

    key = f"payrollx:org:{org_id}:roster_version"
    try:
        new_ver = cast("int | str | bytes", client.incr(key))
        client.expire(key, 604800)  # Keep key alive for 7 days
        return int(new_ver)
    except _REDIS_NETWORK_ERRORS as exc:
        logger.warning(
            "Failed to bump tenant roster version in Redis",
            extra={"org_id": org_id, "error": str(exc)}
        )
        return 1
    except _FAIL_OPEN_ERRORS as exc:
        logger.warning(
            "Unexpected error bumping tenant roster version",
            extra={"org_id": org_id, "error": str(exc)}
        )
        return 1


def cache_ping() -> bool:
    """Health check helper to verify Redis connectivity."""
    client = get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except (*_REDIS_NETWORK_ERRORS, *_FAIL_OPEN_ERRORS):
        _trip_circuit_if_real()
        return False
