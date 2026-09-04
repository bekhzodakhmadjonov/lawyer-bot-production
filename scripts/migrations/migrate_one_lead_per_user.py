"""
Migration script to implement one-lead-per-user architecture.

This script:
1. Adds last_updated_at column to leads table
2. Removes unique constraint from conversation_id
3. Adds unique constraint to user_id
4. Merges duplicate leads (keeps most recent per user)
"""

import sqlite3
from pathlib import Path


def migrate_database(db_path: str = "data/lawyer_bot.db") -> None:
    """Migrate the database to one-lead-per-user architecture."""
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"Database file not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        print("Starting migration to one-lead-per-user architecture...")

        # Step 1: Add last_updated_at column if it doesn't exist
        print("Step 1: Adding last_updated_at column...")
        try:
            cursor.execute("ALTER TABLE leads ADD COLUMN last_updated_at TIMESTAMP")
            print("  - Added last_updated_at column")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("  - Column already exists, skipping")
            else:
                raise

        # Step 2: Set last_updated_at to created_at for existing records
        print("Step 2: Initializing last_updated_at values...")
        cursor.execute(
            "UPDATE leads SET last_updated_at = created_at WHERE last_updated_at IS NULL"
        )
        print(f"  - Updated {cursor.rowcount} records")

        # Step 3: Identify and merge duplicate leads
        print("Step 3: Merging duplicate leads per user...")

        # Get all leads ordered by user_id and created_at
        cursor.execute(
            """
            SELECT id, user_id, conversation_id, created_at
            FROM leads
            ORDER BY user_id, created_at DESC
        """
        )
        all_leads = cursor.fetchall()

        # Track users we've already processed
        processed_users = set()
        leads_to_delete = []

        for lead_id, user_id, conversation_id, created_at in all_leads:
            if user_id in processed_users:
                # This is a duplicate, mark for deletion
                leads_to_delete.append(lead_id)
                print(
                    f"  - Marking duplicate lead {lead_id} for deletion (user: {user_id})"
                )
            else:
                # Keep this lead (most recent for this user)
                processed_users.add(user_id)
                print(f"  - Keeping lead {lead_id} for user {user_id}")

        # Delete duplicate leads
        if leads_to_delete:
            print(f"Step 4: Deleting {len(leads_to_delete)} duplicate leads...")
            for lead_id in leads_to_delete:
                cursor.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
            print(f"  - Deleted {len(leads_to_delete)} duplicate leads")
        else:
            print("Step 4: No duplicate leads found")

        # Step 5: Recreate table with new schema
        print("Step 5: Recreating leads table with new schema...")

        # Get existing data
        cursor.execute(
            """
            SELECT id, conversation_id, user_id, score_value, score_reasons,
                   topic_summary, contact_info, status, created_at, last_updated_at
            FROM leads
        """
        )
        existing_data = cursor.fetchall()

        # Drop old table
        cursor.execute("DROP TABLE leads")
        print("  - Dropped old leads table")

        # Create new table with updated schema
        cursor.execute(
            """
            CREATE TABLE leads (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                user_id TEXT NOT NULL UNIQUE,
                score_value REAL NOT NULL,
                score_reasons TEXT NOT NULL,
                topic_summary TEXT NOT NULL,
                contact_info TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMP NOT NULL,
                last_updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations (id)
            )
        """
        )
        print("  - Created new leads table with user_id unique constraint")

        # Reinsert data
        for row in existing_data:
            cursor.execute(
                """
                INSERT INTO leads
                (id, conversation_id, user_id, score_value, score_reasons,
                 topic_summary, contact_info, status, created_at, last_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        print(f"  - Reinserted {len(existing_data)} leads")

        # Create indexes
        cursor.execute(
            "CREATE INDEX idx_leads_conversation_id ON leads (conversation_id)"
        )
        cursor.execute("CREATE INDEX idx_leads_user_id ON leads (user_id)")
        print("  - Created indexes")

        conn.commit()
        print("\n✅ Migration completed successfully!")
        print(f"   Total leads after migration: {len(existing_data)}")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_database()
