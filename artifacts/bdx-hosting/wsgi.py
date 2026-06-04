"""
WSGI entry point for gunicorn.
Usage: gunicorn wsgi:application
"""
from app import app, init_db

# Ensure DB is initialised for every gunicorn worker
try:
    init_db()
except Exception as e:
    import sys
    print(f"[WARN] init_db error (may already exist): {e}", file=sys.stderr)

application = app
