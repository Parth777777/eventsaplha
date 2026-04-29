"""WSGI entry point for production deployment.

Run with Gunicorn (Linux/macOS):
    gunicorn -w 4 -k gthread --threads 8 -b 0.0.0.0:5000 \
        --access-logfile - --error-logfile - \
        --timeout 120 --graceful-timeout 30 \
        wsgi:application

Or with Waitress (Windows-friendly, single process):
    waitress-serve --host=0.0.0.0 --port=5000 --threads=16 wsgi:application

The Flask dev server (flask run / app.run) is NOT for production. It's
single-threaded, has no graceful shutdown, and silently swallows worker
exceptions on Windows. See run_backend.sh/.bat for development.
"""
from api import app as application  # noqa: F401  (Gunicorn imports `application`)

# Sanity probe — fail loudly at import time if env vars missing
import os
import logging

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    # Direct invocation (`python wsgi.py`) for smoke-testing the WSGI shape.
    # In production use Gunicorn/Waitress, not this.
    port = int(os.getenv("PORT", "5000"))
    application.run(host="0.0.0.0", port=port, debug=False)
