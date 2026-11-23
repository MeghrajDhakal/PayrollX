"""
PayrollX — Vercel Serverless Entrypoint
Directs incoming HTTP requests to the Flask WSGI application.
"""
import os
import sys

# Ensure your application source directory is on sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, '..'))
PROJECT_DIR = os.path.join(ROOT_DIR, 'My_Project')
SRC_DIR = os.path.join(PROJECT_DIR, 'src')

for path in [SRC_DIR, PROJECT_DIR, ROOT_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ['VERCEL'] = '1'

from app import app  # import your Flask app instance


class VercelPathMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        # Vercel passes the original requested URL in HTTP_X_MATCHED_PATH
        matched_path = (
            environ.get("HTTP_X_MATCHED_PATH") or
            environ.get("HTTP_X_FORWARDED_URI") or
            environ.get("HTTP_X_FORWARDED_PATH")
        )
        if matched_path:
            environ["PATH_INFO"] = matched_path.split("?")[0]
        elif environ.get("PATH_INFO") in ("/api/index", "/api/index.py", "/api"):
            environ["PATH_INFO"] = "/"
        elif environ.get("PATH_INFO", "").startswith("/api/index/"):
            environ["PATH_INFO"] = environ["PATH_INFO"][len("/api/index"):]
        return self.wsgi_app(environ, start_response)


# Apply the middleware
app.wsgi_app = VercelPathMiddleware(app.wsgi_app)

# Defensive fallback: If '/' is not explicitly defined, redirect to '/login'
registered_rules = [rule.rule for rule in app.url_map.iter_rules()]
if "/" not in registered_rules:
    from flask import redirect
    @app.route("/")
    @app.route("/api/index")
    def root_redirect():
        return redirect("/login")
