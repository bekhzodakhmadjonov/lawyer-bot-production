"""
Database initialization script for SQLite.

Run this script to create the database schema:
    python init_db.py
"""

from __future__ import annotations

import asyncio

from infrastructure.persistence.database import Base, create_engine
from config.settings import Settings


async def main() -> None:
    """Create all database tables."""
    settings = Settings()  # type: ignore[call-arg]
    engine = create_engine(settings)

    print("Creating database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created successfully!")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
