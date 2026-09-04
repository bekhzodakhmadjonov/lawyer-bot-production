"""
Database migration script to add users table.

Run this script to add the users table to the existing database:
    python migrate_add_users_table.py
"""

from __future__ import annotations

import asyncio

from infrastructure.persistence.database import Base, create_engine
from config.settings import Settings


async def main() -> None:
    """Add users table to existing database."""
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings)

    print("Adding users table to database...")
    async with engine.begin() as conn:
        # Only create the users table (not all tables)
        await conn.run_sync(Base.metadata.tables["users"].create)
    print("Users table added successfully!")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
