"""
PayrollX — Structured JSON Logging & PII Masking
Ensures all logs are emitted as structured JSON with request correlation and PII protection.
"""
import logging
import json
import re
import time
from datetime import datetime
from flask import has_request_context, request, g
from flask_login import current_user

# PII Masking regular expressions
RE_EMAIL = re.compile(r'([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]*@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)')
RE_PAN = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b')
RE_AADHAAR = re.compile(r'\b[0-9]{12}\b')
RE_BANK = re.compile(r'\b[0-9]{9,18}\b')
RE_TOKEN = re.compile(r'(Bearer\s+[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.?[A-Za-z0-9\-_=]*)', re.IGNORECASE)
RE_PASSWORD = re.compile(r'(["\']?password["\']?\s*[:=]\s*["\'])([^"\']+)(["\'])', re.IGNORECASE)


def mask_pii(text: str) -> str:
    """Mask sensitive PII fields (PAN, Aadhaar, Bank account, password, tokens, emails)."""
    if not isinstance(text, str):
        text = str(text)

    # Mask passwords
    text = RE_PASSWORD.sub(r'\1******\3', text)
    # Mask Bearer tokens
    text = RE_TOKEN.sub(r'Bearer [REDACTED_TOKEN]', text)
    # Mask PAN: show only last 4 digits + letter (e.g., *****1234F)
    text = RE_PAN.sub(lambda m: '*****' + m.group(0)[5:], text)
    # Mask Aadhaar: show only last 4 digits
    text = RE_AADHAAR.sub(lambda m: '********' + m.group(0)[8:], text)
    # Mask Bank account: show only last 4 digits
    text = RE_BANK.sub(lambda m: '****' + m.group(0)[-4:], text)
    # Mask Emails: j***@example.com
    text = RE_EMAIL.sub(r'\1***@\2', text)

    return text


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as JSON strings with request metadata and PII masking."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': mask_pii(record.getMessage()),
        }

        if record.exc_info:
            log_obj['exception'] = mask_pii(self.formatException(record.exc_info))

        # Attach request context if available
        if has_request_context():
            log_obj['request_id'] = getattr(g, 'request_id', None)
            log_obj['route'] = request.path
            log_obj['method'] = request.method
            log_obj['ip'] = request.headers.get('X-Forwarded-For', request.remote_addr)

            # Tenant & User ID
            user_id = None
            org_id = None
            if hasattr(g, 'current_api_user') and g.current_api_user:
                user_id = g.current_api_user.id
                org_id = g.current_api_user.org_id
            elif current_user and current_user.is_authenticated:
                user_id = current_user.id
                org_id = current_user.org_id
            elif hasattr(g, 'jwt_claims') and g.jwt_claims:
                user_id = g.jwt_claims.get('sub')
                org_id = g.jwt_claims.get('org_id')

            log_obj['user_id'] = user_id
            log_obj['tenant_id'] = org_id

            if hasattr(g, 'status_code'):
                log_obj['status_code'] = g.status_code
            if hasattr(g, 'latency_ms'):
                log_obj['latency_ms'] = g.latency_ms

        return json.dumps(log_obj)


def configure_logging(app):
    """Configure structured JSON logging for the Flask app."""
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJsonFormatter())

    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

    # Also attach to root / gunicorn logger
    logging.getLogger('werkzeug').handlers.clear()
    logging.getLogger('werkzeug').addHandler(handler)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    logging.getLogger('gunicorn.access').handlers.clear()
    logging.getLogger('gunicorn.access').addHandler(handler)
    logging.getLogger('gunicorn.error').handlers.clear()
    logging.getLogger('gunicorn.error').addHandler(handler)
