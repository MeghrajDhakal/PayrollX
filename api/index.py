"""
PayrollX — Vercel Serverless Entrypoint
Directs incoming HTTP requests to the Flask WSGI application.
"""
import os
import sys

# Ensure My_Project/src is on the import path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, '..'))
PROJECT_DIR = os.path.join(ROOT_DIR, 'My_Project')
SRC_DIR = os.path.join(PROJECT_DIR, 'src')

for path in [SRC_DIR, PROJECT_DIR, ROOT_DIR]:
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ['VERCEL'] = '1'

from flask import redirect, jsonify
from app import app


class VercelPathNormalizer:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        # Extract the real requested URI from client headers
        client_path = (
            environ.get("HTTP_X_INVOKE_PATH")
            or environ.get("HTTP_X_FORWARDED_URI")
        )

        path_info = environ.get("PATH_INFO", "")

        if client_path and client_path not in ("/api/index", "/api/index.py"):
            environ["PATH_INFO"] = client_path.split("?")[0]
        elif path_info in ("/api/index", "/api/index.py", "/api", ""):
            environ["PATH_INFO"] = "/"
        elif path_info.startswith("/api/index/"):
            environ["PATH_INFO"] = path_info[len("/api/index"):]

        return self.wsgi_app(environ, start_response)


# Apply WSGI middleware
app.wsgi_app = VercelPathNormalizer(app.wsgi_app)


# Explicit fallback so neither '/' nor '/api/index' can ever 404
@app.route("/", endpoint="root_entry")
@app.route("/api/index", endpoint="api_index_entry")
@app.route("/api/index.py", endpoint="api_index_py_entry")
def index_entry():
    # Detect existing login or dashboard route automatically
    rules = [r.rule for r in app.url_map.iter_rules()]
    for candidate in ("/login", "/auth/login", "/dashboard"):
        if candidate in rules:
            return redirect(candidate)

    # If no candidate route matched, return a 200 status with registered routes
    return jsonify({
        "status": "online",
        "message": "PayrollX backend is running",
        "available_routes": [r for r in rules if not r.startswith("/static")]
    }), 200
