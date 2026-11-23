"""
PayrollX — Vercel Serverless Entrypoint
Directs incoming HTTP requests to the Flask WSGI application.
"""
import os
import sys

# Ensure My_Project/src is on the import path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, ".."))
PROJECT_DIR = os.path.join(ROOT_DIR, "My_Project")
SRC_DIR = os.path.join(PROJECT_DIR, "src")

for path in [SRC_DIR, PROJECT_DIR, ROOT_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ["VERCEL"] = "1"

from flask import redirect
from app import app  # imports your Flask app instance


class VercelPathNormalizer:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        # Extract the real requested URI from Vercel edge proxy headers
        client_path = (
            environ.get("HTTP_X_INVOKE_PATH")
            or environ.get("HTTP_X_FORWARDED_URI")
            or environ.get("HTTP_X_MATCHED_PATH")
        )
        path_info = environ.get("PATH_INFO", "")

        if client_path and client_path not in ("/api/index", "/api/index.py", "/api"):
            environ["PATH_INFO"] = client_path.split("?")[0]
        elif path_info in ("/api/index", "/api/index.py", "/api", ""):
            environ["PATH_INFO"] = "/"
        elif path_info.startswith("/api/index/"):
            environ["PATH_INFO"] = path_info[len("/api/index"):]

        return self.wsgi_app(environ, start_response)


# Apply WSGI middleware
app.wsgi_app = VercelPathNormalizer(app.wsgi_app)


# Safety fallback: catch any stray requests that still arrive as /api/index
@app.route("/api/index", endpoint="vercel_api_index_fallback")
@app.route("/api/index.py", endpoint="vercel_api_index_py_fallback")
def handle_vercel_raw_destination():
    # If a login or dashboard route exists in your app, redirect there
    existing_rules = [rule.rule for rule in app.url_map.iter_rules()]
    for target in ("/login", "/auth/login", "/dashboard", "/"):
        if target in existing_rules and target not in ("/api/index", "/api/index.py"):
            return redirect(target)
    return "PayrollX is running", 200
