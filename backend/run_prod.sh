#!/bin/bash
# Production runner. Picks the right WSGI server for the host OS.
#   Linux/macOS  -> Gunicorn with worker pool
#   Windows/Git Bash -> Waitress (Gunicorn doesn't run on Windows)
# Override with WSGI_SERVER=gunicorn|waitress to force one.

set -u
cd "$(dirname "$0")"

# Pick Python (prefer venv)
if [ -x "../.venv/Scripts/python.exe" ]; then
    PY="../.venv/Scripts/python.exe"
elif [ -x "../.venv/bin/python" ]; then
    PY="../.venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi
[ -z "$PY" ] && { echo "no python found" >&2; exit 1; }

# Auto-pick server unless overridden
SERVER="${WSGI_SERVER:-}"
if [ -z "$SERVER" ]; then
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) SERVER="waitress" ;;
        *)                    SERVER="gunicorn" ;;
    esac
fi

PORT="${PORT:-5000}"
WORKERS="${WORKERS:-4}"
THREADS="${THREADS:-8}"

LOG_DIR="../logs"
mkdir -p "$LOG_DIR"

case "$SERVER" in
    gunicorn)
        echo "[$(date)] starting gunicorn on :$PORT (workers=$WORKERS, threads=$THREADS)"
        exec "$PY" -m gunicorn -w "$WORKERS" -k gthread --threads "$THREADS" \
            -b "0.0.0.0:$PORT" \
            --timeout 120 --graceful-timeout 30 \
            --access-logfile "$LOG_DIR/access.log" \
            --error-logfile "$LOG_DIR/error.log" \
            wsgi:application
        ;;
    waitress)
        echo "[$(date)] starting waitress on :$PORT (threads=$THREADS)"
        exec "$PY" -m waitress --host=0.0.0.0 --port="$PORT" --threads="$THREADS" \
            wsgi:application
        ;;
    *)
        echo "unknown WSGI_SERVER=$SERVER (use gunicorn|waitress)" >&2
        exit 2
        ;;
esac
