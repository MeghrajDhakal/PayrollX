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


class VercelPathNormalizer:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        # Retrieve the original browser path from Vercel edge headers
        forwarded_uri = (
            environ.get("HTTP_X_FORWARDED_URI") or
            environ.get("HTTP_X_MATCHED_PATH") or
            environ.get("HTTP_X_FORWARDED_PATH")
        )
        path_info = environ.get("PATH_INFO", "")

        if path_info in ("/api/index", "/api/index.py", "/api"):
            # If forwarded_uri exists and isn't /api/index, use it; otherwise default to /
            environ["PATH_INFO"] = (
                forwarded_uri if (forwarded_uri and not forwarded_uri.startswith("/api")) else "/"
            )
        elif path_info.startswith("/api/index/"):
            environ["PATH_INFO"] = path_info[len("/api/index"):]

        return self.wsgi_app(environ, start_response)


# Apply the middleware to your Flask instance
app.wsgi_app = VercelPathNormalizer(app.wsgi_app)
