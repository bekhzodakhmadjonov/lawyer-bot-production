"""
Add lead status column to existing SQLite databases.

Run:
    python migrate_add_lead_status.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("data/lawyer_bot.db")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Run python init_db.py first.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()
        }
        if "status" not in columns:
            conn.execute("ALTER TABLE leads ADD COLUMN status TEXT DEFAULT 'new'")
            conn.execute("UPDATE leads SET status = 'new' WHERE status IS NULL")
            conn.commit()
            print("Added leads.status column.")
        else:
            print("leads.status already exists.")


if __name__ == "__main__":
    main()
