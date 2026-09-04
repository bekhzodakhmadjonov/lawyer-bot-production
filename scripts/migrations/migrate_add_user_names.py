"""
Migration script to add first_name and last_name columns to users table.
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path to import modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import text
from src.infrastructure.persistence.database import (
    create_engine,
    create_session_factory,
)
from src.config.settings import Settings


async def migrate():
    """Add first_name and last_name columns to users table."""
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Check if columns already exist
        check_sql = text("PRAGMA table_info(users)")
        result = await session.execute(check_sql)
        columns = [row[1] for row in result.fetchall()]

        if "first_name" in columns and "last_name" in columns:
            print("Columns first_name and last_name already exist. Skipping migration.")
            return

        # Add columns
        try:
            await session.execute(
                text("ALTER TABLE users ADD COLUMN first_name VARCHAR(255)")
            )
            print("Added first_name column")
        except Exception as e:
            if "duplicate column name" not in str(e).lower():
                print(f"Error adding first_name column: {e}")

        try:
            await session.execute(
                text("ALTER TABLE users ADD COLUMN last_name VARCHAR(255)")
            )
            print("Added last_name column")
        except Exception as e:
            if "duplicate column name" not in str(e).lower():
                print(f"Error adding last_name column: {e}")

        await session.commit()
        print("Migration completed successfully")


if __name__ == "__main__":
    asyncio.run(migrate())
