"""
Migration script to populate users table from existing conversations.

This script extracts unique users from the conversations table and
creates corresponding entries in the users table.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.persistence.database import UserModel


def _stable_user_id(telegram_id: int) -> UUID:
    """Bir xil telegram_id har doim bir xil UUID beradi (deterministic)."""
    return uuid5(NAMESPACE_URL, f"telegram:{telegram_id}")


async def main() -> None:
    """Migrate existing users from conversations to users table."""
    db_url = "sqlite+aiosqlite:///data/lawyer_bot.db"
    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    print("Migrating existing users from conversations table...")

    async with session_factory() as session:
        # Get unique users from conversations
        result = await session.execute(
            text(
                "SELECT DISTINCT user_id, user_telegram_id, MIN(created_at) as first_seen FROM conversations WHERE user_telegram_id IS NOT NULL GROUP BY user_id, user_telegram_id"
            )
        )
        existing_users = result.fetchall()

        print(f"Found {len(existing_users)} unique users in conversations table")

        migrated_count = 0
        for user_id, telegram_id, first_seen in existing_users:
            # Convert string UUID to UUID object if needed
            if isinstance(user_id, str):
                user_id = UUID(user_id)

            # Convert datetime string to datetime object if needed
            if isinstance(first_seen, str):
                first_seen = datetime.fromisoformat(first_seen)

            # Check if user already exists
            existing = await session.get(UserModel, user_id)
            if existing is None:
                # Create new user entry
                new_user = UserModel(
                    id=user_id,
                    telegram_id=telegram_id,
                    username=None,  # We don't have username in conversations table
                    has_joined_channel=False,  # Default to False, will be updated on next interaction
                    created_at=first_seen or datetime.now(UTC),
                )
                session.add(new_user)
                migrated_count += 1
                print(f"Migrated user: telegram_id={telegram_id}, user_id={user_id}")

        await session.commit()
        print(f"Successfully migrated {migrated_count} users to users table")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
