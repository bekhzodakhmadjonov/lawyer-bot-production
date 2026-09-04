"""
Migration script to add message_count column to conversations table.
"""

import asyncio
import sqlite3
from pathlib import Path


async def main() -> None:
    db_path = Path("data/lawyer_bot.db")

    if not db_path.exists():
        print("Database not found. Run init_db.py first.")
        return

    print(f"Migrating database: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(conversations)")
    columns = [row[1] for row in cursor.fetchall()]

    if "message_count" in columns:
        print("Column 'message_count' already exists. Skipping migration.")
        conn.close()
        return

    # Add the column
    cursor.execute(
        "ALTER TABLE conversations ADD COLUMN message_count INTEGER DEFAULT 0"
    )
    conn.commit()

    print("Migration completed successfully!")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
