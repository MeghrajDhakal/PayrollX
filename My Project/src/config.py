import os
import sys

# Detect Vercel serverless environment
IS_VERCEL = os.environ.get('VERCEL', False)

# Determine base directory (works for script, frozen exe, and Vercel)
if getattr(sys, 'frozen', False):
    # Running as PyInstaller EXE — writable data goes next to the .exe
    BASE_DIR = os.environ.get('PAYROLLX_BASE_DIR', os.path.dirname(sys.executable))
    # Bundled resources are in PyInstaller's temp dir
    BUNDLE_DIR = os.environ.get('PAYROLLX_BUNDLE_DIR', sys._MEIPASS)
else:
    # Running as script — go up one level from src/
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    BUNDLE_DIR = BASE_DIR


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'payroll-secret-key-2026-razorpay-clone'

    # ── JWT Configuration ──
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or 'payrollx-jwt-secret-prod-2026-xK9mZ'
    JWT_EXPIRY_HOURS = int(os.environ.get('JWT_EXPIRY_HOURS', 8))
    JWT_ALGORITHM = 'HS256'

    # Vercel only allows writes to /tmp; local dev uses instance/ folder
    if IS_VERCEL:
        SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/payroll.db'
        PDF_OUTPUT_DIR = '/tmp/payslips'
    else:
        INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(INSTANCE_DIR, 'payroll.db')
        PDF_OUTPUT_DIR = os.path.join(INSTANCE_DIR, 'payslips')

    SQLALCHEMY_TRACK_MODIFICATIONS = False
