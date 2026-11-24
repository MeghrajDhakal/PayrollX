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
    """Fails fast at startup if critical production or Vercel secrets are missing or default."""
    env = os.environ.get('PAYROLLX_ENV', os.environ.get('FLASK_ENV', 'development')).lower()
    is_prod = env == 'production'
    is_vercel = bool(os.environ.get('VERCEL') == '1' or os.environ.get('VERCEL_ENV'))
    if not is_prod and not is_vercel:
        return

    secret_key = os.environ.get('SECRET_KEY')
    if is_prod and (not secret_key or secret_key.strip() in (
        '',
        'payroll-secret-key-2026-razorpay-clone',
        'dev-secret-change-in-production',
        'dev-insecure-secret-key-only-for-local-testing-2026'
    )):
        raise ValueError(
            "FATAL: Production mode requires a secure 'SECRET_KEY' environment variable. "
            "Please configure SECRET_KEY in your environment. "
            "Generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))'"
        )

    db_url = os.environ.get('DATABASE_URL')
    if (is_prod or is_vercel) and (not db_url or not db_url.strip()):
        raise ValueError(
            "FATAL: Production/Vercel mode requires a managed PostgreSQL 'DATABASE_URL' environment variable "
            "(e.g. postgresql://user:password@ep-xyz.neon.tech/payrollx?sslmode=require). "
            "Please paste your pooled PostgreSQL connection string into Vercel Project Settings -> Environment Variables."
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
        os.environ.get('SECRET_KEY') if (IS_PRODUCTION or IS_VERCEL) else 'dev-insecure-jwt-key-only-for-local-testing-2026'
    )
    JWT_EXPIRY_HOURS = int(os.environ.get('JWT_EXPIRY_HOURS', 8))
    JWT_ALGORITHM = 'HS256'

    # ── Session Cookie Hardening ──
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'true' if (IS_PRODUCTION or IS_VERCEL) else 'false').lower() == 'true'
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get('SESSION_LIFETIME_HOURS', 8)))
    SESSION_IDLE_TIMEOUT_MINUTES = int(os.environ.get('SESSION_IDLE_TIMEOUT_MINUTES', 30))

    # ── Database Configuration ──
    raw_db_url = os.environ.get('DATABASE_URL')
    if raw_db_url:
        # Standardize modern SQLAlchemy postgres dialect to psycopg2
        if raw_db_url.startswith('postgres://'):
            raw_db_url = raw_db_url.replace('postgres://', 'postgresql+psycopg2://', 1)
        elif raw_db_url.startswith('postgresql://'):
            raw_db_url = raw_db_url.replace('postgresql://', 'postgresql+psycopg2://', 1)
        SQLALCHEMY_DATABASE_URI = raw_db_url
    elif IS_VERCEL or IS_PRODUCTION:
        SQLALCHEMY_DATABASE_URI = None
    else:
        INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(INSTANCE_DIR, 'payroll.db')

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Serverless Pooled Engine Options ──
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres'):
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping": True,
            "pool_recycle": int(os.environ.get('DB_POOL_RECYCLE', 300)),
            "connect_args": {"sslmode": os.environ.get('DB_SSL_MODE', "require")},
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False}
        }

    # ── File Upload Bounds ──
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB max payload size
    ALLOWED_UPLOAD_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

    # ── Redis Caching Configuration ──
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    REDIS_ENABLED = os.environ.get('REDIS_ENABLED', 'true').lower() in ('true', '1', 'yes')
    REDIS_MAX_CONNECTIONS = int(os.environ.get('REDIS_MAX_CONNECTIONS', 10 if (IS_VERCEL or IS_PRODUCTION) else 20))
    REDIS_CONNECT_TIMEOUT = float(os.environ.get('REDIS_CONNECT_TIMEOUT', 0.2))  # 200ms
    REDIS_SOCKET_TIMEOUT = float(os.environ.get('REDIS_SOCKET_TIMEOUT', 0.3))    # 300ms
