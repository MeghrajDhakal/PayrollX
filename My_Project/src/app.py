import gzip
import io
import os
import secrets
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from functools import wraps

from cache_decorators import (
    build_dashboard_summary_cache_key,
    build_employee_cache_key,
    build_roster_cache_key,
    invalidate_tenant_employee_cache,
    invalidate_tenant_payroll_cache,
)
from config import BUNDLE_DIR, IS_VERCEL, Config
from constants import MONTH_NAMES, get_month_name
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from idempotency import (
    compute_request_hash,
    get_idempotency_key,
    get_idempotent_response,
    save_idempotent_response,
)
from logging_config import configure_logging
from middleware import (
    generate_jwt,
    generate_payslip_pdf,
    jwt_or_session_required,
    org_admin_required,
    register_audit_interceptor,
)
from models import (
    AttendanceLog,
    AuditLog,
    Employee,
    Organization,
    PayrollRun,
    Payslip,
    Reimbursement,
    RevokedToken,
    SalaryStructure,
    StatutoryRate,
    TaxDeclaration,
    User,
    db,
)
from pydantic import ValidationError
from redis_client import cache_get, cache_ping, cache_set
from schemas import (
    AttendanceUpdateSchema,
    EmployeeCreateSchema,
    LoginSchema,
    RegisterSchema,
    RunPayrollSchema,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload


# ── Static Asset Versioning (computed once at startup for immutable browser caching) ──
def _compute_asset_version() -> str:
    try:
        css_path = os.path.join(BUNDLE_DIR, 'static', 'css', 'style.css')
        js_path = os.path.join(BUNDLE_DIR, 'static', 'js', 'main.js')
        mtimes = [int(os.path.getmtime(p)) for p in (css_path, js_path) if os.path.exists(p)]
        return str(max(mtimes)) if mtimes else '1'
    except OSError:
        return '1'


ASSET_VERSION = _compute_asset_version()

# ── Flask Application Setup ──
app = Flask(__name__,
            template_folder=os.path.join(BUNDLE_DIR, 'templates'),
            static_folder=os.path.join(BUNDLE_DIR, 'static'))
app.config.from_object(Config)


@app.context_processor
def inject_asset_version():
    return {'asset_v': ASSET_VERSION}

# Configure structured JSON logging
configure_logging(app)

db.init_app(app)
migrate = Migrate(app, db)

# ── Flask-Limiter for Rate Limiting ──
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
    enabled=not app.config.get('TESTING', False)
)

# ── Flask-Login Configuration ──
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access payroll features.'
login_manager.login_message_category = 'info'

register_audit_interceptor(app)


# ────────────────────────────────────────────
# REQUEST CORRELATION & SESSION INTEGRITY HOOKS
# ────────────────────────────────────────────
@app.before_request
def handle_before_request():
    """Assign unique request_id and measure latency for observability."""
    g.request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())
    g.start_time = time.time()

    # Never hit database or mutate session cookies for static assets or health pings
    if request.endpoint == 'static' or request.path.startswith('/static/') or request.path == '/health':
        return None

    # Server-side idle session check for cookie sessions
    if current_user.is_authenticated:
        last_active = session.get('last_active')
        idle_timeout_min = app.config.get('SESSION_IDLE_TIMEOUT_MINUTES', 30)
        now_ts = time.time()
        if last_active and (now_ts - last_active > idle_timeout_min * 60):
            # Session expired due to inactivity
            session.clear()
            logout_user()
            if request.is_json:
                return jsonify({'error': 'Session expired due to inactivity. Please log in again.'}), 401
            flash('Your session has expired due to inactivity. Please log in again.', 'info')
            return redirect(url_for('login'))
        session['last_active'] = now_ts


@app.after_request
def handle_after_request(response):
    """Attach X-Request-ID and log request completion in structured JSON."""
    response.headers['X-Request-ID'] = getattr(g, 'request_id', '')

    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    if app.config.get('SESSION_COOKIE_SECURE'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'

    if request.endpoint == 'static' or request.path.startswith('/static/'):
        response.headers['Cache-Control'] = (
            'public, max-age=31536000, immutable' if 'v' in request.args else 'public, max-age=86400'
        )

    # Compress 200 text/static assets on local/WSGI hosts when client accepts gzip
    if (
        not app.config.get('TESTING')
        and not IS_VERCEL
        and response.status_code == 200
        and (not response.direct_passthrough or request.endpoint == 'static')
        and (response.content_length is None or response.content_length <= 2 * 1024 * 1024)
        and 'Content-Encoding' not in response.headers
        and response.mimetype in ('text/html', 'text/css', 'application/javascript', 'text/javascript', 'application/json', 'image/svg+xml')
        and 'gzip' in request.headers.get('Accept-Encoding', '').lower()
    ):
        response.direct_passthrough = False
        raw_body = response.get_data()
        if len(raw_body) > 1024:
            compressed = gzip.compress(raw_body, compresslevel=5)
            response.set_data(compressed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(len(compressed))
            etag = response.headers.get('ETag')
            if etag and not etag.startswith('W/'):
                response.headers['ETag'] = f'"{etag.strip(chr(34))}-gzip"'
            vary = response.headers.get('Vary')
            response.headers['Vary'] = f"{vary}, Accept-Encoding" if vary and 'Accept-Encoding' not in vary else (vary or 'Accept-Encoding')

    # Compute duration for logging
    if hasattr(g, 'start_time'):
        g.latency_ms = round((time.time() - g.start_time) * 1000, 2)
        g.status_code = response.status_code
        if request.endpoint != 'static':
            app.logger.info(f"{request.method} {request.path} -> {response.status_code} ({g.latency_ms}ms)")

    return response


@login_manager.user_loader
def load_user(user_id):
    """Load user and enforce server-side session token validation."""
    try:
        user = db.session.get(User, int(user_id), options=[joinedload(User.organization)])
        if not user:
            return None
        # Verify server-side session token to prevent replaying invalidated cookies
        expected_token = session.get('session_token')
        if not expected_token or not user.session_token or user.session_token != expected_token:
            return None
        return user
    except (ValueError, TypeError, SQLAlchemyError):
        return None


# ────────────────────────────────────────────
# ERROR HANDLERS (No Stack Trace Leaks)
# ────────────────────────────────────────────
@app.errorhandler(429)
def ratelimit_handler(e):
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({
            'error': 'Too many requests. Please slow down and try again later.',
            'request_id': getattr(g, 'request_id', None)
        }), 429
    flash('Too many attempts. Please wait a few moments and try again.', 'error')
    return render_template('login.html'), 429


@app.errorhandler(400)
def bad_request_handler(e):
    msg = getattr(e, 'description', 'Bad Request')
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'error': msg, 'request_id': getattr(g, 'request_id', None)}), 400
    flash(f"Bad Request: {msg}", 'error')
    return redirect(url_for('dashboard') if current_user.is_authenticated else url_for('home'))


@app.errorhandler(403)
def forbidden_handler(e):
    msg = getattr(e, 'description', 'Forbidden')
    if request.is_json or request.path.startswith('/api/') or '/pdf/' in request.path or '/download/' in request.path:
        return jsonify({'error': msg, 'request_id': getattr(g, 'request_id', None)}), 403
    flash('Access denied — unauthorized action.', 'error')
    return make_response(redirect(url_for('dashboard') if current_user.is_authenticated else url_for('home')), 403)


@app.errorhandler(404)
def not_found_handler(e):
    if request.is_json or request.path.startswith('/api/') or '/pdf/' in request.path or '/download/' in request.path:
        return jsonify({'error': 'Resource not found', 'request_id': getattr(g, 'request_id', None)}), 404
    flash('Resource not found.', 'error')
    return make_response(redirect(url_for('dashboard') if current_user.is_authenticated else url_for('home')), 404)


@app.errorhandler(500)
@app.errorhandler(Exception)
def unhandled_exception_handler(e):
    req_id = getattr(g, 'request_id', 'unknown')
    app.logger.exception("Unhandled exception on %s %s", request.method, request.path)
    db.session.rollback()

    if request.is_json or request.path.startswith('/api/'):
        return jsonify({
            'error': 'An internal server error occurred. Please contact support.',
            'request_id': req_id
        }), 500
    flash(f'An internal error occurred (Request ID: {req_id}). Please try again later.', 'error')
    return redirect(url_for('dashboard') if current_user.is_authenticated else url_for('home'))


# ────────────────────────────────────────────
# RBAC MIDDLEWARE DECORATORS (Session-based)
# ────────────────────────────────────────────
def role_required(*allowed_roles):
    """Decorator: restrict a route to specific roles."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.role not in allowed_roles:
                flash('Access denied — insufficient permissions.', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def get_org_name():
    """Helper – safely return the current user's organization name."""
    if current_user.is_authenticated and current_user.organization:
        return current_user.organization.name
    return 'Unknown Organization'


def get_employee_for_current_user():
    """Helper – returns the Employee record scoped strictly to the current user + org."""
    if not current_user.is_authenticated:
        return None
    return Employee.query.filter_by(
        user_id=current_user.id,
        org_id=current_user.org_id
    ).first()


def paginate_query(query, max_per_page=50):
    """Safe pagination helper bounding list limits."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(max(1, per_page), max_per_page)
    page = max(1, page)
    return query.paginate(page=page, per_page=per_page, error_out=False)


# ────────────────────────────────────────────
# PAYROLL CALCULATION ENGINE
# ────────────────────────────────────────────
def calculate_epf(basic_monthly):
    """EPF: 12% of Basic (employee share), capped at ₹15,000 basic"""
    eligible_basic = min(basic_monthly, 15000)
    return round(eligible_basic * 0.12, 2)


def calculate_esi(gross_monthly):
    """ESI: 0.75% employee share if gross ≤ ₹21,000"""
    if gross_monthly <= 21000:
        return round(gross_monthly * 0.0075, 2)
    return 0


def calculate_professional_tax(gross_monthly):
    """Professional Tax (typical Karnataka/Maharashtra slab)"""
    if gross_monthly <= 15000:
        return 0
    elif gross_monthly <= 20000:
        return 150
    else:
        return 200


def calculate_tds_monthly(annual_taxable, regime='new'):
    """Simplified TDS under New/Old Regime (FY 2025-26 slabs)"""
    if regime == 'new':
        slabs = [
            (300000, 0),
            (700000, 0.05),
            (1000000, 0.10),
            (1200000, 0.15),
            (1500000, 0.20),
            (float('inf'), 0.30)
        ]
    else:
        slabs = [
            (250000, 0),
            (500000, 0.05),
            (1000000, 0.20),
            (float('inf'), 0.30)
        ]

    tax = 0
    prev_limit = 0
    for limit, rate in slabs:
        if annual_taxable <= prev_limit:
            break
        taxable_in_slab = min(annual_taxable, limit) - prev_limit
        tax += taxable_in_slab * rate
        prev_limit = limit

    # Add 4% cess
    tax = tax * 1.04
    return round(tax / 12, 2)


def generate_payslip_for_employee(emp, run, month, year):
    """Generate a complete payslip for one employee scoped to org_id."""
    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, org_id=emp.org_id, is_current=True).first()
    if not salary:
        return None

    attendance_log = AttendanceLog.query.filter_by(emp_id=emp.emp_id, org_id=emp.org_id, month=month, year=year).first()
    working_days = attendance_log.total_working_days if attendance_log else 22
    days_present = attendance_log.days_present if attendance_log else working_days
    lop_days = attendance_log.lop_days if attendance_log else 0

    attendance_ratio = days_present / working_days if working_days > 0 else 1.0

    full_basic = round(salary.basic_pay / 12, 2)
    full_hra = round(salary.hra / 12, 2)
    full_da = round(salary.da / 12, 2)
    full_special = round(salary.special_allowance / 12, 2)
    full_conveyance = round(salary.conveyance / 12, 2)
    full_medical = round(salary.medical_allowance / 12, 2)

    basic = round(full_basic * attendance_ratio, 2)
    hra = round(full_hra * attendance_ratio, 2)
    da = round(full_da * attendance_ratio, 2)
    special = round(full_special * attendance_ratio, 2)
    conveyance = round(full_conveyance * attendance_ratio, 2)
    medical = round(full_medical * attendance_ratio, 2)

    gross = basic + hra + da + special + conveyance + medical

    full_gross = full_basic + full_hra + full_da + full_special + full_conveyance + full_medical
    per_day = full_gross / working_days if working_days > 0 else 0
    lop_deduction = round(per_day * lop_days, 2)

    gross_after_lop = max(0, gross - lop_deduction)

    epf = calculate_epf(basic)
    esi = calculate_esi(gross_after_lop)
    pt = calculate_professional_tax(gross_after_lop)

    tax_decl = TaxDeclaration.query.filter_by(emp_id=emp.emp_id, org_id=emp.org_id).first()
    regime = tax_decl.regime if tax_decl else 'new'
    annual_gross = salary.ctc_annual
    annual_exemptions = 0
    if tax_decl and regime == 'old':
        annual_exemptions = (tax_decl.section_80c + tax_decl.section_80d +
                             tax_decl.section_80e + tax_decl.section_80g +
                             tax_decl.hra_exemption + tax_decl.lta_claimed)
    annual_taxable = max(annual_gross - annual_exemptions - (salary.basic_pay * 0.12), 0)
    tds = calculate_tds_monthly(annual_taxable, regime)

    total_deductions = epf + esi + pt + tds + lop_deduction
    net_pay = round(max(0, gross - total_deductions), 2)

    payslip = Payslip(
        run_id=run.run_id,
        emp_id=emp.emp_id,
        org_id=emp.org_id,
        month=month,
        year=year,
        basic_pay=basic,
        hra=hra,
        da=da,
        special_allowance=special,
        conveyance=conveyance,
        medical_allowance=medical,
        gross_pay=gross,
        epf_deduction=epf,
        esi_deduction=esi,
        professional_tax=pt,
        tds=tds,
        lop_deduction=lop_deduction,
        total_deductions=total_deductions,
        net_pay=net_pay
    )
    db.session.add(payslip)
    return payslip


def _utc_now() -> datetime:
    """Return current UTC timestamp as a naive datetime for SQLAlchemy DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


# ────────────────────────────────────────────
# HEALTH CHECK ENDPOINT
# ────────────────────────────────────────────
@app.route('/health')
def health_check():
    """Observability endpoint verifying app, database, and Redis responsiveness."""
    try:
        db.session.execute(text('SELECT 1'))
        redis_status = 'connected' if cache_ping() else 'disconnected_or_disabled'
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'redis': redis_status,
            'timestamp': _utc_now().isoformat() + 'Z',
            'request_id': getattr(g, 'request_id', None)
        }), 200
    except (SQLAlchemyError, RuntimeError, OSError) as e:
        app.logger.error("Health check failed database connectivity: %s", e)
        return jsonify({
            'status': 'unhealthy',
            'database': 'disconnected',
            'error': 'Database connectivity failed',
            'request_id': getattr(g, 'request_id', None)
        }), 503


@app.route('/api/v1/test/error-trigger')
def test_error_trigger():
    """Controlled error trigger for observability and leak verification in test environments."""
    if app.config.get('TESTING'):
        raise RuntimeError("Simulated controlled crash: secret_api_key_xyz9876543210")
    abort(404)


# ────────────────────────────────────────────
# JWT AUTH API ENDPOINTS
# ────────────────────────────────────────────
@app.route('/api/v1/auth/token', methods=['POST'])
@limiter.limit("5 per minute; 20 per hour")
def api_auth_token():
    """Issue a signed JWT access token with rate limiting and Pydantic validation."""
    payload = request.get_json(silent=True) or {}
    try:
        validated = LoginSchema.model_validate(payload)
    except ValidationError as ve:
        return jsonify({'error': 'Invalid request payload', 'details': ve.errors()}), 400

    username = validated.username or validated.employee_id
    if not username:
        return jsonify({'error': 'username or employee_id is required.'}), 400

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(validated.password):
        app.logger.warning(f"Failed API login attempt for username: {username}")
        return jsonify({'error': 'Invalid credentials.'}), 401

    # Rotate / assign user session_token for server-side invalidation tracking
    user.session_token = secrets.token_hex(32)
    db.session.commit()

    token = generate_jwt(user)
    expiry_hours = app.config.get('JWT_EXPIRY_HOURS', 8)

    return jsonify({
        'access_token': token,
        'token_type': 'Bearer',
        'user_id': user.id,
        'username': user.username,
        'role': user.role,
        'org_id': user.org_id,
        'org_name': user.organization.name if user.organization else None,
        'expires_in': expiry_hours * 3600
    })


@app.route('/api/v1/auth/revoke', methods=['POST'])
@jwt_or_session_required
def api_auth_revoke():
    """Revoke the current session and JWT token server-side immediately."""
    user = g.current_api_user
    user.session_token = None

    if hasattr(g, 'jwt_claims') and g.jwt_claims:
        jti = g.jwt_claims.get('jti')
        exp = g.jwt_claims.get('exp')
        if jti:
            expires_at = datetime.fromtimestamp(exp, UTC).replace(tzinfo=None) if exp else _utc_now() + timedelta(hours=8)
            revoked = RevokedToken(jti=jti, expires_at=expires_at)
            db.session.add(revoked)

    db.session.commit()
    session.clear()
    logout_user()
    return jsonify({'status': 'success', 'message': 'Session and token revoked successfully.'})


# ────────────────────────────────────────────
# API ROUTES — Multi-Tenant & Data Isolation
# ────────────────────────────────────────────
@app.route('/api/v1/employee/data', methods=['GET'])
@jwt_or_session_required
def get_employee_data():
    """getEmployeeData controller with strict Row-Level Security and Cache-Aside."""
    user = g.current_api_user
    claims = g.jwt_claims
    org_id = claims['org_id']

    if user.role == 'employee':
        user_cache_key = f"payrollx:org:{org_id}:user_emp:{claims['sub']}"
        cached_data = cache_get(user_cache_key)
        if cached_data:
            return jsonify(cached_data)

        emp = Employee.query.filter_by(
            user_id=claims['sub'],
            org_id=org_id
        ).first()
        if not emp:
            return jsonify({"error": "Employee profile not found."}), 404

        data = emp.to_dict(include_details=True)
        cache_set(user_cache_key, data, ttl=600)
        cache_set(build_employee_cache_key(org_id, emp.emp_id), data, ttl=600)
        return jsonify(data)
    else:
        target_emp_id = request.args.get('emp_id', type=int)
        if target_emp_id:
            emp_cache_key = build_employee_cache_key(org_id, target_emp_id)
            cached_data = cache_get(emp_cache_key)
            if cached_data:
                return jsonify(cached_data)

            emp = Employee.query.filter_by(
                emp_id=target_emp_id,
                org_id=org_id
            ).first()
            if not emp:
                return jsonify({"error": "Employee not found in your organization."}), 404

            data = emp.to_dict(include_details=True)
            cache_set(emp_cache_key, data, ttl=600)
            return jsonify(data)
        else:
            return jsonify({"error": "emp_id query parameter required for admin lookup."}), 400


@app.route('/api/v1/receipt/generate', methods=['GET'])
@jwt_or_session_required
def api_generate_receipt():
    """generateReceipt controller — non-blocking PDF generation with tenant scoping."""
    user = g.current_api_user
    claims = g.jwt_claims

    slip_id = request.args.get('slip_id', type=int)
    output_format = request.args.get('format', 'pdf')

    if not slip_id:
        return jsonify({"error": "slip_id query parameter is required."}), 400

    # Scope payslip lookup to user's org
    slip = Payslip.query.filter_by(slip_id=slip_id, org_id=claims['org_id']).first()
    if not slip:
        return jsonify({"error": "Payslip not found in your organization."}), 404

    emp = Employee.query.filter_by(emp_id=slip.emp_id, org_id=claims['org_id']).first()
    if not emp:
        return jsonify({"error": "Employee not found in your organization."}), 404

    # IDOR Prevention
    if user.role == 'employee' and emp.user_id != claims['sub']:
        return jsonify({"error": "IDOR Prevention: Cannot access another employee's receipt."}), 403

    if output_format == 'json':
        return jsonify({
            "status": "success",
            "message": "Receipt generation authorised.",
            "receipt_details": {
                "slip_id": slip.slip_id,
                "employee": emp.full_name,
                "month": slip.month,
                "year": slip.year,
                "gross_pay": slip.gross_pay,
                "total_deductions": slip.total_deductions,
                "net_pay": slip.net_pay,
                "download_url": url_for('download_payslip_pdf', slip_id=slip.slip_id, _external=True)
            }
        })

    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, org_id=claims['org_id'], is_current=True).first()
    org_name = emp.organization.name if emp.organization else 'Unknown Organization'

    # Ensure no open DB transactions during ReportLab rendering
    db.session.commit()
    pdf_bytes = generate_payslip_pdf(slip, emp, salary, org_name)

    month_name = get_month_name(slip.month)

    safe_emp_name = "".join(c for c in emp.full_name if c.isalnum() or c in (' ', '_')).replace(" ", "_")
    filename = f"payslip_{safe_emp_name}_{month_name}_{slip.year}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/v1/employees', methods=['GET'])
@org_admin_required
def api_list_employees():
    """List all employees in the current user's organization with pagination and Cache-Aside."""
    user = g.current_api_user
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 50)

    roster_cache_key = build_roster_cache_key(user.org_id, page=page, limit=per_page)
    cached_roster = cache_get(roster_cache_key)
    if cached_roster:
        return jsonify(cached_roster)

    query = Employee.query.filter_by(org_id=user.org_id, status='active').order_by(Employee.emp_id.asc())
    pagination = paginate_query(query, max_per_page=50)

    data = {
        "org_id": user.org_id,
        "total": pagination.total,
        "page": pagination.page,
        "pages": pagination.pages,
        "employees": [e.to_dict() for e in pagination.items]
    }
    cache_set(roster_cache_key, data, ttl=300)
    return jsonify(data)


@app.route('/api/v1/payroll/run', methods=['POST'])
@org_admin_required
def api_run_payroll():
    """API endpoint to run payroll with strict validation and idempotency."""
    user = g.current_api_user
    payload = request.get_json(silent=True) or {}
    try:
        validated = RunPayrollSchema.model_validate(payload)
    except ValidationError as ve:
        return jsonify({'error': 'Invalid input', 'details': ve.errors()}), 422

    idemp_key = get_idempotency_key()
    if idemp_key:
        cached = get_idempotent_response(idemp_key, user.org_id)
        if cached:
            return jsonify({'message': 'Idempotent replay', 'data': cached[1]}), cached[0]

    month = validated.month
    year = validated.year

    try:
        # Atomic transaction
        existing = PayrollRun.query.filter_by(month=month, year=year, org_id=user.org_id).first()
        if existing and existing.status == 'finalized':
            return jsonify({'error': f'Payroll for {month}/{year} is already finalized.'}), 400

        run = existing or PayrollRun(
            month=month,
            year=year,
            created_by=user.id,
            org_id=user.org_id
        )
        if not existing:
            db.session.add(run)
            db.session.flush()

        Payslip.query.filter_by(run_id=run.run_id, org_id=user.org_id).delete()

        employees = Employee.query.filter_by(status='active', org_id=user.org_id).all()
        total_gross = 0
        total_ded = 0
        total_net = 0
        count = 0

        for emp in employees:
            slip = generate_payslip_for_employee(emp, run, month, year)
            if slip:
                total_gross += slip.gross_pay
                total_ded += slip.total_deductions
                total_net += slip.net_pay
                count += 1

        run.total_gross = round(total_gross, 2)
        run.total_deductions = round(total_ded, 2)
        run.total_net = round(total_net, 2)
        run.employee_count = count
        run.status = 'pending'
        db.session.commit()

        # Evict tenant payroll and dashboard caches
        invalidate_tenant_payroll_cache(user.org_id)

        resp_data = {
            'run_id': run.run_id,
            'month': month,
            'year': year,
            'employee_count': count,
            'total_net': run.total_net,
            'status': run.status
        }

        if idemp_key:
            save_idempotent_response(
                key=idemp_key,
                org_id=user.org_id,
                endpoint='/api/v1/payroll/run',
                request_hash=compute_request_hash(payload),
                response_code=200,
                response_body=str(resp_data)
            )

        return jsonify({'message': 'Payroll run executed successfully', 'data': resp_data}), 200

    except Exception:
        db.session.rollback()
        app.logger.exception("Error in api_run_payroll")
        return jsonify({'error': 'Failed to execute payroll run'}), 500


# ────────────────────────────────────────────
# PUBLIC ROUTES (No Login Required)
# ────────────────────────────────────────────
@app.route('/api/index')
@app.route('/api/index.py')
@app.route('/app')
@app.route('/home')
@app.route('/')
def home():
    return render_template('home.html')


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute; 20 per hour")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        form_data = {k: v for k, v in request.form.items()}
        try:
            validated = LoginSchema.model_validate(form_data)
        except ValidationError as ve:
            flash(f"Validation error: {ve.errors()[0]['msg']}", 'error')
            return render_template('login.html'), 400

        login_mode = validated.login_mode
        password = validated.password

        if login_mode == 'employee':
            employee_id = (validated.employee_id or '').strip()
            if not employee_id:
                flash('Please enter your Employee ID.', 'error')
                return render_template('login.html'), 400
            user = User.query.filter_by(username=employee_id, role='employee').first()
            if user and user.check_password(password):
                user.session_token = secrets.token_hex(32)
                db.session.commit()
                session['session_token'] = user.session_token
                session['last_active'] = time.time()
                session.permanent = True
                login_user(user, remember=False)
                flash('Welcome back!', 'success')
                next_page = request.args.get('next')
                # Prevent open redirects
                if next_page and (next_page.startswith('/') and not next_page.startswith('//')):
                    return redirect(next_page)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid Employee ID or password.', 'error')
        else:
            username = (validated.username or '').strip()
            if not username:
                flash('Please enter your username.', 'error')
                return render_template('login.html'), 400
            user = User.query.filter_by(username=username).first()
            if user and user.role == 'employee':
                flash('Employees must use the Employee Login tab.', 'error')
                return render_template('login.html'), 403
            if user and user.check_password(password):
                user.session_token = secrets.token_hex(32)
                db.session.commit()
                session['session_token'] = user.session_token
                session['last_active'] = time.time()
                session.permanent = True
                login_user(user, remember=False)
                flash('Welcome back!', 'success')
                next_page = request.args.get('next')
                if next_page and (next_page.startswith('/') and not next_page.startswith('//')):
                    return redirect(next_page)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid username or password.', 'error')

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
@app.route('/api/v1/auth/register', methods=['POST'])
@limiter.limit("30 per minute; 100 per hour")
def register():
    is_api_request = (
        request.path.startswith('/api/')
        or request.is_json
        or request.headers.get('Accept') == 'application/json'
    )

    if current_user.is_authenticated and not is_api_request:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        # Accept JSON payload or standard multipart/x-www-form-urlencoded
        if request.is_json:
            raw_payload = request.get_json(silent=True) or {}
        else:
            raw_payload = {k: v for k, v in request.form.items()}

        try:
            validated = RegisterSchema.model_validate(raw_payload)
        except ValidationError as ve:
            error_msg = f"Validation error: {ve.errors()[0]['msg']}"
            if is_api_request:
                return jsonify({"error": error_msg, "details": ve.errors()}), 400
            flash(error_msg, 'error')
            return render_template('register.html'), 400

        username = validated.username
        email = validated.email
        password = validated.password
        org_name = validated.organization

        if User.query.filter_by(username=username).first():
            msg = 'Username already exists.'
            if is_api_request:
                return jsonify({"error": msg}), 400
            flash(msg, 'error')
            return render_template('register.html'), 400

        if User.query.filter_by(email=email).first():
            msg = 'Email already registered.'
            if is_api_request:
                return jsonify({"error": msg}), 400
            flash(msg, 'error')
            return render_template('register.html'), 400

        try:
            org = Organization.query.filter_by(name=org_name).first()
            if not org:
                org = Organization(name=org_name)
                db.session.add(org)
                db.session.flush()

            user = User(username=username, email=email, role='hr_admin', org_id=org.org_id)
            user.set_password(password)
            user.session_token = secrets.token_hex(32)
            db.session.add(user)
            db.session.commit()

            if is_api_request:
                return jsonify({
                    "status": "success",
                    "message": "Registration successful! Please log in.",
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "role": user.role,
                        "org_id": org.org_id,
                        "organization": org.name
                    }
                }), 201

            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            app.logger.exception("Registration failure")
            msg = 'Registration failed. Please try again.'
            if is_api_request:
                return jsonify({"error": msg, "details": str(e)}), 500
            flash(msg, 'error')
            return render_template('register.html'), 500

    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    """Invalidate session server-side so copied cookies are rejected immediately."""
    if current_user.is_authenticated:
        current_user.session_token = None
        try:
            db.session.commit()
        except SQLAlchemyError as exc:
            db.session.rollback()
            app.logger.warning("Failed to clear session token on logout: %s", exc)

    session.clear()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))


# ────────────────────────────────────────────
# PROTECTED ROUTES (Login Required)
# ────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        recent_slips = Payslip.query.filter_by(emp_id=emp.emp_id, org_id=current_user.org_id).order_by(
            Payslip.year.desc(), Payslip.month.desc()
        ).limit(5).all() if emp else []
        return render_template('dashboard.html', employee=emp, recent_slips=recent_slips, role='employee')

    org_id = current_user.org_id
    employees = Employee.query.filter_by(status='active', org_id=org_id).all()
    recent_runs = PayrollRun.query.filter_by(org_id=org_id).order_by(PayrollRun.created_at.desc()).limit(5).all()

    summary_key = build_dashboard_summary_cache_key(org_id)
    cached_summary = cache_get(summary_key)

    if cached_summary:
        total_employees = cached_summary.get('total_employees', 0)
        pending_runs = cached_summary.get('pending_runs', 0)
        finalized_runs = cached_summary.get('finalized_runs', 0)
        total_payroll = cached_summary.get('total_payroll', 0)
    else:
        total_employees = len(employees)
        pending_runs = PayrollRun.query.filter_by(status='pending', org_id=org_id).count()
        finalized_runs = PayrollRun.query.filter_by(status='finalized', org_id=org_id).count()
        latest_finalized = next((r for r in recent_runs if r.status == 'finalized'), None)
        if not latest_finalized and finalized_runs:
            latest_finalized = PayrollRun.query.filter_by(status='finalized', org_id=org_id).order_by(PayrollRun.created_at.desc()).first()
        total_payroll = latest_finalized.total_net if latest_finalized else 0
        cache_set(summary_key, {
            'total_employees': total_employees,
            'pending_runs': pending_runs,
            'finalized_runs': finalized_runs,
            'total_payroll': total_payroll
        }, ttl=180)

    return render_template('dashboard.html',
                           total_employees=total_employees,
                           pending_runs=pending_runs,
                           finalized_runs=finalized_runs,
                           total_payroll=total_payroll,
                           recent_runs=recent_runs,
                           employees=employees, role='admin')


@app.route('/payslips')
@login_required
def payslips():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        slips = Payslip.query.filter_by(emp_id=emp.emp_id, org_id=current_user.org_id).order_by(
            Payslip.year.desc(), Payslip.month.desc()
        ).all() if emp else []
        return render_template('payslips.html', payslips=slips, employees=[emp] if emp else [])

    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    emp_ids = [e.emp_id for e in employees]
    slips = Payslip.query.options(joinedload(Payslip.employee)).filter(
        Payslip.org_id == current_user.org_id,
        Payslip.emp_id.in_(emp_ids)
    ).order_by(Payslip.year.desc(), Payslip.month.desc()).all() if emp_ids else []

    return render_template('payslips.html', payslips=slips, employees=employees)


@app.route('/payslip/download/<int:slip_id>')
@login_required
def download_payslip(slip_id):
    """Generate HTML payslip with strict Row-Level Security checks."""
    slip = Payslip.query.filter_by(slip_id=slip_id, org_id=current_user.org_id).first()
    if not slip:
        abort(404)
    emp = Employee.query.filter_by(emp_id=slip.emp_id, org_id=current_user.org_id).first()
    if not emp:
        abort(404)

    if current_user.role == 'employee' and emp.user_id != current_user.id:
        abort(403)

    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, org_id=current_user.org_id, is_current=True).first()
    month_name = get_month_name(slip.month)
    return render_template('payslip_download.html',
                           slip=slip, emp=emp, salary=salary, month_name=month_name,
                           org=get_org_name())


@app.route('/payslip/pdf/<int:slip_id>')
@login_required
def download_payslip_pdf(slip_id):
    """Generate PDF payslip with ReportLab, without holding active database transactions."""
    slip = Payslip.query.filter_by(slip_id=slip_id, org_id=current_user.org_id).first()
    if not slip:
        abort(404)
    emp = Employee.query.filter_by(emp_id=slip.emp_id, org_id=current_user.org_id).first()
    if not emp:
        abort(404)

    if current_user.role == 'employee' and emp.user_id != current_user.id:
        abort(403)

    salary = SalaryStructure.query.filter_by(emp_id=emp.emp_id, org_id=current_user.org_id, is_current=True).first()
    org_name = get_org_name()

    # Commit/close transaction before ReportLab rendering
    db.session.commit()
    pdf_bytes = generate_payslip_pdf(slip, emp, salary, org_name)

    month_name = get_month_name(slip.month)

    safe_emp_name = "".join(c for c in emp.full_name if c.isalnum() or c in (' ', '_')).replace(" ", "_")
    filename = f"payslip_{safe_emp_name}_{month_name}_{slip.year}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename
    )


@app.route('/attendance', methods=['GET', 'POST'])
@login_required
def attendance():
    if request.method == 'POST' and current_user.role != 'employee':
        form_data = {k: v for k, v in request.form.items()}
        try:
            validated = AttendanceUpdateSchema.model_validate(form_data)
        except ValidationError as ve:
            flash(f"Invalid attendance data: {ve.errors()[0]['msg']}", 'error')
            return redirect(url_for('attendance'))

        emp_id = validated.emp_id
        att_month = validated.month
        att_year = validated.year

        emp = Employee.query.filter_by(emp_id=emp_id, org_id=current_user.org_id).first()
        if not emp:
            flash('Employee not found in your organization.', 'error')
            return redirect(url_for('attendance'))

        try:
            existing = AttendanceLog.query.filter_by(
                emp_id=emp_id, org_id=current_user.org_id, month=att_month, year=att_year
            ).first()

            if existing:
                existing.total_working_days = validated.total_working_days
                existing.days_present = validated.days_present
                existing.days_absent = validated.days_absent
                existing.lop_days = validated.lop_days
                existing.overtime_hours = validated.overtime_hours
                flash(f'Attendance updated for {emp.full_name} — {att_month}/{att_year}', 'success')
            else:
                new_log = AttendanceLog(
                    emp_id=emp_id,
                    org_id=current_user.org_id,
                    month=att_month,
                    year=att_year,
                    total_working_days=validated.total_working_days,
                    days_present=validated.days_present,
                    days_absent=validated.days_absent,
                    lop_days=validated.lop_days,
                    overtime_hours=validated.overtime_hours
                )
                db.session.add(new_log)
                flash(f'Attendance added for {emp.full_name} — {att_month}/{att_year}', 'success')

            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Attendance save error")
            flash('Error saving attendance record.', 'error')

        return redirect(url_for('attendance', month=att_month, year=att_year))

    month = request.args.get('month', type=int, default=datetime.now().month)
    year = request.args.get('year', type=int, default=datetime.now().year)

    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        logs = AttendanceLog.query.options(joinedload(AttendanceLog.employee)).filter_by(
            emp_id=emp.emp_id, org_id=current_user.org_id, month=month, year=year
        ).all() if emp else []
        employees = [emp] if emp else []
    else:
        employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
        emp_ids = [e.emp_id for e in employees]
        logs = AttendanceLog.query.options(joinedload(AttendanceLog.employee)).filter(
            AttendanceLog.org_id == current_user.org_id,
            AttendanceLog.emp_id.in_(emp_ids),
            AttendanceLog.month == month,
            AttendanceLog.year == year
        ).all() if emp_ids else []

    return render_template('attendance.html',
                           logs=logs, employees=employees,
                           selected_month=month, selected_year=year,
                           months=MONTH_NAMES)


@app.route('/tax-deductions')
@login_required
def tax_deductions():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        declarations = TaxDeclaration.query.options(joinedload(TaxDeclaration.employee)).filter_by(
            emp_id=emp.emp_id, org_id=current_user.org_id
        ).all() if emp else []
    else:
        declarations = TaxDeclaration.query.options(joinedload(TaxDeclaration.employee)).filter_by(
            org_id=current_user.org_id
        ).all()

    return render_template('tax_deductions.html', declarations=declarations)


@app.route('/reimbursements')
@login_required
def reimbursements():
    if current_user.role == 'employee':
        emp = get_employee_for_current_user()
        claims = Reimbursement.query.filter_by(emp_id=emp.emp_id, org_id=current_user.org_id).order_by(
            Reimbursement.submitted_at.desc()
        ).all() if emp else []
    else:
        claims = Reimbursement.query.filter_by(org_id=current_user.org_id).order_by(
            Reimbursement.submitted_at.desc()
        ).all()

    return render_template('reimbursements.html', claims=claims)


@app.route('/documents')
@login_required
def documents():
    return render_template('documents.html')


@app.route('/people')
@login_required
@role_required('hr_admin', 'finance')
def people():
    query = Employee.query.options(selectinload(Employee.salary_structures)).filter_by(
        org_id=current_user.org_id
    ).order_by(Employee.emp_id.asc())
    pagination = paginate_query(query, max_per_page=50)
    return render_template('people.html', employees=pagination.items, pagination=pagination)


@app.route('/people/add', methods=['POST'])
@login_required
@role_required('hr_admin')
def add_employee():
    """Add employee with schema validation, atomic transaction, and idempotency."""
    form_data = {k: v for k, v in request.form.items()}
    try:
        validated = EmployeeCreateSchema.model_validate(form_data)
    except ValidationError as ve:
        flash(f"Validation error: {ve.errors()[0]['msg']}", 'error')
        return redirect(url_for('people')), 400

    idemp_key = get_idempotency_key()
    if idemp_key:
        cached = get_idempotent_response(idemp_key, current_user.org_id)
        if cached:
            flash("Employee record already created (idempotent request).", 'info')
            return redirect(url_for('people'))

    # Atomic multi-step transaction
    try:
        emp = Employee(
            first_name=validated.first_name,
            last_name=validated.last_name,
            email=validated.email,
            phone=validated.phone,
            pan_number=validated.pan_number,
            aadhaar_number=validated.aadhaar_number,
            bank_account=validated.bank_account,
            bank_ifsc=validated.bank_ifsc,
            bank_name=validated.bank_name,
            department=validated.department,
            designation=validated.designation,
            org_id=current_user.org_id
        )
        db.session.add(emp)
        db.session.flush()

        # Employee user account with EMP<id>
        emp_user = User(
            username=f"EMP{emp.emp_id}",
            email=emp.email,
            role='employee',
            org_id=current_user.org_id
        )
        emp_user.set_password(f"{emp.first_name.lower()}123")
        db.session.add(emp_user)
        db.session.flush()
        emp.user_id = emp_user.id

        ctc = validated.ctc
        basic = round(ctc * 0.40, 2)
        hra = round(basic * 0.50, 2)
        da = round(ctc * 0.05, 2)
        conveyance = round(ctc * 0.05, 2)
        medical = round(ctc * 0.03, 2)
        special = round(max(0, ctc - basic - hra - da - conveyance - medical), 2)

        salary = SalaryStructure(
            emp_id=emp.emp_id,
            org_id=current_user.org_id,
            ctc_annual=ctc,
            basic_pay=basic,
            hra=hra,
            da=da,
            special_allowance=special,
            conveyance=conveyance,
            medical_allowance=medical
        )
        db.session.add(salary)
        db.session.commit()

        # Evict cache & bump roster version
        invalidate_tenant_employee_cache(current_user.org_id, emp.emp_id)

        if idemp_key:
            save_idempotent_response(
                key=idemp_key,
                org_id=current_user.org_id,
                endpoint='/people/add',
                request_hash=compute_request_hash(form_data),
                response_code=302,
                response_body=f"Created EMP{emp.emp_id}"
            )

        flash(f'Employee {emp.full_name} added successfully! Login: EMP{emp.emp_id} / {emp.first_name.lower()}123', 'success')
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to add employee")
        flash('An error occurred while adding the employee. Please check inputs and try again.', 'error')

    return redirect(url_for('people'))


@app.route('/run-payroll', methods=['GET', 'POST'])
@login_required
@role_required('hr_admin', 'finance')
def run_payroll():
    """Execute monthly payroll run atomically with idempotency protection."""
    if request.method == 'POST':
        form_data = {k: v for k, v in request.form.items()}
        try:
            validated = RunPayrollSchema.model_validate(form_data)
        except ValidationError as ve:
            flash(f"Validation error: {ve.errors()[0]['msg']}", 'error')
            return redirect(url_for('run_payroll')), 400

        idemp_key = get_idempotency_key()
        if idemp_key:
            cached = get_idempotent_response(idemp_key, current_user.org_id)
            if cached:
                flash("Payroll run already executed (idempotent request).", 'info')
                return redirect(url_for('run_payroll'))

        month = validated.month
        year = validated.year

        try:
            existing = PayrollRun.query.filter_by(month=month, year=year, org_id=current_user.org_id).first()
            if existing and existing.status == 'finalized':
                flash(f'Payroll for {month}/{year} is already finalized.', 'error')
                return redirect(url_for('run_payroll'))

            run = existing or PayrollRun(
                month=month,
                year=year,
                created_by=current_user.id,
                org_id=current_user.org_id
            )
            if not existing:
                db.session.add(run)
                db.session.flush()

            # Clear old payslips for this run within org
            Payslip.query.filter_by(run_id=run.run_id, org_id=current_user.org_id).delete()

            employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
            total_gross = 0
            total_ded = 0
            total_net = 0
            count = 0

            for emp in employees:
                slip = generate_payslip_for_employee(emp, run, month, year)
                if slip:
                    total_gross += slip.gross_pay
                    total_ded += slip.total_deductions
                    total_net += slip.net_pay
                    count += 1

            run.total_gross = round(total_gross, 2)
            run.total_deductions = round(total_ded, 2)
            run.total_net = round(total_net, 2)
            run.employee_count = count
            run.status = 'pending'
            db.session.commit()

            # Evict tenant payroll and dashboard caches
            invalidate_tenant_payroll_cache(current_user.org_id)

            if idemp_key:
                save_idempotent_response(
                    key=idemp_key,
                    org_id=current_user.org_id,
                    endpoint='/run-payroll',
                    request_hash=compute_request_hash(form_data),
                    response_code=302,
                    response_body=f"Payroll calculated for {count} employees."
                )

            flash(f'Payroll calculated for {count} employees. Total Net: ₹{run.total_net:,.2f}', 'success')
            return redirect(url_for('run_payroll'))

        except Exception:
            db.session.rollback()
            app.logger.exception("Error executing payroll run")
            flash('Failed to run payroll due to a database conflict or error.', 'error')
            return redirect(url_for('run_payroll'))

    runs = PayrollRun.query.filter_by(org_id=current_user.org_id).order_by(PayrollRun.year.desc(), PayrollRun.month.desc()).all()
    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    return render_template('run_payroll.html', runs=runs, employees=employees)


@app.route('/approve-payroll/<int:run_id>', methods=['POST'])
@login_required
@role_required('hr_admin', 'finance')
def approve_payroll(run_id):
    run = PayrollRun.query.filter_by(run_id=run_id, org_id=current_user.org_id).first_or_404()
    run.status = 'finalized'
    run.approver_id = current_user.id
    run.finalized_at = _utc_now()
    db.session.commit()

    # Evict tenant payroll and dashboard caches
    invalidate_tenant_payroll_cache(current_user.org_id)
    flash(f'Payroll for {run.month}/{run.year} finalized!', 'success')
    return redirect(url_for('run_payroll'))


@app.route('/reports')
@login_required
@role_required('hr_admin', 'finance')
def reports():
    runs = PayrollRun.query.filter_by(status='finalized', org_id=current_user.org_id).order_by(
        PayrollRun.year.desc(), PayrollRun.month.desc()
    ).all()
    return render_template('reports.html', runs=runs)


@app.route('/reports/payroll-summary')
@login_required
@role_required('hr_admin', 'finance')
def report_payroll_summary():
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    run = PayrollRun.query.filter_by(month=month, year=year, status='finalized', org_id=current_user.org_id).first()
    slips = Payslip.query.options(joinedload(Payslip.employee)).filter_by(
        month=month, year=year, run_id=run.run_id, org_id=current_user.org_id
    ).all() if run else []
    runs = PayrollRun.query.filter_by(status='finalized', org_id=current_user.org_id).order_by(
        PayrollRun.year.desc(), PayrollRun.month.desc()
    ).all()
    return render_template('report_payroll_summary.html',
                           run=run, slips=slips, runs=runs,
                           selected_month=month, selected_year=year,
                           months=MONTH_NAMES)


@app.route('/reports/statutory-compliance')
@login_required
@role_required('hr_admin', 'finance')
def report_statutory():
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    slips = Payslip.query.options(joinedload(Payslip.employee)).filter_by(
        org_id=current_user.org_id, month=month, year=year
    ).all()
    return render_template('report_statutory.html',
                           slips=slips, selected_month=month, selected_year=year,
                           months=MONTH_NAMES)


@app.route('/reports/department-cost')
@login_required
@role_required('hr_admin', 'finance')
def report_dept_cost():
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    slips = Payslip.query.options(joinedload(Payslip.employee)).filter_by(
        org_id=current_user.org_id, month=month, year=year
    ).all()

    dept_data = {}
    for slip in slips:
        dept = slip.employee.department if slip.employee else 'General'
        if dept not in dept_data:
            dept_data[dept] = {'count': 0, 'gross': 0, 'deductions': 0, 'net': 0}
        dept_data[dept]['count'] += 1
        dept_data[dept]['gross'] += slip.gross_pay
        dept_data[dept]['deductions'] += slip.total_deductions
        dept_data[dept]['net'] += slip.net_pay

    return render_template('report_dept_cost.html',
                           dept_data=dept_data, slips=slips,
                           selected_month=month, selected_year=year,
                           months=MONTH_NAMES)


@app.route('/reports/ytd-summary')
@login_required
@role_required('hr_admin', 'finance')
def report_ytd():
    year = request.args.get('year', type=int, default=2026)
    employees = Employee.query.filter_by(status='active', org_id=current_user.org_id).all()
    emp_ids = [emp.emp_id for emp in employees]
    slips_by_emp = {}
    if emp_ids:
        all_slips = Payslip.query.filter(
            Payslip.org_id == current_user.org_id,
            Payslip.year == year,
            Payslip.emp_id.in_(emp_ids)
        ).all()
        for slip in all_slips:
            slips_by_emp.setdefault(slip.emp_id, []).append(slip)

    ytd_data = []
    for emp in employees:
        slips = slips_by_emp.get(emp.emp_id, [])
        if slips:
            ytd_data.append({
                'employee': emp,
                'months': len(slips),
                'total_gross': sum(s.gross_pay for s in slips),
                'total_epf': sum(s.epf_deduction for s in slips),
                'total_esi': sum(s.esi_deduction for s in slips),
                'total_pt': sum(s.professional_tax for s in slips),
                'total_tds': sum(s.tds for s in slips),
                'total_deductions': sum(s.total_deductions for s in slips),
                'total_net': sum(s.net_pay for s in slips),
            })
    return render_template('report_ytd.html', ytd_data=ytd_data, selected_year=year)


@app.route('/reports/bank-transfer')
@login_required
@role_required('hr_admin', 'finance')
def report_bank_transfer():
    month = request.args.get('month', type=int, default=2)
    year = request.args.get('year', type=int, default=2026)
    slips = Payslip.query.options(joinedload(Payslip.employee)).filter_by(
        org_id=current_user.org_id, month=month, year=year
    ).all()
    return render_template('report_bank_transfer.html',
                           slips=slips, selected_month=month, selected_year=year,
                           months=MONTH_NAMES)


@app.route('/reports/audit-trail')
@login_required
@role_required('hr_admin', 'finance')
def report_audit():
    query = AuditLog.query.filter_by(org_id=current_user.org_id).order_by(AuditLog.timestamp.desc())
    pagination = paginate_query(query, max_per_page=100)
    runs = PayrollRun.query.filter_by(org_id=current_user.org_id).order_by(PayrollRun.created_at.desc()).all()
    return render_template('report_audit.html', audit_logs=pagination.items, runs=runs, pagination=pagination)


# ────────────────────────────────────────────
# ADDITIONAL SIDEBAR PAGES
# ────────────────────────────────────────────
@app.route('/help')
@login_required
def help_page():
    return render_template('help.html')


@app.route('/refer-earn')
@login_required
def refer_earn():
    return render_template('refer_earn.html')


@app.route('/pay-contractors')
@login_required
@role_required('hr_admin', 'finance')
def pay_contractors():
    return render_template('pay_contractors.html')


@app.route('/insurance')
@login_required
def insurance():
    return render_template('insurance.html')


@app.route('/company-details')
@login_required
@role_required('hr_admin', 'finance')
def company_details():
    return render_template('company_details.html')


@app.route('/settings')
@login_required
@role_required('hr_admin')
def settings():
    rates = StatutoryRate.query.filter_by(is_active=True).all()
    return render_template('settings.html', rates=rates)


@app.route('/choose-organization')
@login_required
def choose_organization():
    return render_template('choose_organization.html')


# ────────────────────────────────────────────
# DEMO DATA SEEDING (Realistic Fake Data)
# ────────────────────────────────────────────
def seed_database():
    """Create realistic fake demo data for testing & live preview."""
    if User.query.first():
        return

    org = Organization(name='LEARNTODAY DIGITAL PRIVATE LIMITED')
    db.session.add(org)
    db.session.flush()

    # Admin user
    admin = User(username='admin', email='admin@learntoday.fake', role='hr_admin',
                 org_id=org.org_id)
    admin.set_password('admin123')
    db.session.add(admin)

    # Finance user
    finance = User(username='finance', email='finance@learntoday.fake', role='finance',
                   org_id=org.org_id)
    finance.set_password('finance123')
    db.session.add(finance)

    db.session.flush()

    # Realistic demo employees with synthetic identifiers
    emp_data = [
        ('Rahul', 'Sharma', 'rahul@learntoday.fake', 'Engineering', 'Senior Developer', 1200000, 'ABCDE1234F', '123456789012', '123456789012', 'HDFC0001234'),
        ('Priya', 'Patel', 'priya@learntoday.fake', 'Engineering', 'Full Stack Developer', 900000, 'BCDEF2345G', '234567890123', '234567890123', 'HDFC0001234'),
        ('Amit', 'Kumar', 'amit@learntoday.fake', 'Design', 'UI/UX Designer', 750000, 'CDEFG3456H', '345678901234', '345678901234', 'ICIC0002345'),
        ('Sneha', 'Gupta', 'sneha@learntoday.fake', 'Marketing', 'Marketing Manager', 850000, 'DEFGH4567I', '456789012345', '456789012345', 'SBIN0003456'),
        ('Vikram', 'Singh', 'vikram@learntoday.fake', 'HR', 'HR Executive', 600000, 'EFGHI5678J', '567890123456', '567890123456', 'HDFC0001234'),
        ('Neha', 'Reddy', 'neha@learntoday.fake', 'Finance', 'Accountant', 700000, 'FGHIJ6789K', '678901234567', '678901234567', 'ICIC0002345'),
        ('Arjun', 'Nair', 'arjun@learntoday.fake', 'Engineering', 'Backend Developer', 1000000, 'GHIJK7890L', '789012345678', '789012345678', 'SBIN0003456'),
        ('Kavita', 'Joshi', 'kavita@learntoday.fake', 'Operations', 'Operations Lead', 800000, 'HIJKL8901M', '890123456789', '890123456789', 'HDFC0001234'),
    ]

    for fn, ln, email, dept, desig, ctc, pan, aadhaar, acc, ifsc in emp_data:
        emp = Employee(
            first_name=fn, last_name=ln, email=email,
            phone='+919876543210', pan_number=pan,
            aadhaar_number=aadhaar,
            bank_account=acc, bank_ifsc=ifsc,
            bank_name='HDFC Bank',
            department=dept, designation=desig,
            date_of_joining=date(2024, 1, 15),
            org_id=org.org_id
        )
        db.session.add(emp)
        db.session.flush()

        emp_user = User(
            username=f"EMP{emp.emp_id}",
            email=emp.email,
            role='employee',
            org_id=org.org_id
        )
        emp_user.set_password(f"{emp.first_name.lower()}123")
        db.session.add(emp_user)
        db.session.flush()
        emp.user_id = emp_user.id

        basic = round(ctc * 0.40, 2)
        hra = round(basic * 0.50, 2)
        da = round(ctc * 0.05, 2)
        conveyance = round(ctc * 0.05, 2)
        medical = round(ctc * 0.03, 2)
        special = round(max(0, ctc - basic - hra - da - conveyance - medical), 2)

        salary = SalaryStructure(
            emp_id=emp.emp_id,
            org_id=org.org_id,
            ctc_annual=ctc,
            basic_pay=basic,
            hra=hra,
            da=da,
            special_allowance=special,
            conveyance=conveyance,
            medical_allowance=medical
        )
        db.session.add(salary)

        for m in range(1, 4):
            att = AttendanceLog(
                emp_id=emp.emp_id,
                org_id=org.org_id,
                month=m, year=2026,
                total_working_days=22,
                days_present=21 if m == 2 else 22,
                days_absent=1 if m == 2 else 0,
                lop_days=0
            )
            db.session.add(att)

        tax = TaxDeclaration(
            emp_id=emp.emp_id,
            org_id=org.org_id,
            financial_year='2025-2026',
            regime='new',
            section_80c=150000,
            section_80d=25000
        )
        db.session.add(tax)

    db.session.flush()

    # Statutory rates
    rates = [
        ('EPF', 'Employee Provident Fund - Employee Share', 12.0, 15000),
        ('EPF_EMPLOYER', 'Employer PF Contribution', 12.0, 15000),
        ('ESI_EMPLOYEE', 'ESI Employee Share', 0.75, 21000),
        ('ESI_EMPLOYER', 'ESI Employer Share', 3.25, 21000),
        ('PT', 'Professional Tax', 200, None),
    ]
    for rt, desc, pct, limit in rates:
        rate = StatutoryRate(rate_type=rt, description=desc, percentage=pct, max_limit=limit)
        db.session.add(rate)

    db.session.commit()

    # Generate a demo payroll run for February 2026
    admin_user = User.query.filter_by(username='admin').first()
    run = PayrollRun(month=2, year=2026, created_by=admin_user.id if admin_user else None, status='finalized',
                     finalized_at=_utc_now(), org_id=org.org_id)
    db.session.add(run)
    db.session.flush()

    employees = Employee.query.filter_by(org_id=org.org_id).all()
    t_gross = t_ded = t_net = 0
    for emp in employees:
        slip = generate_payslip_for_employee(emp, run, 2, 2026)
        if slip:
            t_gross += slip.gross_pay
            t_ded += slip.total_deductions
            t_net += slip.net_pay
    run.total_gross = round(t_gross, 2)
    run.total_deductions = round(t_ded, 2)
    run.total_net = round(t_net, 2)
    run.employee_count = len(employees)
    db.session.commit()


_db_initialized = False


@app.before_request
def initialize_database():
    """Ensure database schema is created and seeded on startup for local SQLite development."""
    global _db_initialized
    if not _db_initialized and request.endpoint != 'static':
        uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''
        if uri.startswith('sqlite'):
            try:
                db.create_all()
                seed_database()
            except SQLAlchemyError as exc:
                app.logger.warning("SQLite initialization skipped: %s", exc)
        _db_initialized = True


if __name__ == '__main__':
    app.run(debug=False, port=5000)
