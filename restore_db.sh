#!/bin/bash
set -e

# Restore script for bot_remna SQLite database
# Usage: ./restore_db.sh <path_to_backup.zip or bot_database.db>

if [ -z "$1" ]; then
    echo "Usage: $0 <path_to_backup.zip or bot_database.db>"
    exit 1
fi

BACKUP_PATH="$1"

if [ ! -f "$BACKUP_PATH" ]; then
    echo "Error: Backup file '$BACKUP_PATH' not found."
    exit 1
fi

# Create a temporary directory for extraction
TEMP_DIR=$(mktemp -d)
# Automatically clean up temp directory on exit
trap 'rm -rf "$TEMP_DIR"' EXIT

DB_FILE=""

# Check if the backup is a zip or a db file
if [[ "$BACKUP_PATH" == *.zip ]]; then
    echo "Extracting database from zip backup..."
    if command -v unzip >/dev/null 2>&1; then
        unzip -q "$BACKUP_PATH" -d "$TEMP_DIR"
    else
        python3 -m zipfile -e "$BACKUP_PATH" "$TEMP_DIR"
    fi
    
    # Locate the database file inside the extracted zip
    if [ -f "$TEMP_DIR/bot_database.db" ]; then
        DB_FILE="$TEMP_DIR/bot_database.db"
    elif [ -f "$TEMP_DIR/data/bot_database.db" ]; then
        DB_FILE="$TEMP_DIR/data/bot_database.db"
    else
        # Fallback to search recursively for any .db file
        DB_FILE=$(find "$TEMP_DIR" -name "*.db" | head -n 1)
    fi
elif [[ "$BACKUP_PATH" == *.db ]]; then
    DB_FILE="$BACKUP_PATH"
fi

if [ -z "$DB_FILE" ] || [ ! -f "$DB_FILE" ]; then
    echo "Error: Could not find SQLite database file in the provided backup."
    exit 1
fi

echo "Found database file: $DB_FILE"

# Resolve volume directory dynamically
echo "Locating docker volume mountpoint..."
VOLUME_DIR=$(docker volume inspect bot_remna_bot_data --format '{{.Mountpoint}}' 2>/dev/null || true)

if [ -z "$VOLUME_DIR" ]; then
    # Fallback to default compose project volume path if docker inspect failed
    VOLUME_DIR="/var/lib/containers/storage/volumes/bot_remna_bot_data/_data"
fi

echo "Volume mountpoint located at: $VOLUME_DIR"

if [ ! -d "$VOLUME_DIR" ]; then
    echo "Error: Volume directory '$VOLUME_DIR' does not exist."
    exit 1
fi

# Stop the bot container to prevent writes and file locks
echo "Stopping bot-remna container..."
docker compose stop bot || docker stop bot-remna

# Backup current DB just in case before overwriting
CURRENT_DB="$VOLUME_DIR/bot_database.db"
if [ -f "$CURRENT_DB" ]; then
    BACKUP_BEFORE_RESTORE="$VOLUME_DIR/bot_database.db.bak.$(date +%Y%m%d%H%M%S)"
    echo "Backing up current database to $BACKUP_BEFORE_RESTORE..."
    cp "$CURRENT_DB" "$BACKUP_BEFORE_RESTORE"
fi

# Copy the database into the volume
echo "Restoring database to volume..."
cp "$DB_FILE" "$CURRENT_DB"
chmod 644 "$CURRENT_DB"

# Start the bot container again
echo "Starting bot-remna container..."
docker compose start bot || docker start bot-remna

echo "✅ Database successfully restored!"
