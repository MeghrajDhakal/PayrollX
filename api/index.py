"""
PayrollX — Vercel Serverless Entrypoint
Directs incoming HTTP requests to the Flask WSGI application.
"""
import os
import sys

# Signal Vercel environment before importing application modules
os.environ['VERCEL'] = '1'

# Calculate absolute paths to ensure reliable imports in Vercel lambda
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, '..'))
PROJECT_DIR = os.path.join(ROOT_DIR, 'My_Project')
SRC_DIR = os.path.join(PROJECT_DIR, 'src')

for path in [SRC_DIR, PROJECT_DIR, ROOT_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

from app import app, db, seed_database

# Pre-initialize SQLite schema and seed demo data in /tmp during cold start
try:
    with app.app_context():
        db.create_all()
        seed_database()
except Exception:
    pass
