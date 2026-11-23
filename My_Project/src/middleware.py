"""
PayrollX — Production-Ready Auth Middleware
JWT Token Management, Role-Based Access Decorators, and Request Interceptor.
"""
import jwt
import os
import uuid
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, current_app, g
from flask_login import current_user
from models import User, AuditLog, RevokedToken, db


# ────────────────────────────────────────────
# JWT TOKEN MANAGEMENT
# ────────────────────────────────────────────
def generate_jwt(user):
    """Issue a signed JWT containing sub (User ID), org_id, role, session_token, and unique jti.

    Token structure:
        {
            "sub": <user.id>,
            "org_id": <user.org_id>,
            "role": <user.role>,
            "username": <user.username>,
            "session_token": <user.session_token>,
            "jti": <unique_uuid>,
            "iat": <issued_at>,
            "exp": <expiration>
        }
    """
    now = datetime.utcnow()
    expiry_hours = current_app.config.get('JWT_EXPIRY_HOURS', 8)
    jti = str(uuid.uuid4())
    payload = {
        'sub': user.id,
        'org_id': user.org_id,
        'role': user.role,
        'username': user.username,
        'session_token': user.session_token,
        'jti': jti,
        'iat': now,
        'exp': now + timedelta(hours=expiry_hours)
    }
    secret = current_app.config['JWT_SECRET_KEY']
    algorithm = current_app.config.get('JWT_ALGORITHM', 'HS256')
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_jwt(token):
    """Validate JWT signature and expiry. Returns decoded claims dict.

    Raises:
        jwt.ExpiredSignatureError: Token has expired
        jwt.InvalidTokenError: Token is malformed or tampered
    """
    secret = current_app.config['JWT_SECRET_KEY']
    algorithm = current_app.config.get('JWT_ALGORITHM', 'HS256')
    return jwt.decode(token, secret, algorithms=[algorithm])


def _extract_jwt_from_request():
    """Extract JWT token from the Authorization header.

    Supports format: Authorization: Bearer <token>
    Returns None if no valid header is present.
    """
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:].strip()
    return None


def _get_jwt_user():
    """Decode JWT and load the corresponding User from the database.
    Verifies server-side revocation and session integrity.

    Sets g.jwt_claims for downstream access.
    Returns (user, None) on success, (None, error_response) on failure.
    """
    token = _extract_jwt_from_request()
    if not token:
        return None, (jsonify({'error': 'Missing Authorization header. Use: Bearer <token>'}), 401)

    try:
        claims = decode_jwt(token)
    except jwt.ExpiredSignatureError:
        return None, (jsonify({'error': 'Token expired. Please re-authenticate.'}), 401)
    except jwt.InvalidTokenError as e:
        return None, (jsonify({'error': f'Invalid token: {str(e)}'}), 401)

    # Check if token JTI is in RevokedToken registry
    jti = claims.get('jti')
    if jti:
        revoked = RevokedToken.query.filter_by(jti=jti).first()
        if revoked:
            return None, (jsonify({'error': 'Token has been revoked. Please log in again.'}), 401)

    user = db.session.get(User, claims['sub'])
    if not user:
        return None, (jsonify({'error': 'User not found for this token.'}), 401)

    # Server-side session check: verify user session_token matches if present
    token_session = claims.get('session_token')
    if user.session_token and token_session and user.session_token != token_session:
        return None, (jsonify({'error': 'Session has been invalidated. Please log in again.'}), 401)

    # Verify org_id in token matches the user's current org_id (tamper protection)
    if user.org_id != claims.get('org_id'):
        return None, (jsonify({'error': 'Token org_id mismatch. Re-authenticate.'}), 403)

    g.jwt_claims = claims
    return user, None


# ────────────────────────────────────────────
# AUTH DECORATORS
# ────────────────────────────────────────────
def jwt_required(f):
    """Decorator: requires a valid JWT in the Authorization header.
    Sets g.current_api_user and g.jwt_claims for the decorated function.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        user, error = _get_jwt_user()
        if error:
            return error
        g.current_api_user = user
        return f(*args, **kwargs)
    return wrapped


def jwt_or_session_required(f):
    """Hybrid decorator: accepts EITHER a JWT token OR a Flask-Login session.
    Priority: JWT first (for API clients), then falls back to session (for browser).
    Sets g.current_api_user to the authenticated user regardless of method.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        token = _extract_jwt_from_request()
        if token:
            user, error = _get_jwt_user()
            if error:
                return error
            g.current_api_user = user
        elif current_user.is_authenticated:
            # Fall back to Flask-Login session
            # Server-side validation of session_token is enforced in app.py load_user
            g.current_api_user = current_user
            g.jwt_claims = {
                'sub': current_user.id,
                'org_id': current_user.org_id,
                'role': current_user.role,
                'username': current_user.username
            }
        else:
            return jsonify({'error': 'Authentication required. Provide JWT or session.'}), 401
        return f(*args, **kwargs)
    return wrapped


def org_admin_required(f):
    """Composite decorator: login required + role must be hr_admin or finance.
    Works with both JWT and session auth (uses jwt_or_session_required internally).
    """
    @wraps(f)
    @jwt_or_session_required
    def wrapped(*args, **kwargs):
        user = g.current_api_user
        if user.role not in ('hr_admin', 'finance'):
            return jsonify({'error': 'Access denied — admin/finance role required.'}), 403
        return f(*args, **kwargs)
    return wrapped


def role_api_required(*allowed_roles):
    """Decorator factory: restrict API endpoints to specific roles.
    Usage: @role_api_required('hr_admin', 'finance')
    Works with both JWT and session auth.
    """
    def decorator(f):
        @wraps(f)
        @jwt_or_session_required
        def wrapped(*args, **kwargs):
            user = g.current_api_user
            if user.role not in allowed_roles:
                return jsonify({
                    'error': f'Access denied — requires role: {", ".join(allowed_roles)}.'
                }), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ────────────────────────────────────────────
# REQUEST INTERCEPTOR / AUDIT LOGGER
# ────────────────────────────────────────────
def register_audit_interceptor(app):
    """Register a before_request hook that logs every authenticated API request.
    Masks PII and records audit events without breaking the main request.
    """
    @app.after_request
    def audit_request(response):
        """Log write operations (POST, PUT, DELETE) to the audit trail."""
        if request.method in ('POST', 'PUT', 'DELETE') and request.endpoint != 'static':
            user = None
            org_id = None

            if hasattr(g, 'current_api_user') and g.current_api_user:
                user = g.current_api_user
                org_id = user.org_id
            elif current_user and current_user.is_authenticated:
                user = current_user
                org_id = current_user.org_id

            if user:
                try:
                    # Sanitize details, avoiding PII leakage
                    details = f"Status: {response.status_code}"
                    log = AuditLog(
                        user_id=user.id,
                        org_id=org_id,
                        action=f"{request.method} {request.path}",
                        entity_type=request.endpoint or 'unknown',
                        details=details,
                        ip_address=request.headers.get('X-Forwarded-For', request.remote_addr)
                    )
                    db.session.add(log)
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        return response


# ────────────────────────────────────────────
# PDF PAYSLIP GENERATOR (ReportLab)
# ────────────────────────────────────────────
def generate_payslip_pdf(slip, emp, salary, org_name):
    """Generate a production-quality PDF payslip using ReportLab.
    Note: Database transactions must NOT be held open while this function executes.

    Args:
        slip: Payslip model instance or dict
        emp: Employee model instance or dict
        salary: SalaryStructure model instance or dict
        org_name: str — organization name

    Returns:
        bytes — the PDF content as a byte string
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                         Paragraph, Spacer)
    except ImportError:
        # ReportLab not available (e.g. Vercel) — return a plain-text fallback
        return (f"PayrollX Payslip — {emp.full_name}\n"
                f"Net Pay: {slip.net_pay}\n"
                f"ReportLab unavailable in this environment.").encode('utf-8')
    from io import BytesIO

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=15*mm, bottomMargin=15*mm)

    styles = getSampleStyleSheet()
    elements = []

    dark_navy = HexColor('#1b2a4a')
    accent_blue = HexColor('#528ff0')
    green = HexColor('#27ae60')
    red = HexColor('#e74c3c')
    light_gray = HexColor('#f8fafc')
    border_gray = HexColor('#dde3ec')

    months = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']
    month_name = months[slip.month] if 1 <= slip.month <= 12 else str(slip.month)

    header_data = [
        [Paragraph(f'<font color="white"><b>PayrollX — Payslip</b></font>', styles['Title']),
         Paragraph(f'<font color="white">Pay Period<br/><b>{month_name} {slip.year}</b></font>',
                   styles['Normal'])]
    ]
    header_table = Table(header_data, colWidths=[120*mm, 50*mm])
    header_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), dark_navy),
        ('TEXTCOLOR', (0, 0), (-1, -1), HexColor('#ffffff')),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (0, 0), 12),
        ('RIGHTPADDING', (1, 0), (1, 0), 12),
    ]))
    elements.append(header_table)

    org_data = [[Paragraph(f'<font size="9" color="#5a6b82"><b>{str(org_name).upper()}</b></font>',
                            styles['Normal'])]]
    org_table = Table(org_data, colWidths=[170*mm])
    org_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), light_gray),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (0, 0), 12),
        ('LINEBELOW', (0, 0), (-1, -1), 0.5, border_gray),
    ]))
    elements.append(org_table)
    elements.append(Spacer(1, 4*mm))

    emp_id_formatted = f"EMP-{emp.emp_id:04d}" if hasattr(emp, 'emp_id') else "EMP-0000"
    bank_acc = emp.bank_account if hasattr(emp, 'bank_account') else ''
    bank_display = f"{getattr(emp, 'bank_name', '') or ''} — ****{bank_acc[-4:] if bank_acc else '0000'}"
    ctc_val = getattr(salary, 'ctc_annual', 0) if salary else 0
    ctc_display = f"₹{ctc_val:,.0f}" if ctc_val else "—"

    # Mask PAN for PII safety in PDF display
    raw_pan = getattr(emp, 'pan_number', '') or ''
    masked_pan = f"*****{raw_pan[-5:]}" if len(raw_pan) == 10 else (raw_pan or "—")

    detail_data = [
        [Paragraph('<font size="8" color="#8e9ab5"><b>EMPLOYEE NAME</b></font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5"><b>EMPLOYEE ID</b></font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5"><b>DEPARTMENT</b></font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5"><b>DESIGNATION</b></font>', styles['Normal'])],
        [Paragraph(f'<font size="10"><b>{emp.full_name}</b></font>', styles['Normal']),
         Paragraph(f'<font size="10"><b>{emp_id_formatted}</b></font>', styles['Normal']),
         Paragraph(f'<font size="10"><b>{getattr(emp, "department", "—")}</b></font>', styles['Normal']),
         Paragraph(f'<font size="10"><b>{getattr(emp, "designation", "—")}</b></font>', styles['Normal'])],
        [Paragraph('<font size="8" color="#8e9ab5"><b>PAN NUMBER</b></font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5"><b>BANK ACCOUNT</b></font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5"><b>DATE OF JOINING</b></font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5"><b>ANNUAL CTC</b></font>', styles['Normal'])],
        [Paragraph(f'<font size="10"><b>{masked_pan}</b></font>', styles['Normal']),
         Paragraph(f'<font size="10"><b>{bank_display}</b></font>', styles['Normal']),
         Paragraph(f'<font size="10"><b>{getattr(emp, "date_of_joining", "—")}</b></font>', styles['Normal']),
         Paragraph(f'<font size="10"><b>{ctc_display}</b></font>', styles['Normal'])],
    ]
    detail_table = Table(detail_data, colWidths=[42.5*mm]*4)
    detail_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, -1), (-1, -1), 0.5, border_gray),
    ]))
    elements.append(detail_table)
    elements.append(Spacer(1, 4*mm))

    def fmt(val):
        return f"₹{val:,.2f}"

    earnings_rows = [
        ['Basic Pay', fmt(slip.basic_pay)],
        ['House Rent Allowance', fmt(slip.hra)],
        ['Dearness Allowance', fmt(slip.da)],
        ['Special Allowance', fmt(slip.special_allowance)],
        ['Conveyance', fmt(slip.conveyance)],
        ['Medical Allowance', fmt(slip.medical_allowance)],
        ['', ''],
        ['GROSS EARNINGS', fmt(slip.gross_pay)],
    ]

    deductions_rows = [
        ['Employee PF (EPF)', fmt(slip.epf_deduction)],
        ['ESI', fmt(slip.esi_deduction)],
        ['Professional Tax', fmt(slip.professional_tax)],
        ['TDS (Income Tax)', fmt(slip.tds)],
        ['LOP Deduction', fmt(slip.lop_deduction)],
        ['', ''],
        ['', ''],
        ['TOTAL DEDUCTIONS', fmt(slip.total_deductions)],
    ]

    combined_data = [
        [Paragraph('<font color="#528ff0"><b>EARNINGS</b></font>', styles['Normal']),
         '', '',
         Paragraph('<font color="#e74c3c"><b>DEDUCTIONS</b></font>', styles['Normal']),
         '']
    ]
    for i in range(len(earnings_rows)):
        row = [earnings_rows[i][0], earnings_rows[i][1], '',
               deductions_rows[i][0], deductions_rows[i][1]]
        combined_data.append(row)

    combined_table = Table(combined_data, colWidths=[45*mm, 30*mm, 10*mm, 45*mm, 30*mm])
    style_cmds = [
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (1, 0), 1.5, accent_blue),
        ('LINEBELOW', (3, 0), (4, 0), 1.5, red),
        ('FONT', (0, -1), (1, -1), 'Helvetica-Bold', 10),
        ('FONT', (3, -1), (4, -1), 'Helvetica-Bold', 10),
        ('TEXTCOLOR', (1, -1), (1, -1), green),
        ('TEXTCOLOR', (4, -1), (4, -1), red),
        ('LINEABOVE', (0, -1), (1, -1), 1.5, HexColor('#1a2332')),
        ('LINEABOVE', (3, -1), (4, -1), 1.5, HexColor('#1a2332')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
    ]
    for i in range(1, len(combined_data) - 1):
        style_cmds.append(('LINEBELOW', (0, i), (1, i), 0.3, HexColor('#f0f3f7')))
        style_cmds.append(('LINEBELOW', (3, i), (4, i), 0.3, HexColor('#f0f3f7')))

    combined_table.setStyle(TableStyle(style_cmds))
    elements.append(combined_table)
    elements.append(Spacer(1, 4*mm))

    net_data = [
        [Paragraph('<font color="white"><b>NET PAY (TAKE HOME)</b></font>', styles['Normal']),
         Paragraph(f'<font color="white" size="16"><b>{fmt(slip.net_pay)}</b></font>',
                   styles['Normal'])]
    ]
    net_table = Table(net_data, colWidths=[100*mm, 70*mm])
    net_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), green),
        ('TEXTCOLOR', (0, 0), (-1, -1), HexColor('#ffffff')),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (0, 0), 12),
        ('RIGHTPADDING', (1, 0), (1, 0), 12),
    ]))
    elements.append(net_table)
    elements.append(Spacer(1, 4*mm))

    footer_data = [
        [Paragraph('<font size="8" color="#8e9ab5">This is a system-generated payslip and '
                    'does not require a signature.</font>', styles['Normal']),
         Paragraph('<font size="8" color="#8e9ab5">Generated by PayrollX</font>',
                   styles['Normal'])]
    ]
    footer_table = Table(footer_data, colWidths=[120*mm, 50*mm])
    footer_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), light_gray),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (0, 0), 12),
        ('RIGHTPADDING', (1, 0), (1, 0), 12),
        ('LINEABOVE', (0, 0), (-1, 0), 0.5, border_gray),
    ]))
    elements.append(footer_table)

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
