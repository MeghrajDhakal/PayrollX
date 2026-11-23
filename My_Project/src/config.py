import os
import sys
from datetime import timedelta

# Detect environment
PAYROLLX_ENV = os.environ.get('PAYROLLX_ENV', os.environ.get('FLASK_ENV', 'development')).lower()
IS_PRODUCTION = PAYROLLX_ENV == 'production'
IS_VERCEL = bool(os.environ.get('VERCEL', False))

# Determine base directory (works for script, frozen exe, and Vercel)
if getattr(sys, 'frozen', False):
    BASE_DIR = os.environ.get('PAYROLLX_BASE_DIR', os.path.dirname(sys.executable))
    BUNDLE_DIR = os.environ.get('PAYROLLX_BUNDLE_DIR', sys._MEIPASS)
elif IS_VERCEL:
    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = os.path.dirname(SRC_DIR)
    BUNDLE_DIR = BASE_DIR
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)
else:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    BUNDLE_DIR = BASE_DIR


def validate_production_secrets():
    """Fails fast at startup if critical production secrets are missing or default."""
    if IS_VERCEL:
        # On Vercel serverless demo deployment, allow fallback demo secrets so the demo runs out-of-the-box
        return

    env = os.environ.get('PAYROLLX_ENV', os.environ.get('FLASK_ENV', 'development')).lower()
    if env != 'production':
        return

    secret_key = os.environ.get('SECRET_KEY')
    if not secret_key or secret_key.strip() in ('', 'payroll-secret-key-2026-razorpay-clone', 'dev-secret-change-in-production'):
        raise ValueError(
            "FATAL: Production mode requires a secure, non-default 'SECRET_KEY' environment variable. "
            "Generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))'"
        )

    jwt_secret = os.environ.get('JWT_SECRET_KEY')
    if not jwt_secret or jwt_secret.strip() in ('', 'payrollx-jwt-secret-prod-2026-xK9mZ', 'dev-jwt-secret-change-in-production'):
        raise ValueError(
            "FATAL: Production mode requires a secure, non-default 'JWT_SECRET_KEY' environment variable. "
            "Generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))'"
        )

    db_url = os.environ.get('DATABASE_URL')
    if not db_url and not IS_VERCEL:
        raise ValueError(
            "FATAL: Production mode requires a managed PostgreSQL 'DATABASE_URL' environment variable "
            "(e.g. postgresql://user:pass@host:5432/payrollx)."
        )


class Config:
    # Validate secrets on class initialization
    validate_production_secrets()

    # Secret keys
    SECRET_KEY = os.environ.get('SECRET_KEY') or (
        'payrollx-vercel-demo-secret-2026-secure-key' if IS_VERCEL else (
            'dev-insecure-secret-key-only-for-local-testing-2026' if not IS_PRODUCTION else None
        )
    )
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or (
        'payrollx-vercel-demo-jwt-2026-secure-key' if IS_VERCEL else (
            'dev-insecure-jwt-key-only-for-local-testing-2026' if not IS_PRODUCTION else None
        )
    )
    JWT_EXPIRY_HOURS = int(os.environ.get('JWT_EXPIRY_HOURS', 8))
    JWT_ALGORITHM = 'HS256'

    # ── Session Cookie Hardening ──
    SESSION_COOKIE_HTTPONLY = True
    # Secure cookie enabled in production or if explicitly configured
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'true' if IS_PRODUCTION else 'false').lower() == 'true'
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get('SESSION_LIFETIME_HOURS', 8)))
    SESSION_IDLE_TIMEOUT_MINUTES = int(os.environ.get('SESSION_IDLE_TIMEOUT_MINUTES', 30))

    # ── Database Configuration ──
    raw_db_url = os.environ.get('DATABASE_URL')
    if raw_db_url:
        # Standardize modern SQLAlchemy postgres dialect
        if raw_db_url.startswith('postgres://'):
            raw_db_url = raw_db_url.replace('postgres://', 'postgresql://', 1)
        SQLALCHEMY_DATABASE_URI = raw_db_url
    elif IS_VERCEL:
        SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/payroll.db'
    else:
        INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(INSTANCE_DIR, 'payroll.db')

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Connection pooling for SQLAlchemy (tuned for PostgreSQL / SQLite)
    if SQLALCHEMY_DATABASE_URI.startswith('sqlite'):
        SQLALCHEMY_ENGINE_OPTIONS = {
            'connect_args': {'check_same_thread': False}
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            'pool_size': int(os.environ.get('DB_POOL_SIZE', 10)),
            'max_overflow': int(os.environ.get('DB_MAX_OVERFLOW', 20)),
            'pool_timeout': int(os.environ.get('DB_POOL_TIMEOUT', 30)),
            'pool_recycle': int(os.environ.get('DB_POOL_RECYCLE', 1800)),
            'pool_pre_ping': True,
        }

    # ── File Upload Bounds ──
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB max payload size
    ALLOWED_UPLOAD_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

    # ── Directories ──
    if IS_VERCEL:
        PDF_OUTPUT_DIR = '/tmp/payslips'
    else:
        INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
        PDF_OUTPUT_DIR = os.path.join(INSTANCE_DIR, 'payslips')
    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
