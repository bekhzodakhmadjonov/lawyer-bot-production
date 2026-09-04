#!/bin/bash
# SQLite database backup script for Lawyer Bot
# Usage: ./backup_db.sh

# Configuration
DB_PATH="/app/data/lawyer_bot.db"
BACKUP_DIR="/app/data/backups"
RETENTION_DAYS=30

# Azure Blob Storage (optional - set these if using Azure)
AZURE_STORAGE_ACCOUNT=""
AZURE_STORAGE_CONTAINER="backups"
AZURE_STORAGE_KEY=""

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Generate backup filename with date
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/lawyer_bot_$DATE.db"

# Backup database
echo "Backing up database to $BACKUP_FILE"
cp "$DB_PATH" "$BACKUP_FILE"

# Compress backup
gzip "$BACKUP_FILE"
BACKUP_FILE="${BACKUP_FILE}.gz"

echo "Backup created: $BACKUP_FILE"

# Upload to Azure Blob Storage if configured
if [ -n "$AZURE_STORAGE_ACCOUNT" ] && [ -n "$AZURE_STORAGE_KEY" ]; then
    echo "Uploading to Azure Blob Storage..."

    # Install az CLI if not present (for Alpine/Docker)
    if ! command -v az &> /dev/null; then
        echo "Azure CLI not found. Installing..."
        apk add --no-cache azure-cli 2>/dev/null || apt-get update && apt-get install -y azure-cli
    fi

    # Login with storage account key
    az storage account show-connection-string \
        --name "$AZURE_STORAGE_ACCOUNT" \
        --query connectionString \
        --output tsv | \
        xargs -I {} az storage blob upload \
            --account-name "$AZURE_STORAGE_ACCOUNT" \
            --account-key "$AZURE_STORAGE_KEY" \
            --container-name "$AZURE_STORAGE_CONTAINER" \
            --name "lawyer_bot_$DATE.db.gz" \
            --file "$BACKUP_FILE"

    echo "Upload completed"
fi

# Delete old backups (local)
echo "Cleaning up old backups (older than $RETENTION_DAYS days)..."
find "$BACKUP_DIR" -name "lawyer_bot_*.db.gz" -mtime +$RETENTION_DAYS -delete

echo "Backup completed successfully"
