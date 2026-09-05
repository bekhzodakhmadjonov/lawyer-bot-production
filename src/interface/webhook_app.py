"""
webhook_app.py — FastAPI webhook endpoint va application lifespan.

Bu fayl BUTUN TIZIMNI birlashtirib, ishga tushiradi:
  1. Startup: Settings yuklash, Container/Bot/DB yaratish, webhook o'rnatish.
  2. Webhook: Telegram update'larni qabul qilib Dispatcher'ga uzatish.
  3. Health: Monitoring uchun health check endpoint.
  4. Shutdown: Barcha resurslarni (DB, AI client'lar) to'g'ri yopish.

MUHIM: Bu fayl BIROR BIZNES LOGIKA o'z ichiga olmaydi — faqat
infrastruktura "simlarini" ulaydi.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import structlog
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    Update,
)
from fastapi import FastAPI, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy import text

from config.container import Container
from config.settings import Settings
from infrastructure.notifications.telegram_admin_notifier import TelegramAdminNotifier
from infrastructure.persistence.database import create_engine, create_session_factory
from infrastructure.persistence.sqlite_conversation_repo import SQLiteConversationRepo
from infrastructure.persistence.sqlite_lead_repo import SQLiteLeadRepo
from infrastructure.persistence.sqlite_notification_registry import (
    SQLiteNotificationRegistry,
)
from infrastructure.persistence.redis_rate_limiter import RedisRateLimiter
from infrastructure.persistence.sqlite_rate_limiter import SQLiteRateLimiter
from infrastructure.persistence.sqlite_user_repo import SQLiteUserRepo
from infrastructure.telegram.aiogram_bot import create_bot, create_dispatcher

logger = structlog.get_logger()


def setup_file_logging() -> None:
    """Configure file-based logging with rotation."""
    # Create logs directory in data volume if it doesn't exist
    import os

    log_dir = "/app/data/logs"
    try:
        os.makedirs(log_dir, exist_ok=True)

        # Configure standard logging with file handler and rotation
        file_handler = RotatingFileHandler(
            f"{log_dir}/lawyer_bot.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
        )
        file_handler.setLevel(logging.INFO)

        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
    except PermissionError:
        # If we can't write to file, just use console logging
        pass

    # Console handler (always enabled)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.addHandler(console_handler)

    # Configure structlog to use standard logging
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# Setup file logging on module import
setup_file_logging()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Application startup va shutdown lifecycle."""
    # ── Startup ──
    settings = Settings()  # type: ignore[call-arg]
    logger.info("Starting application", environment=settings.environment.value)

    # Infra clients
    engine: AsyncEngine = create_engine(settings)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)

    # AI Container
    container = Container(settings)

    # Telegram Bot va Dispatcher
    bot: Bot = create_bot(settings)
    dp: Dispatcher = create_dispatcher()

    # Bot commands ro'yxatini o'rnatish
    # User commands (private chats only)
    user_commands = [
        BotCommand(command="start", description="Botni boshlash"),
        BotCommand(command="help", description="Yordam"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    logger.info("User commands registered", commands=[c.command for c in user_commands])

    # Admin commands (lawyer group only)
    if settings.telegram_lead_chat_id:
        admin_commands = [
            BotCommand(command="stats", description="Statistika"),
            BotCommand(command="leads", description="Leadlar ro'yxati"),
            BotCommand(command="users", description="Foydalanuvchilar ro'yxati"),
            BotCommand(command="close", description="Suhbatni yopish"),
        ]
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=settings.telegram_lead_chat_id),
        )
        logger.info(
            "Admin commands registered", commands=[c.command for c in admin_commands]
        )

    # Per-request dependency'lar uchun factory — handler'lar shu orqali oladi
    async def _build_request_deps(session: AsyncSession) -> dict:
        """Har bir xabar uchun yangi session-scoped dependency'lar."""
        conversation_repo = SQLiteConversationRepo(session)
        lead_repo = SQLiteLeadRepo(session)
        user_repo = SQLiteUserRepo(session)
        notification_registry = SQLiteNotificationRegistry(session)

        # Use Redis rate limiter if available, otherwise fallback to SQLite
        try:
            redis_client = await container.redis_client
            rate_limiter = RedisRateLimiter(redis_client)
        except Exception:
            # Fallback to SQLite if Redis is not available
            rate_limiter = SQLiteRateLimiter(session)

        # Notifier per-request: session-aware registry bilan yaratiladi
        per_request_notifier = TelegramAdminNotifier(
            bot=bot,
            settings=settings,
            notification_registry=notification_registry,
            conversation_repo=conversation_repo,
        )

        handle_message = container.build_handle_user_message_use_case(
            conversation_repo=conversation_repo,
            rate_limiter=rate_limiter,
            notifier=per_request_notifier,
            lead_repo=lead_repo,
        )

        return {
            "handle_message": handle_message,
            "conversation_repo": conversation_repo,
            "lead_repo": lead_repo,
            "user_repo": user_repo,
            "notifier": per_request_notifier,
            "notification_registry": notification_registry,
        }

    # Dispatcher workflow_data ga asosiy dependency'larni qo'shish
    dp.workflow_data.update(
        {
            "settings": settings,
            "session_factory": session_factory,
            "build_request_deps": _build_request_deps,
            "user_repo": None,  # Will be set per-request in middleware
        }
    )

    # Application state ga saqlash (webhook handler uchun)
    application.state.bot = bot
    application.state.dp = dp
    application.state.settings = settings
    application.state.container = container

    # Webhook o'rnatish
    webhook_url = f"{settings.telegram_webhook_url}/webhook"

    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret,
            drop_pending_updates=True,
        )
        logger.info("Webhook set", url=webhook_url)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to set webhook — running without it (dev mode)",
            url=webhook_url,
        )

    yield

    # ── Shutdown ──
    logger.info("Shutting down application")
    try:
        await bot.delete_webhook()
    except Exception:  # noqa: BLE001
        logger.debug("Failed to delete webhook during shutdown")

    await container.aclose()
    await engine.dispose()
    await bot.session.close()
    logger.info("Application shutdown complete")


app = FastAPI(
    title="Lawyer Bot",
    description="Advokat jamoasi uchun AI lead intake va Telegram handoff bot",
    lifespan=lifespan,
)


@app.post("/webhook")
async def webhook_handler(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Telegram update'larni qabul qilish va Dispatcher'ga uzatish."""
    settings: Settings = request.app.state.settings
    bot: Bot = request.app.state.bot
    dp: Dispatcher = request.app.state.dp

    # Xavfsizlik: secret token tekshiruvi
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid secret token",
        )

    # Update parse qilish
    body = await request.json()
    update = Update.model_validate(body, context={"bot": bot})

    # Dispatcher'ga uzatish (per-request session bilan)
    session_factory = dp.workflow_data["session_factory"]
    build_request_deps = dp.workflow_data["build_request_deps"]

    async with session_factory() as session:
        try:
            request_deps = await build_request_deps(session)
            # Add user_repo to workflow_data for callback handlers
            dp.workflow_data["user_repo"] = request_deps["user_repo"]
            await dp.feed_update(bot=bot, update=update, **request_deps)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Error processing update")
            raise

    return {"ok": True}


@app.get("/health")
async def health_check(
    request: Request,
) -> dict:
    """Sog'lik tekshiruvi — monitoring uchun."""
    settings: Settings = request.app.state.settings
    bot: Bot = request.app.state.bot

    # Check database connectivity
    db_healthy = True
    db_latency_ms = 0
    try:
        import time
        session_factory = request.app.state.dp.workflow_data["session_factory"]
        start_time = time.time()
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        db_latency_ms = int((time.time() - start_time) * 1000)
    except Exception as exc:
        db_healthy = False
        db_latency_ms = -1
        logger.error("Database health check failed", error=str(exc))

    # Check bot connectivity
    bot_healthy = True
    bot_latency_ms = 0
    try:
        import time
        start_time = time.time()
        await bot.get_me()
        bot_latency_ms = int((time.time() - start_time) * 1000)
    except Exception as exc:
        bot_healthy = False
        bot_latency_ms = -1
        logger.error("Bot health check failed", error=str(exc))

    # Check Redis connectivity
    redis_healthy = True
    redis_latency_ms = 0
    try:
        import time
        start_time = time.time()
        redis_client = await request.app.state.container.redis_client
        await redis_client.client.ping()
        redis_latency_ms = int((time.time() - start_time) * 1000)
    except Exception as exc:
        redis_healthy = False
        redis_latency_ms = -1
        logger.error("Redis health check failed", error=str(exc))

    overall_healthy = db_healthy and bot_healthy and redis_healthy

    return {
        "status": "healthy" if overall_healthy else "unhealthy",
        "service": "lawyer-bot",
        "mode": "ai-lead-intake",
        "environment": settings.environment.value,
        "database": {
            "status": "ok" if db_healthy else "error",
            "latency_ms": db_latency_ms,
        },
        "bot": {
            "status": "ok" if bot_healthy else "error",
            "latency_ms": bot_latency_ms,
        },
        "redis": {
            "status": "ok" if redis_healthy else "error",
            "latency_ms": redis_latency_ms,
        },
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    uvicorn.run(
        "interface.webhook_app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
