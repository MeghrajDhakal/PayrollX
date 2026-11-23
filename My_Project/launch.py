"""
PayrollX — Standalone Launcher
Starts the Flask server and opens the browser automatically.
This is the entry point for the PyInstaller EXE build.
"""
import sys
import os
import threading
import webbrowser
import time

# ── Determine base paths (works for both script & frozen exe) ──
if getattr(sys, 'frozen', False):
    # Running as compiled EXE
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS  # PyInstaller temp extraction dir
else:
    # Running as script
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = BASE_DIR

# Add src/ to Python path so imports work
SRC_DIR = os.path.join(BUNDLE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Set environment variables BEFORE importing the app
os.environ['PAYROLLX_BASE_DIR'] = BASE_DIR
os.environ['PAYROLLX_BUNDLE_DIR'] = BUNDLE_DIR


def open_browser(port=5000):
    """Wait for the server to start, then open the browser."""
    time.sleep(2)
    webbrowser.open(f'http://127.0.0.1:{port}')


def main():
    PORT = 5000

    print("=" * 50)
    print("  PayrollX — Payroll Management System")
    print("=" * 50)
    print(f"  Starting server on http://127.0.0.1:{PORT}")
    print("  Opening browser automatically...")
    print("  Press Ctrl+C to stop the server")
    print("=" * 50)

    # Open browser in a background thread
    threading.Thread(target=open_browser, args=(PORT,), daemon=True).start()

    # Import and run the Flask app
    from app import app
    try:
        from waitress import serve
        serve(app, host='127.0.0.1', port=PORT)
    except ImportError:
        print("  Waitress not installed. Falling back to development server.")
        app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
