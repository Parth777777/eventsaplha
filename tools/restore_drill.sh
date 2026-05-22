#!/usr/bin/env bash
# Restore-drill: validate that the latest backup is actually restorable.
# Untested backups don't exist. Run this monthly.
#
# Restores the most recent ae-*.sql.gz into a SCRATCH database (never prod)
# and reports row counts on the critical tables.
#
# Required env:
#   SCRATCH_DATABASE_URL  postgres://user:pass@host:5432/scratch_db
#   BACKUP_LOCAL_DIR      where backup.sh writes (default /var/backups/alphaevent)

set -euo pipefail

BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-/var/backups/alphaevent}"

if [[ -z "${SCRATCH_DATABASE_URL:-}" ]]; then
    echo "[restore-drill] FATAL: SCRATCH_DATABASE_URL not set"
    echo "Create a scratch DB first: createdb scratch_ae (or via Supabase)"
    exit 1
fi

LATEST=$(ls -t "${BACKUP_LOCAL_DIR}"/ae-*.sql.gz 2>/dev/null | head -1 || true)
if [[ -z "$LATEST" ]]; then
    echo "[restore-drill] FATAL: no backups found in ${BACKUP_LOCAL_DIR}"
    exit 2
fi

echo "[restore-drill] restoring ${LATEST} into scratch DB"

# Reset scratch DB to a clean state
psql "$SCRATCH_DATABASE_URL" -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;" >/dev/null

# Restore
gunzip -c "$LATEST" | psql "$SCRATCH_DATABASE_URL" -q >/dev/null

# Report row counts on the critical tables
echo "[restore-drill] row counts:"
psql "$SCRATCH_DATABASE_URL" -t -A -c "
    SELECT format('  %-30s %s', table_name, n_live_tup)
    FROM (
        SELECT 'signals'              AS table_name, (SELECT count(*) FROM signals) AS n_live_tup
        UNION ALL SELECT 'events',     (SELECT count(*) FROM events)
        UNION ALL SELECT 'users',      (SELECT count(*) FROM users)
        UNION ALL SELECT 'watchlist',  (SELECT count(*) FROM watchlist)
        UNION ALL SELECT 'job_runs',   (SELECT count(*) FROM job_runs)
        UNION ALL SELECT 'job_queue',  (SELECT count(*) FROM job_queue)
    ) t;
" 2>&1 | sed 's/^/  /'

echo "[restore-drill] PASS"
