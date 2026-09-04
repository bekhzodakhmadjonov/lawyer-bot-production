"""
Migration script to add conversation_id column to admin_notifications table.

This migration adds the conversation_id column to support chat history
toggle functionality in notifications.

Usage: python scripts/migrations/migrate_add_conversation_id_to_notifications.py
"""

import sqlite3
from pathlib import Path


def migrate():
    """Add conversation_id column to admin_notifications table."""
    db_path = Path("data/lawyer_bot.db")

    if not db_path.exists():
        print(f"❌ Database file not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(admin_notifications)")
    columns = [row[1] for row in cursor.fetchall()]

    if "conversation_id" in columns:
        print("✅ conversation_id column already exists in admin_notifications table")
        conn.close()
        return

    # Add the column
    try:
        cursor.execute(
            "ALTER TABLE admin_notifications ADD COLUMN conversation_id VARCHAR(36)"
        )

        # Create index for faster lookups
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_admin_notifications_conversation_id "
            "ON admin_notifications (conversation_id)"
        )

        conn.commit()
        print(
            "✅ Successfully added conversation_id column to admin_notifications table"
        )
        print("✅ Created index on conversation_id column")
    except Exception as e:
        print(f"❌ Error during migration: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
