#!/bin/bash
# EventAlpha Backend — auto-restart wrapper.
# Restarts api.py if it crashes. Backoff caps at 60s; ~10 crashes/hour kills it.

set -u
cd "$(dirname "$0")"

# Pick the right Python: prefer project venv, then python3, then python.
# (On Windows the binary is "python", not "python3", and a stub aliased
# to the MS Store can intercept "python3" calls.)
if [ -x "../.venv/Scripts/python.exe" ]; then
    PY="../.venv/Scripts/python.exe"
elif [ -x "../.venv/bin/python" ]; then
    PY="../.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "Error: no Python found (looked in ../.venv and PATH)" >&2
    exit 1
fi
echo "Using Python: $PY"

if [ ! -f .deps_installed ] || [ requirements.txt -nt .deps_installed ]; then
    echo "Installing/updating dependencies..."
    "$PY" -m pip install -r requirements.txt && touch .deps_installed
fi

LOG_DIR="../logs"
mkdir -p "$LOG_DIR"

backoff=2
crash_count=0
window_start=$(date +%s)

while true; do
    echo "[$(date)] Starting API server on http://localhost:5000"
    "$PY" api.py 2>&1 | tee -a "$LOG_DIR/backend.log"
    rc=${PIPESTATUS[0]}
    echo "[$(date)] api.py exited rc=$rc"

    now=$(date +%s)
    if (( now - window_start > 3600 )); then
        crash_count=0
        window_start=$now
    fi
    crash_count=$((crash_count + 1))
    if (( crash_count > 10 )); then
        echo "[$(date)] FATAL: api.py crashed >10 times in last hour. Stopping watchdog." >&2
        exit 1
    fi

    echo "[$(date)] Restarting in ${backoff}s (crash $crash_count of 10/h)..."
    sleep "$backoff"
    backoff=$(( backoff * 2 ))
    if (( backoff > 60 )); then backoff=60; fi
done
