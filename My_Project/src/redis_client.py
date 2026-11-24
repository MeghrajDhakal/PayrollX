"""
PayrollX — Resilient Redis Caching Client
Provides connection pooling, strict latency timeouts, custom serialization,
TTL jitter, and fail-open resilience against Redis outages.
"""
import os
import json
import uuid
import random
import logging
from decimal import Decimal
from datetime import datetime, date
from typing import Any, Optional, Union

import redis

logger = logging.getLogger("payrollx.cache")

# Global pool and client singletons
_pool: Optional[redis.ConnectionPool] = None
_client: Optional[redis.Redis] = None
_override_client: Optional[Any] = None


class SafeJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder supporting datetime, date, Decimal, and UUID objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


def serialize(data: Any) -> str:
    """Serialize data into a JSON string using SafeJSONEncoder."""
    return json.dumps(data, cls=SafeJSONEncoder)


def deserialize(raw: Optional[Union[str, bytes]]) -> Any:
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


def get_redis_client() -> Optional[redis.Redis]:
    """
    Return a pooled Redis client singleton with explicit connect/read/write timeouts.
    If Redis is disabled or fails to initialize, returns None (fail-open).
    """
    global _pool, _client, _override_client
    if _override_client is not None:
        return _override_client

    redis_enabled = os.environ.get("REDIS_ENABLED", "true").lower() in ("true", "1", "yes")
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    if not redis_enabled or not redis_url:
        return None

    if _client is not None:
        return _client

    try:
        is_serverless = bool(os.environ.get("VERCEL") == "1" or os.environ.get("VERCEL_ENV") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
        default_max_connections = 10 if is_serverless else 20
        max_connections = int(os.environ.get("REDIS_MAX_CONNECTIONS", default_max_connections))
        connect_timeout = float(os.environ.get("REDIS_CONNECT_TIMEOUT", 0.2))  # 200ms
        socket_timeout = float(os.environ.get("REDIS_SOCKET_TIMEOUT", 0.3))    # 300ms

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
    except Exception as exc:
        logger.warning(
            "Redis client initialization failed; continuing in fail-open mode",
            extra={"error": str(exc)}
        )
        return None


def set_redis_client_override(client: Optional[Any]) -> None:
    """Set an override client instance for testing or mocking."""
    global _override_client
    _override_client = client


def reset_redis_client() -> None:
    """Reset the singleton client and pool connections."""
    global _pool, _client, _override_client
    _override_client = None
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
    if _pool is not None:
        try:
            _pool.disconnect()
        except Exception:
            pass
        _pool = None


def cache_get(key: str) -> Optional[Any]:
    """
    Fetch and deserialize a key from Redis.
    Fails open by returning None on Redis error or connection timeout.
    """
    client = get_redis_client()
    if client is None:
        return None

    try:
        raw = client.get(key)
        if raw is None:
            return None
        return deserialize(raw)
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:
        logger.warning(
            "Redis cache_get failed; falling back to primary database",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return None
    except Exception as exc:
        logger.warning(
            "Unexpected error in cache_get; falling back to primary database",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return None


def cache_set(key: str, value: Any, ttl: Optional[int] = None, jitter_pct: float = 0.10) -> bool:
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
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:
        logger.warning(
            "Redis cache_set failed; cache population skipped",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return False
    except Exception as exc:
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
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:
        logger.warning(
            "Redis cache_delete failed",
            extra={"key": key, "error_type": type(exc).__name__, "error": str(exc)}
        )
        return False
    except Exception as exc:
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
        return client.delete(*valid_keys)
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:
        logger.warning(
            "Redis cache_delete_many failed",
            extra={"keys_count": len(valid_keys), "error_type": type(exc).__name__, "error": str(exc)}
        )
        return 0
    except Exception as exc:
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
        val = client.get(key)
        if val is None:
            # Initialize version to 1 with 7-day TTL
            client.set(key, "1", ex=604800)
            return 1
        return int(val)
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:
        logger.warning(
            "Failed to retrieve tenant roster version from Redis; defaulting to 1",
            extra={"org_id": org_id, "error": str(exc)}
        )
        return 1
    except Exception as exc:
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
        new_ver = client.incr(key)
        client.expire(key, 604800)  # Keep key alive for 7 days
        return int(new_ver)
    except (redis.ConnectionError, redis.TimeoutError, redis.RedisError) as exc:
        logger.warning(
            "Failed to bump tenant roster version in Redis",
            extra={"org_id": org_id, "error": str(exc)}
        )
        return 1
    except Exception as exc:
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
    except Exception:
        return False
