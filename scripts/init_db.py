"""
PayrollX — Database Schema Initializer (PostgreSQL / SQLite)
Idempotently creates all tables and constraints defined in SQLAlchemy models.
Usage:
    python3 scripts/init_db.py
"""
import os
import sys

# Calculate directory paths
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, '..'))
PROJECT_DIR = os.path.join(ROOT_DIR, 'My_Project')
SRC_DIR = os.path.join(PROJECT_DIR, 'src')

for p in [SRC_DIR, PROJECT_DIR, ROOT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from app import app, db
from sqlalchemy import text, inspect

def init_database():
    """Initializes database schema idempotently."""
    print("=" * 60)
    print("PayrollX — Database Initialization")
    print("=" * 60)

    with app.app_context():
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''
        # Mask credentials in connection URI for safe console output
        if '@' in db_uri:
            prefix = db_uri.split('@')[0]
            masked_prefix = prefix.split(':')[0] + ':****'
            masked_uri = masked_prefix + '@' + db_uri.split('@')[1]
        else:
            masked_uri = db_uri
        print(f"[*] Target Database: {masked_uri}")

        print("[*] Creating all tables and constraints...")
        db.create_all()

        print("[*] Verifying database connectivity...")
        result = db.session.execute(text("SELECT 1")).scalar()
        if result == 1:
            print("[+] Database connection verified successfully (SELECT 1 -> OK).")

        # Inspect and report created tables
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        print(f"[+] Total tables created/verified ({len(tables)}):")
        for t in sorted(tables):
            print(f"    - {t}")

    print("=" * 60)
    print("[+] Database schema initialization complete.")
    print("=" * 60)

if __name__ == '__main__':
    init_database()
