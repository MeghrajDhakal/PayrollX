"""
PayrollX — Production Gunicorn Configuration
Tuned for resilient multi-worker concurrency, keepalive, and memory management.
"""
import os
import multiprocessing

# Network binding
host = os.environ.get("HOST", "0.0.0.0")
port = os.environ.get("PORT", "5000")
bind = f"{host}:{port}"

# Worker processes & concurrency
default_workers = (multiprocessing.cpu_count() * 2) + 1
workers = int(os.environ.get("GUNICORN_WORKERS", default_workers))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
worker_class = "gthread"

# Timeout bounds
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 60))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))

# Memory bounds & recycling to prevent memory leaks
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 1000))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 100))

# Logging configuration (stdout/stderr for container & cloud platforms)
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
capture_output = True
