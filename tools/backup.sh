#!/usr/bin/env bash
# Nightly pg_dump backup → Oracle Object Storage (10 GB free tier).
# Cron entry (on the Oracle VM, not in a container):
#   0 21 * * *  /opt/alphaevent/tools/backup.sh >> /var/log/ae_backup.log 2>&1
# That's 02:30 IST (UTC+5:30). After 30 days the oldest .sql.gz is dropped.
#
# Required env (export in /etc/profile.d/ae.sh or pull from .env.production):
#   DATABASE_URL          postgres://user:pass@host:5432/dbname
#   OCI_NAMESPACE         your OCI tenancy object-storage namespace
#   OCI_BUCKET            bucket name (must already exist; create once)
#   BACKUP_LOCAL_DIR      e.g. /var/backups/alphaevent (default below)
#
# Tools required on host:
#   - postgresql-client (for pg_dump)
#   - oci CLI (for object storage upload)
#       https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm

set -euo pipefail

BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-/var/backups/alphaevent}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="${BACKUP_LOCAL_DIR}/ae-${TS}.sql.gz"

mkdir -p "$BACKUP_LOCAL_DIR"

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "[backup] FATAL: DATABASE_URL not set"
    exit 1
fi

echo "[backup] $(date -u) starting pg_dump → ${OUT}"
pg_dump --no-owner --no-acl --format=plain "$DATABASE_URL" | gzip -9 > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "[backup] dump complete: ${SIZE}"

# Upload to Oracle Object Storage (skip if oci CLI not configured)
if command -v oci >/dev/null 2>&1 && [[ -n "${OCI_NAMESPACE:-}" && -n "${OCI_BUCKET:-}" ]]; then
    echo "[backup] uploading to oci os ${OCI_NAMESPACE}/${OCI_BUCKET}"
    oci os object put \
        --namespace "$OCI_NAMESPACE" \
        --bucket-name "$OCI_BUCKET" \
        --file "$OUT" \
        --name "backups/$(basename "$OUT")" \
        --force \
        --no-overwrite 2>&1 | tail -3 || echo "[backup] WARN upload failed (kept local)"
else
    echo "[backup] oci CLI not configured — backup kept local only"
fi

# Prune local files older than RETENTION_DAYS
echo "[backup] pruning local files older than ${RETENTION_DAYS}d"
find "$BACKUP_LOCAL_DIR" -name 'ae-*.sql.gz' -type f -mtime "+${RETENTION_DAYS}" -delete -print

echo "[backup] $(date -u) done"
