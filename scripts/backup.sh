#!/bin/bash
# Automated PostgreSQL Backup Script for Oracle Cloud
# This script backs up the PostgreSQL database to Oracle Object Storage

set -e

# Configuration
BACKUP_DIR="/tmp/backups"
BACKUP_RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="lawyer_bot_backup_${TIMESTAMP}.sql.gz"
LOG_FILE="/var/log/lawyer_bot_backup.log"

# Database configuration (from environment)
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-lawyer_bot}"
DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="${POSTGRES_PASSWORD}"

# Oracle Object Storage configuration
OCI_BUCKET="${OCI_BUCKET:-lawyer-bot-backups}"
OCI_NAMESPACE="${OCI_NAMESPACE:-your-namespace}"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Log function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Check if OCI CLI is installed
if ! command -v oci &> /dev/null; then
    log "ERROR: OCI CLI not installed. Install it with: curl -sL https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh | bash"
    exit 1
fi

# Check if database is accessible
log "Checking database connection..."
if ! docker exec lawyer_bot_postgres pg_isready -U "$DB_USER" -d "$DB_NAME" > /dev/null 2>&1; then
    log "ERROR: Database is not accessible"
    exit 1
fi

# Perform backup
log "Starting backup of database: $DB_NAME"
log "Backup file: $BACKUP_FILE"

# Create backup using pg_dump
docker exec lawyer_bot_postgres pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "$BACKUP_DIR/$BACKUP_FILE"

# Check if backup was successful
if [ ! -f "$BACKUP_DIR/$BACKUP_FILE" ]; then
    log "ERROR: Backup file was not created"
    exit 1
fi

# Get backup file size
BACKUP_SIZE=$(du -h "$BACKUP_DIR/$BACKUP_FILE" | cut -f1)
log "Backup completed successfully. Size: $BACKUP_SIZE"

# Upload to Oracle Object Storage
log "Uploading backup to Oracle Object Storage: $OCI_BUCKET"

if oci os object put --bucket-name "$OCI_BUCKET" --namespace "$OCI_NAMESPACE" --name "$BACKUP_FILE" --file "$BACKUP_DIR/$BACKUP_FILE" >> "$LOG_FILE" 2>&1; then
    log "Backup uploaded successfully to Object Storage"
else
    log "ERROR: Failed to upload backup to Object Storage"
    exit 1
fi

# Clean up local backup file
rm "$BACKUP_DIR/$BACKUP_FILE"
log "Local backup file removed"

# Clean up old backups (retention policy)
log "Cleaning up backups older than $BACKUP_RETENTION_DAYS days"

oci os object list --bucket-name "$OCI_BUCKET" --namespace "$OCI_NAMESPACE" | \
    grep -oP '"name": "\K[^"]+' | \
    grep "lawyer_bot_backup" | \
    while read -r old_backup; do
        backup_date=$(echo "$old_backup" | grep -oP '\d{8}_\d{6}' | head -1)
        if [ -n "$backup_date" ]; then
            backup_timestamp=$(date -d "${backup_date:0:8} ${backup_date:9:2}:${backup_date:11:2}:${backup_date:13:2}" +%s 2>/dev/null || echo "0")
            current_timestamp=$(date +%s)
            age_days=$(( (current_timestamp - backup_timestamp) / 86400 ))
            
            if [ "$age_days" -gt "$BACKUP_RETENTION_DAYS" ]; then
                log "Deleting old backup: $old_backup (age: $age_days days)"
                oci os object delete --bucket-name "$OCI_BUCKET" --namespace "$OCI_NAMESPACE" --name "$old_backup" --force >> "$LOG_FILE" 2>&1
            fi
        fi
    done

log "Backup process completed successfully"

# Send notification (optional - configure your preferred notification method)
# You can add email, Slack, or Telegram notifications here
# Example: curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage?chat_id=$ADMIN_CHAT_ID&text=Backup completed successfully"

exit 0
