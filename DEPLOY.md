# PayrollX — Production Deployment & Operations Guide

This guide documents the procedures for deploying, maintaining, and rolling back **PayrollX** in production.

---

## 1. System Architecture & Prerequisites
- **Runtime**: Python 3.10+ (tested on Python 3.13 / 3.14)
- **WSGI Server**: Gunicorn with `gthread` workers
- **Database**:
  - **Local/Demo**: SQLite with WAL mode & Foreign Key enforcement
  - **Cloud/Production (Render, Fly.io, AWS, GCP)**: Managed PostgreSQL (v14+) with automated daily backups & PITR
- **Reverse Proxy**: NGINX / Cloudflare with TLS termination and HTTP/2

---

## 2. Environment Variables Reference

| Variable | Required in Prod? | Default (Dev) | Description |
|---|---|---|---|
| `PAYROLLX_ENV` | **Yes** | `development` | Set to `production` to enforce fail-fast security checks |
| `SECRET_KEY` | **Yes** | Dev fallback | Strong 256-bit cryptographic secret for Flask sessions |
| `JWT_SECRET_KEY` | **Yes** | Dev fallback | Cryptographic secret for signing API JWT tokens |
| `DATABASE_URL` | **Yes** | SQLite `instance/payroll.db` | PostgreSQL connection URI (`postgresql://user:pass@host:5432/payrollx`) |
| `SESSION_COOKIE_SECURE` | Recommended | `true` (when in prod) | Enforce `Secure` flag on HTTPS |
| `SESSION_IDLE_TIMEOUT_MINUTES` | No | `30` | Inactivity session timeout |
| `SESSION_LIFETIME_HOURS` | No | `8` | Absolute session timeout |
| `PORT` | No | `5000` | Port for WSGI listener |
| `GUNICORN_WORKERS` | No | `(2 * CPU) + 1` | Number of worker processes |
| `GUNICORN_THREADS` | No | `2` | Threads per worker process |
| `GUNICORN_TIMEOUT` | No | `60` | Request timeout bound (seconds) |

### Generating Cryptographic Secrets
```bash
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_hex(32))"
```

---

## 3. Production Deployment Step-by-Step

### Step 1: Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 2: Run Database Migrations
Always run database migrations before launching new application workers:
```bash
cd My_Project
export PYTHONPATH=src
flask db upgrade
```

### Step 3: Run Gunicorn Production Server
```bash
cd My_Project
export PYTHONPATH=src
gunicorn --config gunicorn_config.py "app:app"
```

For systemd service configuration (`/etc/systemd/system/payrollx.service`):
```ini
[Unit]
Description=PayrollX Production Application
After=network.target postgresql.service

[Service]
User=payrollx
Group=payrollx
WorkingDirectory=/opt/payrollx/My_Project
Environment="PATH=/opt/payrollx/.venv/bin"
EnvironmentFile=/etc/payrollx.env
ExecStart=/opt/payrollx/.venv/bin/gunicorn --config gunicorn_config.py "app:app"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 4. Health Checks & Monitoring

- **Endpoint**: `GET /health`
- **Expected Status**: `200 OK`
- **Response Format**:
```json
{
  "status": "healthy",
  "database": "connected",
  "timestamp": "2026-11-23T19:30:00Z",
  "request_id": "a9c78d5e-21ef-42f0-975d-3d4e8c1b6a7f"
}
```
- **Alert Trigger**: Any status other than 200 or response time > 1500ms triggers PagerDuty / SRE alert.

---

## 5. Automated Database Backup & Disaster Recovery

### Manual Backup Command
```bash
# SQLite (Local/Persistent Disk):
PYTHONPATH=My_Project/src python3 scripts/backup_restore.py backup My_Project/instance/payroll.db

# PostgreSQL:
export DATABASE_URL="postgresql://user:pass@host:5432/payrollx"
PYTHONPATH=My_Project/src python3 scripts/backup_restore.py backup
```

### Test Restore Command
```bash
PYTHONPATH=My_Project/src python3 scripts/backup_restore.py test
```

### Automated Cron Job (Daily at 02:00 UTC)
```crontab
0 2 * * * cd /opt/payrollx && PYTHONPATH=My_Project/src /opt/payrollx/.venv/bin/python3 scripts/backup_restore.py backup >> /var/log/payrollx_backup.log 2>&1
```

---

## 6. Rollback Procedures

### Application Code Rollback
If a regression or fault is detected post-deployment:
```bash
# 1. Checkout previous stable release or git commit
git checkout <previous_stable_commit_or_tag>

# 2. Re-install pinned requirements if changed
pip install -r requirements.txt

# 3. Reload WSGI server gracefully (zero downtime)
sudo systemctl reload payrollx
# or if running standalone gunicorn:
kill -HUP $(pgrep -f gunicorn)
```

### Database Migration Rollback
If a database migration needs to be reversed:
```bash
cd My_Project
export PYTHONPATH=src
# Downgrade by one migration revision
flask db downgrade -1
```

### Emergency Restore from Backup
```bash
# SQLite:
PYTHONPATH=My_Project/src python3 scripts/backup_restore.py restore backups/payroll_backup_YYYYMMDD_HHMMSS.db My_Project/instance/payroll.db

# PostgreSQL:
PYTHONPATH=My_Project/src python3 scripts/backup_restore.py restore backups/postgres_backup_YYYYMMDD_HHMMSS.sql.gz
```
Restart the service immediately following database restoration:
```bash
sudo systemctl restart payrollx
```
