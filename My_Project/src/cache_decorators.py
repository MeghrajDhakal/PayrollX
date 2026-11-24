"""
PayrollX — Cache Decorators & Invalidation Policies
Provides declarative caching, cache-aside execution, and O(1) version-based
cache invalidation helpers for multi-tenant endpoints.
"""
from functools import wraps
from typing import Callable, Any, Optional, List, Union

from redis_client import (
    cache_get,
    cache_set,
    cache_delete,
    cache_delete_many,
    get_tenant_roster_version,
    bump_tenant_roster_version
)


def build_employee_cache_key(org_id: int, emp_id: int) -> str:
    """Standardized single employee cache key."""
    return f"payrollx:org:{org_id}:employee:{emp_id}"


def build_roster_cache_key(org_id: int, page: int = 1, limit: int = 50) -> str:
    """
    Standardized paginated roster cache key.
    Includes the tenant's current roster version to allow O(1) instantaneous
    invalidation of all pages without scanning Redis.
    """
    version = get_tenant_roster_version(org_id)
    return f"payrollx:org:{org_id}:employees:v{version}:p{page}:l{limit}"


def build_dashboard_summary_cache_key(org_id: int) -> str:
    """Standardized tenant dashboard analytics summary cache key."""
    return f"payrollx:org:{org_id}:dashboard:summary"


def invalidate_tenant_employee_cache(org_id: int, emp_id: Optional[int] = None) -> None:
    """
    Evict employee profile cache and bump roster version counter.
    Also invalidates the dashboard summary since employee count/status changed.
    """
    keys_to_delete = [build_dashboard_summary_cache_key(org_id)]
    if emp_id is not None:
        keys_to_delete.append(build_employee_cache_key(org_id, emp_id))

    cache_delete_many(*keys_to_delete)
    # O(1) version bump invalidates all paginated employee pages simultaneously
    bump_tenant_roster_version(org_id)


def invalidate_tenant_payroll_cache(org_id: int) -> None:
    """
    Evict dashboard summary and payroll status caches when a payroll run is created,
    approved, or finalized.
    """
    cache_delete(build_dashboard_summary_cache_key(org_id))


def cached(
    key_builder: Union[str, Callable[..., Optional[str]]],
    ttl: int = 300,
    jitter_pct: float = 0.10
):
    """
    Decorator for Cache-Aside query acceleration.
    If cached value exists, returns it directly; otherwise executes the function,
    populates the cache with a jittered TTL, and returns the result.
    """
    def decorator(fn: Callable[..., Any]):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Compute cache key
            if callable(key_builder):
                key = key_builder(*args, **kwargs)
            else:
                key = str(key_builder)

            if not key:
                return fn(*args, **kwargs)

            # 1. Check cache
            cached_data = cache_get(key)
            if cached_data is not None:
                return cached_data

            # 2. Cache miss -> execute primary source
            result = fn(*args, **kwargs)

            # 3. Store result in cache
            if result is not None:
                cache_set(key, result, ttl=ttl, jitter_pct=jitter_pct)

            return result
        return wrapper
    return decorator


def invalidate_cache(
    keys_builder: Union[str, List[str], Callable[..., List[str]]]
):
    """
    Decorator for Write-Through / Cache-Invalidation.
    Executes the underlying mutation and, on successful return, deletes the target keys.
    """
    def decorator(fn: Callable[..., Any]):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)

            # Determine keys to invalidate
            if callable(keys_builder):
                keys = keys_builder(*args, **kwargs)
            elif isinstance(keys_builder, (list, tuple)):
                keys = list(keys_builder)
            else:
                keys = [str(keys_builder)]

            if keys:
                cache_delete_many(*keys)

            return result
        return wrapper
    return decorator
