import os

# Detect Vercel serverless environment
IS_VERCEL = os.environ.get('VERCEL', False)

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'payroll-secret-key-2026-razorpay-clone'
    # Vercel only allows writes to /tmp; local dev uses instance/ folder
    if IS_VERCEL:
        SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/payroll.db'
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///payroll.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
