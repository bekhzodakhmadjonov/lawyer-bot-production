"""
Migration script to add rate limit columns to rate_limits table.

Adds:
- recent_timestamps (TEXT, default "[]") for burst detection
- violation_count (INTEGER, default 0) for progressive penalties
- last_violation_time (DATETIME, nullable) for violation tracking

Run:
    python scripts/migrations/migrate_add_rate_limit_columns.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("data/lawyer_bot.db")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Run python init_db.py first.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # Check existing columns
        cursor.execute("PRAGMA table_info(rate_limits)")
        columns = {row[1] for row in cursor.fetchall()}

        # Add recent_timestamps column
        if "recent_timestamps" not in columns:
            cursor.execute(
                "ALTER TABLE rate_limits ADD COLUMN recent_timestamps TEXT DEFAULT '[]'"
            )
            print("Added rate_limits.recent_timestamps column.")
        else:
            print("rate_limits.recent_timestamps already exists.")

        # Add violation_count column
        if "violation_count" not in columns:
            cursor.execute(
                "ALTER TABLE rate_limits ADD COLUMN violation_count INTEGER DEFAULT 0"
            )
            print("Added rate_limits.violation_count column.")
        else:
            print("rate_limits.violation_count already exists.")

        # Add last_violation_time column
        if "last_violation_time" not in columns:
            cursor.execute(
                "ALTER TABLE rate_limits ADD COLUMN last_violation_time DATETIME"
            )
            print("Added rate_limits.last_violation_time column.")
        else:
            print("rate_limits.last_violation_time already exists.")

        conn.commit()
        print("Migration completed successfully!")


if __name__ == "__main__":
    main()
