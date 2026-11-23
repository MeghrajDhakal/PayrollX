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

from app import app


class VercelPathFixMiddleware:
    """Restores the original requested path in WSGI environment when Vercel rewrites to /api/index."""
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        matched_path = environ.get("HTTP_X_MATCHED_PATH") or environ.get("HTTP_X_FORWARDED_PATH")
        path_info = environ.get("PATH_INFO", "")
        if path_info in ("/api/index", "/api/index.py"):
            environ["PATH_INFO"] = matched_path if matched_path else "/"
        elif path_info.startswith("/api/index/"):
            environ["PATH_INFO"] = path_info[len("/api/index"):]
        return self.wsgi_app(environ, start_response)


# Wrap Flask app instance with WSGI path normalization
app.wsgi_app = VercelPathFixMiddleware(app.wsgi_app)
