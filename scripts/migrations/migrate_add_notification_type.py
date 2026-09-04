"""
Migration script to add notification_type column to admin_notifications table.

This migration adds the notification_type column to support re-rendering
notifications with updated chat history visibility.
"""

import sqlite3
from pathlib import Path


def migrate():
    """Add notification_type column to admin_notifications table."""
    db_path = Path("data/lawyer_bot.db")

    if not db_path.exists():
        print(f"❌ Database file not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(admin_notifications)")
    columns = [row[1] for row in cursor.fetchall()]

    if "notification_type" in columns:
        print("✅ notification_type column already exists in admin_notifications table")
        conn.close()
        return

    # Add the column
    try:
        cursor.execute(
            "ALTER TABLE admin_notifications ADD COLUMN notification_type VARCHAR(32)"
        )

        # Create index for faster lookups
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_admin_notifications_notification_type "
            "ON admin_notifications (notification_type)"
        )

        conn.commit()
        print(
            "✅ Successfully added notification_type column to admin_notifications table"
        )
        print("✅ Created index on notification_type column")
    except Exception as e:
        print(f"❌ Error during migration: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
