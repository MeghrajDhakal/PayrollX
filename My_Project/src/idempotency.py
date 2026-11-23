"""
PayrollX — Idempotency Key Manager
Atomic storage and lookup for state-changing operations to guarantee duplicate protection.
"""
import hashlib
import json
from typing import Optional, Tuple, Any
from flask import request, Response, jsonify
from models import db, IdempotencyRecord


def get_idempotency_key() -> Optional[str]:
    """Extract idempotency key from request header or body."""
    key = (
        request.headers.get('Idempotency-Key') or
        request.headers.get('X-Idempotency-Key')
    )
    if not key and request.is_json:
        data = request.get_json(silent=True) or {}
        key = data.get('idempotency_key')
    elif not key and request.form:
        key = request.form.get('idempotency_key')
    return key.strip() if key else None


def compute_request_hash(payload: Any = None) -> str:
    """Compute sha256 checksum of request method, path, and payload."""
    hasher = hashlib.sha256()
    hasher.update(request.method.encode('utf-8'))
    hasher.update(request.path.encode('utf-8'))
    if payload is None:
        if request.is_json:
            payload = request.get_data()
        elif request.form:
            payload = sorted(request.form.items())
        else:
            payload = b''

    if isinstance(payload, bytes):
        hasher.update(payload)
    elif isinstance(payload, str):
        hasher.update(payload.encode('utf-8'))
    else:
        hasher.update(json.dumps(payload, sort_keys=True, default=str).encode('utf-8'))
    return hasher.hexdigest()


def get_idempotent_response(key: str, org_id: int) -> Optional[Tuple[int, str]]:
    """Retrieve saved idempotent response if available."""
    if not key or not org_id:
        return None
    record = IdempotencyRecord.query.filter_by(key=key, org_id=org_id).first()
    if record:
        return record.response_code, record.response_body
    return None


def save_idempotent_response(key: str, org_id: int, endpoint: str, request_hash: str, response_code: int, response_body: str):
    """Save response atomically in IdempotencyRecord."""
    if not key or not org_id:
        return
    try:
        record = IdempotencyRecord(
            key=key,
            org_id=org_id,
            endpoint=endpoint,
            request_hash=request_hash,
            response_code=response_code,
            response_body=response_body
        )
        db.session.add(record)
        db.session.commit()
    except Exception:
        db.session.rollback()
