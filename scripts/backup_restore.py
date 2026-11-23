#!/usr/bin/env python3
"""
PayrollX — Database Backup & Restore Utility
Supports local SQLite backups (with file integrity verification)
and managed PostgreSQL backups via pg_dump / pg_restore.
"""
import os
import sys
import shutil
import sqlite3
import subprocess
from datetime import datetime


def backup_sqlite(source_path: str, backup_dir: str) -> str:
    """Create a verified SQLite online backup."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source database not found: {source_path}")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"payroll_backup_{timestamp}.db")

    # Use SQLite online backup API for safe live copying without corruption
    src_conn = sqlite3.connect(source_path)
    dst_conn = sqlite3.connect(dest_path)
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        src_conn.close()
        dst_conn.close()

    # Integrity check on backup file
    chk_conn = sqlite3.connect(dest_path)
    try:
        cur = chk_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchone()
        if not res or res[0] != "ok":
            raise ValueError(f"Backup integrity check failed: {res}")
    finally:
        chk_conn.close()

    print(f"[SUCCESS] SQLite backup created and verified: {dest_path}")
    return dest_path


def restore_sqlite(backup_path: str, target_path: str) -> bool:
    """Restore SQLite database from backup after verifying integrity."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    chk_conn = sqlite3.connect(backup_path)
    try:
        cur = chk_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        res = cur.fetchone()
        if not res or res[0] != "ok":
            raise ValueError(f"Cannot restore from corrupt backup: {res}")
    finally:
        chk_conn.close()

    # Create safety backup of existing target before overwrite
    if os.path.exists(target_path):
        safety_path = f"{target_path}.pre_restore_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target_path, safety_path)
        print(f"[INFO] Pre-restore safety copy saved to: {safety_path}")

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    shutil.copy2(backup_path, target_path)
    print(f"[SUCCESS] Database successfully restored from {backup_path} to {target_path}")
    return True


def backup_postgres(database_url: str, backup_dir: str) -> str:
    """Backup PostgreSQL database using pg_dump."""
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"postgres_backup_{timestamp}.sql.gz")

    cmd = f"pg_dump {database_url} | gzip > {dest_path}"
    subprocess.run(cmd, shell=True, check=True)
    print(f"[SUCCESS] PostgreSQL backup completed: {dest_path}")
    return dest_path


def restore_postgres(backup_path: str, database_url: str) -> bool:
    """Restore PostgreSQL database from .sql.gz backup."""
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    cmd = f"gunzip -c {backup_path} | psql {database_url}"
    subprocess.run(cmd, shell=True, check=True)
    print(f"[SUCCESS] PostgreSQL database restored from {backup_path}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: backup_restore.py [backup|restore|test] [source/backup_path] [target_path]")
        sys.exit(1)

    action = sys.argv[1].lower()
    db_url = os.environ.get("DATABASE_URL", "")

    if action == "backup":
        if db_url and "postgresql" in db_url:
            backup_postgres(db_url, "backups")
        else:
            src = sys.argv[2] if len(sys.argv) > 2 else "My_Project/instance/payroll.db"
            backup_sqlite(src, "backups")
    elif action == "restore":
        if len(sys.argv) < 3:
            print("Specify backup file to restore.")
            sys.exit(1)
        backup_file = sys.argv[2]
        if db_url and "postgresql" in db_url:
            restore_postgres(backup_file, db_url)
        else:
            target = sys.argv[3] if len(sys.argv) > 3 else "My_Project/instance/payroll.db"
            restore_sqlite(backup_file, target)
    elif action == "test":
        print("[INFO] Running backup and restore test verification...")
        test_src = "My_Project/instance/payroll.db"
        if not os.path.exists(test_src):
            # initialize database first
            from app import app, db
            with app.app_context():
                db.create_all()

        backup_file = backup_sqlite(test_src, "test_backups")
        test_restore_dest = "test_backups/test_restored.db"
        restore_sqlite(backup_file, test_restore_dest)

        # Verify tables in restored database
        conn = sqlite3.connect(test_restore_dest)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table';")
        count = cur.fetchone()[0]
        conn.close()
        print(f"[SUCCESS] Test restore verified! Tables restored: {count}")
        shutil.rmtree("test_backups", ignore_errors=True)


if __name__ == "__main__":
    main()
