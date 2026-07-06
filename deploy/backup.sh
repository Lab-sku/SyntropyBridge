#!/bin/bash
# ---------------------------------------------------------------------------
# SQLite online backup for API Zhongzhuan Platform
#
# Uses the .backup command which is safe to run while the application
# is writing to the database (WAL mode).
#
# Install as a cron job:
#   cp deploy/backup.sh /usr/local/bin/api-zhuanzhuan-backup.sh
#   chmod +x /usr/local/bin/api-zhuanzhuan-backup.sh
#   echo "0 3 * * * root /usr/local/bin/api-zhuanzhuan-backup.sh" > /etc/cron.d/api-zhuanzhuan-backup
#
# Restore procedure:
#   1. Stop the application: systemctl stop api-zhuanzhuan
#   2. Decompress the backup: gunzip /var/backups/api-zhuanzhuan/data_YYYYMMDD_HHMMSS.db.gz
#   3. Verify integrity: sqlite3 /var/backups/api-zhuanzhuan/data_YYYYMMDD_HHMMSS.db "PRAGMA integrity_check;"
#   4. Copy to data dir: cp /var/backups/api-zhuanzhuan/data_YYYYMMDD_HHMMSS.db /var/lib/api-zhuanzhuan/data.db
#   5. Remove WAL/SHM if present: rm -f /var/lib/api-zhuanzhuan/data.db-wal /var/lib/api-zhuanzhuan/data.db-shm
#   6. Start the application: systemctl start api-zhuanzhuan
# ---------------------------------------------------------------------------
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/api-zhuanzhuan}"
DB_PATH="${DATABASE_PATH:-/var/lib/api-zhuanzhuan/data.db}"
DATE="$(date +%Y%m%d_%H%M%S)"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

# Offsite backup configuration (optional)
# Leave empty to disable. Set to an rclone remote path to enable, e.g.:
#   RCLONE_REMOTE="s3:api-zhuanzhuan-backups"
#   RCLONE_REMOTE="b2:api-zhuanzhuan-backups"
# Requires `rclone` installed and configured (`rclone config`) on the host.
RCLONE_REMOTE="${RCLONE_REMOTE:-}"

# Verify source database exists
if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found at $DB_PATH" >&2
    exit 1
fi

# Verify sqlite3 is available
if ! command -v sqlite3 &> /dev/null; then
    echo "ERROR: sqlite3 command not found" >&2
    exit 1
fi

# Create backup directory if it does not exist
mkdir -p "$BACKUP_DIR"

BACKUP_FILE="$BACKUP_DIR/data_${DATE}.db"

# Perform online backup (safe while app is running)
echo "[$(date)] Starting backup of $DB_PATH ..."
sqlite3 "$DB_PATH" ".backup \"${BACKUP_FILE}\""

# Verify the backup is valid
if ! sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" > /dev/null 2>&1; then
    echo "ERROR: Backup integrity check failed for $BACKUP_FILE" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Compress
gzip "$BACKUP_FILE"

# Report size
SIZE=$(du -h "${BACKUP_FILE}.gz" | cut -f1)
echo "[$(date)] Backup complete: ${BACKUP_FILE}.gz ($SIZE)"

# Retain only the last N days
DELETED=$(find "$BACKUP_DIR" -name "data_*.db.gz" -mtime "+${RETENTION_DAYS}" -delete -print | wc -l)
if [ "$DELETED" -gt 0 ]; then
    echo "[$(date)] Cleaned up $DELETED backup(s) older than ${RETENTION_DAYS} days"
fi

# Offsite backup (optional - configure RCLONE_REMOTE to enable)
# NOTE: uses `if rclone ...; then` so a failure does not abort the script
# under `set -e` (we want to warn, not exit, on offsite sync failure).
if [ -n "$RCLONE_REMOTE" ] && command -v rclone >/dev/null 2>&1; then
    echo "Syncing backup to $RCLONE_REMOTE ..."
    if rclone copy "$BACKUP_FILE.gz" "$RCLONE_REMOTE/" --quiet; then
        echo "Offsite sync complete"
    else
        echo "WARNING: Offsite sync failed" >&2
    fi
else
    echo "NOTE: RCLONE_REMOTE not set or rclone not installed - skipping offsite sync"
fi
