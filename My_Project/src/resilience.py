"""
PayrollX — Resilience & External Call Utilities
Configured with strict connect and read timeouts and graceful failure handling.
"""
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger('payrollx.resilience')

DEFAULT_CONNECT_TIMEOUT = 3.0  # seconds
DEFAULT_READ_TIMEOUT = 10.0    # seconds
DEFAULT_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)


class ExternalCallResult:
    """Wrapper for external call outcome."""
    def __init__(self, success: bool, status_code: Optional[int] = None, data: Any = None, error: Optional[str] = None):
        self.success = success
        self.status_code = status_code
        self.data = data
        self.error = error


def safe_external_call(
    url: str,
    method: str = 'GET',
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: tuple = DEFAULT_TIMEOUT,
    service_name: str = "external_service"
) -> ExternalCallResult:
    """
    Execute an external HTTP call with explicit connect and read timeouts.
    Failures are handled gracefully so external services never crash or block
    core payroll operations.
    """
    try:
        import requests
    except ImportError:
        logger.warning(f"[{service_name}] requests library not available; skipping external call.")
        return ExternalCallResult(success=False, error="requests library not installed")

    try:
        kwargs: Dict[str, Any] = {
            'timeout': timeout,
            'headers': headers or {}
        }
        if payload is not None:
            if method.upper() in ('POST', 'PUT', 'PATCH'):
                kwargs['json'] = payload
            else:
                kwargs['params'] = payload

        response = requests.request(method.upper(), url, **kwargs)
        return ExternalCallResult(
            success=response.ok,
            status_code=response.status_code,
            data=response.text,
            error=None if response.ok else f"HTTP {response.status_code}"
        )
    except requests.exceptions.ConnectTimeout:
        logger.error(f"[{service_name}] Connection timed out after {timeout[0]}s connecting to {url}")
        return ExternalCallResult(success=False, error=f"Connection timeout ({timeout[0]}s)")
    except requests.exceptions.ReadTimeout:
        logger.error(f"[{service_name}] Read timed out after {timeout[1]}s from {url}")
        return ExternalCallResult(success=False, error=f"Read timeout ({timeout[1]}s)")
    except requests.exceptions.RequestException as e:
        logger.error(f"[{service_name}] Network/HTTP error: {str(e)}")
        return ExternalCallResult(success=False, error=str(e))
    except Exception as e:
        logger.exception(f"[{service_name}] Unexpected error during external call: {str(e)}")
        return ExternalCallResult(success=False, error=str(e))
