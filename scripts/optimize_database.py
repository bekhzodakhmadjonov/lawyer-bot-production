#!/usr/bin/env python3
"""
Database optimization script for lawyer-bot-production.
Adds composite indexes and optimizes database performance.
"""

import asyncio
from sqlalchemy import text
from infrastructure.persistence.database import create_engine, create_session_factory
from config.settings import Settings


async def optimize_database():
    """Add composite indexes and optimize database performance."""
    settings = Settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Composite indexes for common query patterns
        optimizations = [
            # Index for user conversations with status
            "CREATE INDEX IF NOT EXISTS idx_conversations_user_status ON conversations(user_id, status)",
            
            # Index for recent messages per conversation
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation_time ON messages(conversation_id, sent_at DESC)",
            
            # Index for leads by status and update time
            "CREATE INDEX IF NOT EXISTS idx_leads_status_updated ON leads(status, last_updated_at DESC)",
            
            # Index for admin notifications by type and time
            "CREATE INDEX IF NOT EXISTS idx_admin_notifications_type_time ON admin_notifications(notification_type, sent_at DESC)",
            
            # Index for rate limiting queries
            "CREATE INDEX IF NOT EXISTS idx_rate_limits_reset ON rate_limits(last_reset)",
            
            # Analyze tables for query optimization
            "ANALYZE users",
            "ANALYZE conversations", 
            "ANALYZE messages",
            "ANALYZE leads",
            "ANALYZE rate_limits",
            "ANALYZE admin_notifications",
        ]

        for sql in optimizations:
            try:
                await session.execute(text(sql))
                print(f"✓ Executed: {sql[:50]}...")
            except Exception as e:
                print(f"✗ Failed: {sql[:50]}... - {e}")

        await session.commit()
        print("\nDatabase optimization completed!")


if __name__ == "__main__":
    asyncio.run(optimize_database())
