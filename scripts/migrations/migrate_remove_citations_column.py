"""
Migration script to remove citations column from messages table.

Run:
    python scripts/migrations/migrate_remove_citations_column.py
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
        # Check if citations column exists
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in cursor.fetchall()}

        if "citations" not in columns:
            print("citations column does not exist. Skipping migration.")
            return

        # SQLite doesn't support DROP COLUMN directly in older versions
        # We need to recreate the table without the citations column
        print("Removing citations column from messages table...")

        # Create new table without citations
        cursor.execute("""
            CREATE TABLE messages_new (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)

        # Copy data from old table to new table
        cursor.execute("""
            INSERT INTO messages_new (id, conversation_id, sender, text, sent_at)
            SELECT id, conversation_id, sender, text, sent_at FROM messages
        """)

        # Drop old table
        cursor.execute("DROP TABLE messages")

        # Rename new table to original name
        cursor.execute("ALTER TABLE messages_new RENAME TO messages")

        # Recreate indexes
        cursor.execute(
            "CREATE INDEX ix_messages_conversation_id ON messages (conversation_id)"
        )
        cursor.execute("CREATE INDEX ix_messages_sent_at ON messages (sent_at)")

        conn.commit()
        print("Successfully removed citations column from messages table.")


if __name__ == "__main__":
    main()
