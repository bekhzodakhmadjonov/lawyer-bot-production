"""
Telegram message handlers — Aiogram Router.

Bu fayldagi handler'lar HECH QANDAY BIZNES LOGIKA o'z ichiga olmaydi.
Ular faqat:
  1. Telegram xabarini domain obyektiga aylantiradi.
  2. Tegishli use case ni chaqiradi.
  3. Natijani Telegram javobiga aylantiradi.

Dependency'lar dispatcher workflow_data orqali inject qilinadi
(webhook_app.py da o'rnatiladi).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import async_sessionmaker

import structlog
from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from application.use_cases.conversation.handle_user_message import (
    HandledUserMessageUseCase,
)
from config.settings import Settings
from domain.entities import User
from domain.exceptions import (
    ChannelMembershipRequiredError,
    RateLimitExceededError,
)
from domain.value_objects import ConversationStatus, LeadStatus, MessageSender
from infrastructure.notifications.telegram_admin_notifier import TelegramAdminNotifier
from infrastructure.persistence.sqlite_conversation_repo import SQLiteConversationRepo
from infrastructure.persistence.sqlite_lead_repo import SQLiteLeadRepo
from infrastructure.persistence.sqlite_notification_registry import (
    SQLiteNotificationRegistry,
)
from infrastructure.persistence.sqlite_user_repo import SQLiteUserRepo

logger = structlog.get_logger()
router = Router(name="main_router")

# Security constants for input validation
MAX_MESSAGE_LENGTH = 10000
MAX_USERNAME_LENGTH = 255
MAX_TOPIC_SUMMARY_LENGTH = 5000

# User-friendly error messages (avoid technical details)
USER_FRIENDLY_ERRORS = {
    "Noto'g'ri status tugmasi": "Xatolik yuz berdi",
    "Noto'g'ri callback data": "Xatolik yuz berdi",
    "Lead yoki suhbat topilmadi": "Ma'lumot topilmadi",
    "Lead topilmadi": "Ma'lumot topilmadi",
    "Leadlar yo'q": "Ma'lumot yo'q",
    "Faol suhbat topilmadi": "Suhbat topilmadi",
}


def _mask_telegram_id(telegram_id: int) -> str:
    """Mask Telegram ID for logging (show only first 3 and last 3 digits)."""
    id_str = str(telegram_id)
    if len(id_str) <= 6:
        return "***"
    return f"{id_str[:3]}***{id_str[-3:]}"


def _mask_message(text: str) -> str:
    """Mask sensitive content in messages for logging."""
    if len(text) > 50:
        return f"{text[:20]}...{text[-20:]}"
    return text


# ────────────────────── Yordamchi funksiyalar ──────────────────────


async def _safe_answer(
    message: types.Message,
    text: str,
    **kwargs,
) -> None:
    """HTML bilan xabar yuboradi; HTML xato bo'lsa teg'siz qayta yuboradi.

    GPT javobi Telegram HTML'ni qabul qilmagan holda (masalan, yopilmagan
    teg) TelegramBadRequest xatoligi kelib chiqadi. Bu holda teglarni olib
    tashlab oddiy matn sifatida yuboramiz, foydalanuvchi hech bo'lmaganda
    javobni ko'radi.
    """
    try:
        await message.answer(text, parse_mode="HTML", **kwargs)
    except TelegramBadRequest as exc:
        exc_str = str(exc).lower()
        if "can't parse entities" in exc_str or "parse entities" in exc_str:
            plain = re.sub(r"<[^>]+>", "", text).strip()
            if not plain:
                plain = "⚠️ Javob tayyorlandi, lekin formatlashda xatolik bo'ldi."
            logger.warning(
                "HTML parse error — sending as plain text",
                error=str(exc),
                text_preview=text[:200],  # diagnoz uchun
            )
            await message.answer(plain, parse_mode=None, **kwargs)
        else:
            raise


async def _safe_edit_message(
    message: types.Message,
    text: str,
    **kwargs,
) -> None:
    """Xabarni xavfsiz tarzda tahrirlaydi; xatolik bo'lsa log qiladi.

    TelegramBadRequest "message is not modified" xatoligini tutib oladi
    va foydalanuvchi tajribasini buzmaydi.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        exc_str = str(exc).lower()
        if "message is not modified" in exc_str or "message not modified" in exc_str:
            logger.debug(
                "Message edit skipped - content unchanged",
                error=str(exc),
            )
        else:
            logger.warning(
                "Failed to edit message",
                error=str(exc),
                text_preview=text[:200],
            )
            raise


def _stable_user_id(telegram_id: int) -> UUID:
    """Bir xil telegram_id har doim bir xil UUID beradi (deterministic)."""
    return uuid5(NAMESPACE_URL, f"telegram:{telegram_id}")


def _resolve_user(tg_user: types.User, *, has_joined_channel: bool) -> User:
    """Telegram foydalanuvchisini domain User entity'ga aylantiradi."""
    return User(
        id=_stable_user_id(tg_user.id),
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        has_joined_channel=has_joined_channel,
    )


def _actual_name(user: User) -> str:
    """Foydalanuvchining haqiqy ismini qaytaradi (first_name + last_name)."""
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    elif user.first_name:
        return user.first_name
    elif user.last_name:
        return user.last_name
    else:
        return "Noma'lum"


def _validate_callback_data(data: str, expected_parts: int) -> list[str] | None:
    """Validate and parse callback data safely."""
    parts = data.split(":")
    if len(parts) != expected_parts:
        return None
    return parts


def _validate_uuid(uuid_string: str) -> bool:
    """Validate UUID format."""
    try:
        UUID(uuid_string)
        return True
    except ValueError:
        return False


def _validate_page_number(page_str: str) -> int | None:
    """Validate page number is within safe range."""
    try:
        page = int(page_str)
        if 0 <= page <= 1000:  # Reasonable upper limit
            return page
    except ValueError:
        pass
    return None


def _validate_filter_value(filter_str: str) -> str | None:
    """Validate filter value against allowed values."""
    allowed_filters = {"all", "ochiq", "yangi", "yopiq"}
    if filter_str in allowed_filters:
        return filter_str
    return None


async def _check_channel_membership(bot: Bot, user_id: int, channel_id: int) -> bool:
    """Foydalanuvchi kanalga a'zo ekanligini Telegram API orqali tekshiradi."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:  # noqa: BLE001
        return False


def _channel_join_keyboard(channel_username: str) -> InlineKeyboardMarkup:
    """Kanalga a'zo bo'lish va tekshirish tugmalari (ustma-ust)."""
    clean_username = channel_username.lstrip("@")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Kanalga a'zo bo'lish",
                    url=f"https://t.me/{clean_username}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Qo'shildim",
                    callback_data="check_channel",
                ),
            ],
        ]
    )


def _return_to_ai_keyboard() -> InlineKeyboardMarkup:
    """Foydalanuvchi uchun AI ga qaytish tugmasi."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 AI ga qaytish",
                    callback_data="return_to_ai",
                ),
            ],
        ]
    )


def _greeting_message() -> str:
    """Compact welcome text for subscribed users."""
    return (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "Men — <b>Advokat Jasurbek</b> jamoasining AI-yordamchisiman ⚖️\n"
        "Vaziyatingizni qisqa aniqlab, zarur ma'lumotlarni yig'aman va kerak bo'lsa "
        "pullik konsultatsiya uchun jamoaga yo'naltiraman.\n\n"
        "💬 <b>Masalan, shunday yozishingiz mumkin:</b>\n"
        "• Ishdan bo'shatishdi, hujjatlarim bor\n"
        "• Aliment undirish bo'yicha advokat kerak\n"
        "• Shartnoma bo'yicha nizo chiqdi\n"
        "• Sud qaroridan norozi bo'ldim\n"
        "• Biznesim uchun shartnoma tayyorlatmoqchiman\n\n"
        "📌 <i>Vaziyatni 2-3 gapda yozing: nima bo'ldi, qachon bo'ldi, "
        "qo'lingizda qanday hujjat bor.</i>\n\n"
        "👇 <b>Savolingizni yozing:</b>"
    )


def _subscription_required_message() -> str:
    """Welcome text shown before channel subscription."""
    return (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "Men — <b>Advokat Jasurbek</b> jamoasining AI-yordamchisiman ⚖️\n"
        "Bot orqali vaziyatingizni qisqa bayon qilib, zarur bo'lsa advokat "
        "jamoasi bilan pullik konsultatsiyaga yo'nalishingiz mumkin.\n\n"
        "📢 <b>Davom etish uchun kanalga a'zo bo'ling.</b>\n"
        "Kanalda foydali huquqiy maslahatlar, qonunchilik yangiliklari va "
        "amaliy tavsiyalar berib boriladi.\n\n"
        "A'zo bo'lgach, <b>✅ Qo'shildim</b> tugmasini bosing."
    )


def _subscription_confirmed_message() -> str:
    """Message shown after the channel membership check succeeds."""
    return (
        "✅ <b>A'zolik tasdiqlandi!</b>\n\n"
        "Endi vaziyatingizni yuborishingiz mumkin. Men kerakli ma'lumotlarni "
        "aniqlab, mos bo'lsa Jasurbek advokat jamoasiga yo'naltiraman.\n\n"
        "💬 <b>Namuna savollar:</b>\n"
        "• Ish beruvchim oylik bermayapti, hujjatlarim bor\n"
        "• Ajrashish va aliment bo'yicha advokat kerak\n"
        "• Qarz bo'yicha tilxat bor, Toshkentdaman\n"
        "• Soliq tekshiruvi keldi, muddat qisqa\n"
        "• Apellyatsiya bo'yicha konsultatsiya kerak\n\n"
        "📌 <i>Savolda faktlarni aniq yozing: sana, joy, hujjat, muddat.</i>\n\n"
        "👇 <b>Savolingizni yozing:</b>"
    )


def _is_escalated(status: ConversationStatus) -> bool:
    """Suhbat eskalatsiya holatida ekanligini tekshiradi."""
    return status in (
        ConversationStatus.ESCALATED_LEAD,
        ConversationStatus.ESCALATED_GENERAL,
    )


def _lead_status_label(status: LeadStatus) -> str:
    labels = {
        LeadStatus.NEW: "Yangi",
        LeadStatus.CONTACTED: "Bog'landim",
        LeadStatus.BOOKED: "Belgilandi",
        LeadStatus.PAID: "To'langan",
        LeadStatus.LOST: "Yo'qolgan",
        LeadStatus.CLOSED: "Yopilgan",
    }
    return labels[status]


def _should_close_conversation_for_lead_status(status: LeadStatus) -> bool:
    return status in {LeadStatus.PAID, LeadStatus.LOST, LeadStatus.CLOSED}


def _lead_score_label(score_value: float) -> str:
    """Lead score uchun vizual indikator."""
    if score_value >= 0.7:
        return "⭐ Yuqori"
    elif score_value >= 0.4:
        return "⚡ O'rtacha"
    else:
        return "💡 Past"


def _format_lead_lines(leads: list, *, offset: int) -> list[str]:
    """Bitta joyda leadlar ro'yxati matnini formatlaydi.

    Har bir lead qatoridan keyin "Batafsil: /leads {index}" matni
    qo'shiladi — bu <code> teg orqali Telegram'da bosib nusxalanadi,
    tugma emas, oddiy matn sifatida.
    """
    lines: list[str] = []
    for index, lead in enumerate(leads, start=offset + 1):
        created_at = lead.created_at.strftime("%d %b %H:%M")
        summary = lead.topic_summary
        if len(summary) > 100:
            summary = summary[:97] + "..."
        contact = lead.contact_info or "Aloqa noma'lum"
        if len(contact) > 30:
            contact = contact[:27] + "..."

        # Remove HTML tags from summary and escape remaining special characters
        summary = re.sub(r"<[^>]+>", "", summary)
        summary = (
            summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        contact = (
            contact.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

        lines.append(
            f"<b>{index}. {_lead_status_label(lead.status)}</b> "
            f"• {_lead_score_label(lead.score.value)}\n"
            f"📅 {created_at} • 📞 {contact}\n"
            f"📝 {summary}\n"
            f"📖 Batafsil: <code>/leads {index}</code>\n\n"
        )
    return lines


def _leads_list_keyboard(
    *,
    page: int,
    total_pages: int,
    status_filter: str | None,
    sort_by_score: bool,
) -> InlineKeyboardMarkup:
    """Leadlar ro'yxati uchun navigatsiya/filtr/saralash klaviaturasi.

    Endi har bir lead uchun alohida "Batafsil" tugmasi YO'Q — ular
    matn ichidagi "/leads {index}" ko'rinishiga almashtirildi.
    """
    keyboard: list[list[InlineKeyboardButton]] = []

    # Navigation row
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="◀ Oldingi",
                callback_data=f"leads_page:{page - 1}:{status_filter or 'all'}:{sort_by_score}",
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="leads_page:current",
        )
    )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Keyingi ▶",
                callback_data=f"leads_page:{page + 1}:{status_filter or 'all'}:{sort_by_score}",
            )
        )
    keyboard.append(nav_row)

    # Filter buttons
    filter_row = [
        InlineKeyboardButton(
            text="Hammasi",
            callback_data=f"leads_filter:all:{page}:{sort_by_score}",
        ),
        InlineKeyboardButton(
            text="Ochiq",
            callback_data=f"leads_filter:ochiq:{page}:{sort_by_score}",
        ),
        InlineKeyboardButton(
            text="Yangi",
            callback_data=f"leads_filter:yangi:{page}:{sort_by_score}",
        ),
    ]
    keyboard.append(filter_row)

    # Sort button
    sort_button_text = "⭐ Yuqori" if sort_by_score else "📅 Sana"
    keyboard.append(
        [
            InlineKeyboardButton(
                text=sort_button_text,
                callback_data=f"leads_sort:{status_filter or 'all'}:{page}:{not sort_by_score}",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _format_chat_history(messages: tuple, *, max_length: int = 4000) -> str:
    """Format chat history with timestamps and sender labels for admin view.

    Args:
        messages: Tuple of Message entities from conversation repo
        max_length: Maximum total length to fit Telegram's 4096 char limit

    Returns:
        Formatted chat history string
    """
    if not messages:
        return "(Suhbat tarixi yo'q)"

    lines = ["\n💬 <b>Suhbat tarixi:</b>\n"]

    for msg in messages:
        # Determine sender emoji and label
        if msg.sender == MessageSender.USER:
            sender_label = "👤 Foydalanuvchi"
        elif msg.sender == MessageSender.AI:
            sender_label = "🤖 AI"
        elif msg.sender == MessageSender.ADMIN:
            sender_label = "👨‍💼 Mutaxassis"
        else:
            sender_label = "🔧 Tizim"

        # Format timestamp
        time_str = msg.sent_at.strftime("%H:%M")

        # Truncate message if too long
        text = msg.text
        if len(text) > 300:
            text = text[:297] + "..."

        # Escape HTML special characters to prevent parsing errors
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")

        lines.append(f"{sender_label} [{time_str}]:\n{text}\n")

    # Join and truncate if still too long
    result = "\n".join(lines)
    if len(result) > max_length:
        result = result[: max_length - 20] + "\n\n...(tarix qisqartirildi)"

    return result


def _leads_list_header(
    *,
    status_filter: str | None,
    sort_by_score: bool,
    page: int,
    total_pages: int,
    total_count: int,
) -> list[str]:
    lines = ["📋 <b>Leadlar ro'yxati</b>"]
    if status_filter:
        lines.append(f"🔍 Filtr: {status_filter.capitalize()}")
    if sort_by_score:
        lines.append("⭐ Saralash: Yuqori ball")
    lines.append(f"\n📊 Sahifa: {page + 1}/{total_pages} (Jami: {total_count})\n")
    return lines


async def _render_leads_list(
    *,
    lead_repo: SQLiteLeadRepo,
    page: int,
    status_filter: str | None,
    sort_by_score: bool,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Leadlar ro'yxati matni va klaviaturasini quradi.

    Leadlar bo'lmasa None qaytaradi.
    """
    limit = 10
    offset = page * limit

    leads = await lead_repo.list_with_pagination(
        offset=offset,
        limit=limit,
        status_filter=status_filter,
        sort_by_score=sort_by_score,
    )
    if not leads:
        return None

    total_count = await lead_repo.count_by_filter(status_filter=status_filter)
    total_pages = (total_count + limit - 1) // limit

    lines = _leads_list_header(
        status_filter=status_filter,
        sort_by_score=sort_by_score,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
    )
    lines.extend(_format_lead_lines(leads, offset=offset))

    keyboard = _leads_list_keyboard(
        page=page,
        total_pages=total_pages,
        status_filter=status_filter,
        sort_by_score=sort_by_score,
    )

    return "\n".join(lines), keyboard


# ────────────────────── /start komandasi ──────────────────────


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(
    message: types.Message,
    settings: Settings,
    conversation_repo: SQLiteConversationRepo,
    user_repo: SQLiteUserRepo,
) -> None:
    """Yangi foydalanuvchini kutib olish va kanalga a'zo bo'lishni so'rash."""
    if message.from_user is None:
        return

    # Foydalanuvchini saqlash
    is_member = await _check_channel_membership(
        message.bot,
        message.from_user.id,
        settings.required_channel_id,  # type: ignore[arg-type]
    )
    user = _resolve_user(message.from_user, has_joined_channel=is_member)
    await user_repo.save(user)

    # Eski ochiq suhbatni yopish — foydalanuvchi boshidan boshlaydi
    user_id = _stable_user_id(message.from_user.id)
    existing = await conversation_repo.get_active_for_user(user_id)
    if existing is not None:
        existing.close()
        await conversation_repo.save(existing)

    if is_member:
        await message.answer(
            _greeting_message(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            _subscription_required_message(),
            reply_markup=_channel_join_keyboard(settings.required_channel_username),
            parse_mode="HTML",
        )


# ────────────────────── Kanal a'zoligi tekshiruvi ──────────────────────


@router.callback_query(lambda c: c.data == "check_channel")
async def on_check_channel(callback: types.CallbackQuery, settings: Settings) -> None:
    """Foydalanuvchi 'Qo'shildim' tugmasini bosganda."""
    if callback.from_user is None or callback.message is None:
        return

    is_member = await _check_channel_membership(
        callback.bot,
        callback.from_user.id,
        settings.required_channel_id,  # type: ignore[arg-type]
    )

    if is_member:
        await _safe_edit_message(  # type: ignore[union-attr]
            callback.message,
            _subscription_confirmed_message(),
            parse_mode="HTML",
        )
    else:
        await callback.answer(
            "❌ Siz hali kanalga a'zo bo'lmadingiz. Iltimos, avval kanalga a'zo bo'ling.",
            show_alert=True,
        )


# ────────────────────── AI ga qaytish callback ──────────────────────


@router.callback_query(lambda c: c.data == "return_to_ai")
async def on_return_to_ai(
    callback: types.CallbackQuery,
    settings: Settings,
    conversation_repo: SQLiteConversationRepo,
    notifier: TelegramAdminNotifier,
) -> None:
    """Foydalanuvchi 'AI ga qaytish' tugmasini bosganda."""
    if callback.from_user is None or callback.message is None:
        return

    user_id = _stable_user_id(callback.from_user.id)
    conversation = await conversation_repo.get_active_for_user(user_id)

    if conversation is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Faol suhbat topilmadi"], show_alert=True
        )
        return

    if conversation.status == ConversationStatus.AI_HANDLED:
        await callback.answer("Suhbat allaqachon AI rejimida.", show_alert=True)
        return

    # Suhbatni AI rejimiga qaytarish
    conversation.return_to_ai()
    await conversation_repo.save(conversation)

    # Admin guruhiga xabar berish
    await notifier.notify_returned_to_ai(conversation)

    # Foydalanuvchiga tasdiqlash
    await _safe_edit_message(  # type: ignore[union-attr]
        callback.message,
        "🤖 <b>Suhbatingiz AI yordamchiga qaytarildi.</b>\n\n"
        "Endi savolingizni bemalol yozishingiz mumkin.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("toggle_history:"))
async def on_toggle_history_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    conversation_repo: SQLiteConversationRepo,
    lead_repo: SQLiteLeadRepo,
    notification_registry: SQLiteNotificationRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Handle toggle history callback for all notification types."""
    if callback.data is None or callback.message is None:
        return

    if callback.message.chat.id != settings.telegram_lead_chat_id:
        await callback.answer("Unauthorized", show_alert=True)
        return

    # Create user_repo from session
    async with session_factory() as session:
        user_repo = SQLiteUserRepo(session)

        parts = _validate_callback_data(callback.data, 3)
        if parts is None:
            await callback.answer(
                USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
            )
            return

        _, conversation_id_str, show_history_str = parts
        if not _validate_uuid(conversation_id_str):
            await callback.answer(
                USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
            )
            return

        try:
            conversation_id = UUID(conversation_id_str)
            show_history = show_history_str == "1"
        except (ValueError, TypeError):
            await callback.answer(
                USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
            )
            return

        # Try to find if this is a lead detail view by checking message content
        message_text = callback.message.text or ""
        if "Lead #" in message_text:
            # This is a lead detail view - need to re-render with updated history
            # Extract lead number from message
            match = re.search(r"Lead #(\d+)", message_text)
            if not match:
                await callback.answer("Xatolik yuz berdi", show_alert=True)
                return

            lead_number = int(match.group(1))
            rendered = await _render_lead_detail(
                lead_number=lead_number,
                lead_repo=lead_repo,
                conversation_repo=conversation_repo,
                user_repo=user_repo,
                bot=callback.bot,
                settings=settings,
                show_history=show_history,
            )

            if rendered is None:
                await callback.answer("Lead topilmadi", show_alert=True)
                return

            text, keyboard, user_telegram_id, display_name = rendered
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )

            # Update notification registry if needed
            if user_telegram_id:
                await notification_registry.save(
                    message_id=callback.message.message_id,
                    user_telegram_id=user_telegram_id,
                    display_name=display_name,
                )

            await callback.answer()
        else:
            # This is a notification message - try to re-render based on notification type
            notification_type = await notification_registry.get_notification_type(
                callback.message.message_id
            )

            if notification_type is None:
                await callback.answer("Xatolik yuz berdi", show_alert=True)
                return

            # Get conversation and user info from registry
            conversation = await conversation_repo.get(conversation_id)
            if not conversation:
                await callback.answer("Suhbat topilmadi", show_alert=True)
                return

            user_telegram_id = await notification_registry.get_user_id(
                callback.message.message_id
            )
            if not user_telegram_id:
                await callback.answer("Foydalanuvchi topilmadi", show_alert=True)
                return

            display_name = (
                await notification_registry.get_display_name(
                    callback.message.message_id
                )
                or "Noma'lum"
            )

            # Fetch and format chat history based on show_history state
            recent_messages = await conversation_repo.get_recent_messages(
                conversation.id, limit=settings.chat_history_notification_limit
            )

            # Re-render based on notification type
            # Format chat history
            if show_history:
                chat_history = _format_chat_history(recent_messages, max_length=1500)
            else:
                chat_history = "\n💬 <b>Suhbat tarixi yashirilgan. Ko'rish uchun tugmani bosing.</b>"

            # Create toggle button
            toggle_text = (
                "📖 Tarixni yashirish" if show_history else "📖 Tarixni ko'rsatish"
            )
            toggle_state = "0" if show_history else "1"

            if notification_type == "new_lead":
                # Re-render new lead notification with full lead data
                lead = await lead_repo.get_by_conversation(conversation_id)
                if lead:
                    # Get user entity for actual name
                    user = (
                        await user_repo.get_by_telegram_id(user_telegram_id)
                        if user_telegram_id
                        else None
                    )
                    actual_name = _actual_name(user) if user else "Noma'lum"

                    text = (
                        f"⭐ <b>Yangi Lead!</b>\n\n"
                        f"👤 <b>Kimdan:</b> {display_name}\n"
                        f"👤 <b>Ism:</b> {actual_name}\n"
                        f"📊 <b>Daraja:</b> {lead.score.value:.0%}\n"
                        f"📋 <b>Mijoz anketasi:</b>\n{lead.topic_summary}\n"
                        f"📞 <b>Aloqa:</b> {lead.contact_info or '—'}\n"
                        f"{chat_history}\n"
                        f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing."
                    )
                    from domain.entities import LeadStatus

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Bog'landim",
                                    callback_data=f"lead_status:{conversation_id}:{LeadStatus.CONTACTED.value}",
                                ),
                                InlineKeyboardButton(
                                    text="📅 Belgilandi",
                                    callback_data=f"lead_status:{conversation_id}:{LeadStatus.BOOKED.value}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    text="💰 To'langan",
                                    callback_data=f"lead_status:{conversation_id}:{LeadStatus.PAID.value}",
                                ),
                                InlineKeyboardButton(
                                    text="❌ Yo'qolgan",
                                    callback_data=f"lead_status:{conversation_id}:{LeadStatus.LOST.value}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    text="✅ Yopilgan",
                                    callback_data=f"lead_status:{conversation_id}:{LeadStatus.CLOSED.value}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    text=toggle_text,
                                    callback_data=f"toggle_history:{conversation_id}:{toggle_state}",
                                ),
                            ],
                        ]
                    )
                else:
                    # Fallback if lead not found
                    text = (
                        f"⭐ <b>Yangi Lead!</b>\n\n"
                        f"👤 <b>Kimdan:</b> {display_name}\n"
                        f"{chat_history}\n"
                        f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing."
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=toggle_text,
                                    callback_data=f"toggle_history:{conversation_id}:{toggle_state}",
                                ),
                            ],
                        ]
                    )
            elif notification_type == "user_followup":
                # Re-render user followup notification
                # Extract original message from text
                match = re.search(r"✉️ <b>Xabar:</b>\n(.+)", message_text)
                message_content = match.group(1) if match else "Xabar topilmadi"

                # Get user entity for actual name
                user = (
                    await user_repo.get_by_telegram_id(user_telegram_id)
                    if user_telegram_id
                    else None
                )
                actual_name = _actual_name(user) if user else "Noma'lum"

                text = (
                    f"💬 <b>Yangi xabar (eskalatsiya holatida)</b>\n\n"
                    f"👤 <b>Kimdan:</b> {display_name}\n"
                    f"👤 <b>Ism:</b> {actual_name}\n\n"
                    f"✉️ <b>Xabar:</b>\n{message_content}\n"
                    f"{chat_history}\n"
                    f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing."
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=toggle_text,
                                callback_data=f"toggle_history:{conversation_id}:{toggle_state}",
                            ),
                        ],
                    ]
                )
            elif notification_type == "escalation":
                # Re-render escalation notification
                # Extract summary from text
                match = re.search(r"📋 <b>Mijoz anketasi:</b>\n(.+)", message_text)
                summary = match.group(1) if match else "Ma'lumot topilmadi"

                # Get user entity for actual name
                user = (
                    await user_repo.get_by_telegram_id(user_telegram_id)
                    if user_telegram_id
                    else None
                )
                actual_name = _actual_name(user) if user else "Noma'lum"

                text = (
                    f"🔔 <b>Yangi mijoz murojaati</b>\n\n"
                    f"👤 <b>Kimdan:</b> {display_name}\n"
                    f"👤 <b>Ism:</b> {actual_name}\n\n"
                    f"📋 <b>Mijoz anketasi:</b>\n{summary}\n\n"
                    f"{chat_history}\n\n"
                    f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing — "
                    f"javobingiz foydalanuvchiga yuboriladi."
                )
                from domain.entities import LeadStatus

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Bog'landim",
                                callback_data=f"lead_status:{conversation_id}:{LeadStatus.CONTACTED.value}",
                            ),
                            InlineKeyboardButton(
                                text="📅 Belgilandi",
                                callback_data=f"lead_status:{conversation_id}:{LeadStatus.BOOKED.value}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                text="💰 To'langan",
                                callback_data=f"lead_status:{conversation_id}:{LeadStatus.PAID.value}",
                            ),
                            InlineKeyboardButton(
                                text="❌ Yo'qolgan",
                                callback_data=f"lead_status:{conversation_id}:{LeadStatus.LOST.value}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                text="✅ Yopilgan",
                                callback_data=f"lead_status:{conversation_id}:{LeadStatus.CLOSED.value}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                text=toggle_text,
                                callback_data=f"toggle_history:{conversation_id}:{toggle_state}",
                            ),
                        ],
                    ]
                )
            else:
                await callback.answer("Noma'lum xabar turi", show_alert=True)
                return

            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
            await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("lead_status:"))
async def on_lead_status_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    lead_repo: SQLiteLeadRepo,
    conversation_repo: SQLiteConversationRepo,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Handle lead status button clicks."""
    if callback.data is None or callback.message is None:
        return

    if callback.message.chat.id != settings.telegram_lead_chat_id:
        await callback.answer("Unauthorized", show_alert=True)
        return

    parts = _validate_callback_data(callback.data, 3)
    if parts is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    _, conversation_id_str, status_str = parts
    if not _validate_uuid(conversation_id_str):
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    try:
        conversation_id = UUID(conversation_id_str)
        # Validate status value
        try:
            new_status = LeadStatus(status_str)
        except ValueError:
            await callback.answer(
                USER_FRIENDLY_ERRORS["Noto'g'ri status tugmasi"], show_alert=True
            )
            return
    except (ValueError, TypeError):
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    # Update lead status
    try:
        await lead_repo.update_status(conversation_id, new_status)
        logger.info(
            "Lead status updated",
            conversation_id=conversation_id_str,
            new_status=status_str,
        )
    except Exception:
        logger.exception("Failed to update lead status")
        await callback.answer("Xatolik yuz berdi", show_alert=True)
        return

    # Close conversation if status is PAID, LOST, or CLOSED
    if _should_close_conversation_for_lead_status(new_status):
        conversation = await conversation_repo.get(conversation_id)
        if conversation:
            conversation.close()
            await conversation_repo.save(conversation)
            logger.info(
                "Conversation closed due to lead status",
                conversation_id=conversation_id_str,
            )

    # Re-render the message with updated status
    # Check if this is a lead detail view or escalation notification
    message_text = callback.message.text or ""
    if "Lead #" in message_text:
        # This is a lead detail view - extract lead number
        match = re.search(r"Lead #(\d+)", message_text)
        if match:
            lead_number = int(match.group(1))
            async with session_factory() as session:
                user_repo = SQLiteUserRepo(session)
                rendered = await _render_lead_detail(
                    lead_number=lead_number,
                    lead_repo=lead_repo,
                    conversation_repo=conversation_repo,
                    user_repo=user_repo,
                    bot=callback.bot,
                    settings=settings,
                )
                if rendered:
                    text, keyboard, _, _ = rendered
                    await callback.message.edit_text(
                        text, parse_mode="HTML", reply_markup=keyboard
                    )
    elif "🔔 <b>Yangi mijoz murojaati</b>" in message_text:
        # This is an escalation notification - re-render with updated status
        # Extract conversation_id and show_history state from existing keyboard
        show_history = False
        if (
            callback.message.reply_markup
            and callback.message.reply_markup.inline_keyboard
        ):
            for row in callback.message.reply_markup.inline_keyboard:
                for button in row:
                    if button.callback_data and button.callback_data.startswith(
                        "toggle_history:"
                    ):
                        parts = button.callback_data.split(":")
                        if len(parts) == 3:
                            show_history = parts[2] == "1"
                            break

        # Re-render escalation notification with new status
        notification_type = "escalation"
        conversation = await conversation_repo.get(conversation_id)
        if not conversation:
            await callback.answer("Suhbat topilmadi", show_alert=True)
            return

        # Get user info from message text
        match = re.search(r"👤 <b>Kimdan:</b> (.+)", message_text)
        display_name = match.group(1) if match else "Noma'lum"

        # Get user entity for actual name
        async with session_factory() as session:
            user_repo = SQLiteUserRepo(session)
            user_telegram_id = conversation.user_telegram_id
            user = (
                await user_repo.get_by_telegram_id(user_telegram_id)
                if user_telegram_id
                else None
            )
            actual_name = _actual_name(user) if user else "Noma'lum"

        # Fetch chat history
        recent_messages = await conversation_repo.get_recent_messages(
            conversation.id, limit=settings.chat_history_notification_limit
        )
        if show_history:
            chat_history = _format_chat_history(recent_messages, max_length=1500)
        else:
            chat_history = (
                "\n💬 <b>Suhbat tarixi yashirilgan. Ko'rish uchun tugmani bosing.</b>"
            )

        # Extract summary
        match = re.search(r"📋 <b>Mijoz anketasi:</b>\n(.+)", message_text)
        summary = match.group(1) if match else "Ma'lumot topilmadi"

        toggle_text = (
            "📖 Tarixni yashirish" if show_history else "📖 Tarixni ko'rsatish"
        )
        toggle_state = "0" if show_history else "1"

        text = (
            f"🔔 <b>Yangi mijoz murojaati</b>\n\n"
            f"👤 <b>Kimdan:</b> {display_name}\n"
            f"👤 <b>Ism:</b> {actual_name}\n\n"
            f"📋 <b>Mijoz anketasi:</b>\n{summary}\n\n"
            f"{chat_history}\n\n"
            f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing — "
            f"javobingiz foydalanuvchiga yuboriladi."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Bog'landim",
                        callback_data=f"lead_status:{conversation_id}:{LeadStatus.CONTACTED.value}",
                    ),
                    InlineKeyboardButton(
                        text="📅 Belgilandi",
                        callback_data=f"lead_status:{conversation_id}:{LeadStatus.BOOKED.value}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💰 To'langan",
                        callback_data=f"lead_status:{conversation_id}:{LeadStatus.PAID.value}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Yo'qolgan",
                        callback_data=f"lead_status:{conversation_id}:{LeadStatus.LOST.value}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="✅ Yopilgan",
                        callback_data=f"lead_status:{conversation_id}:{LeadStatus.CLOSED.value}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=toggle_text,
                        callback_data=f"toggle_history:{conversation_id}:{toggle_state}",
                    ),
                ],
            ]
        )

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    await callback.answer(f"Status o'zgartirildi: {_lead_status_label(new_status)}")


@router.callback_query(lambda c: c.data and c.data.startswith("leads_page:"))
async def on_leads_page_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    lead_repo: SQLiteLeadRepo,
) -> None:
    """Pagination callback for leads list."""
    if callback.data is None or callback.message is None:
        return

    if callback.message.chat.id != settings.telegram_lead_chat_id:
        await callback.answer("Unauthorized", show_alert=True)
        return

    if callback.data == "leads_page:current":
        await callback.answer()
        return

    parts = _validate_callback_data(callback.data, 4)
    if parts is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    _, page_text, filter_text, sort_text = parts
    page = _validate_page_number(page_text)
    if page is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    status_filter = (
        _validate_filter_value(filter_text) if filter_text != "all" else None
    )
    if filter_text != "all" and status_filter is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    sort_by_score = sort_text == "True"

    rendered = await _render_leads_list(
        lead_repo=lead_repo,
        page=page,
        status_filter=status_filter,
        sort_by_score=sort_by_score,
    )
    if rendered is None:
        await callback.answer(USER_FRIENDLY_ERRORS["Leadlar yo'q"], show_alert=True)
        return

    text, keyboard = rendered
    await _safe_edit_message(  # type: ignore[union-attr]
        callback.message,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("leads_filter:"))
async def on_leads_filter_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    lead_repo: SQLiteLeadRepo,
) -> None:
    """Filter callback for leads list."""
    if callback.data is None or callback.message is None:
        return

    if callback.message.chat.id != settings.telegram_lead_chat_id:
        await callback.answer("Unauthorized", show_alert=True)
        return

    parts = _validate_callback_data(callback.data, 4)
    if parts is None:
        await callback.answer("Noto'g'ri callback data.", show_alert=True)
        return

    _, filter_text, page_text, sort_text = parts
    page = _validate_page_number(page_text)
    if page is None:
        await callback.answer("Noto'g'ri callback data.", show_alert=True)
        return

    status_filter = (
        _validate_filter_value(filter_text) if filter_text != "all" else None
    )
    if filter_text != "all" and status_filter is None:
        await callback.answer("Noto'g'ri callback data.", show_alert=True)
        return

    sort_by_score = sort_text == "True"

    rendered = await _render_leads_list(
        lead_repo=lead_repo,
        page=page,
        status_filter=status_filter,
        sort_by_score=sort_by_score,
    )
    if rendered is None:
        await callback.answer(USER_FRIENDLY_ERRORS["Leadlar yo'q"], show_alert=True)
        return

    text, keyboard = rendered
    await _safe_edit_message(  # type: ignore[union-attr]
        callback.message,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("leads_sort:"))
async def on_leads_sort_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    lead_repo: SQLiteLeadRepo,
) -> None:
    """Sort callback for leads list."""
    if callback.data is None or callback.message is None:
        return

    if callback.message.chat.id != settings.telegram_lead_chat_id:
        await callback.answer("Unauthorized", show_alert=True)
        return

    parts = _validate_callback_data(callback.data, 4)
    if parts is None:
        await callback.answer("Noto'g'ri callback data.", show_alert=True)
        return

    _, filter_text, page_text, sort_text = parts
    page = _validate_page_number(page_text)
    if page is None:
        await callback.answer("Noto'g'ri callback data.", show_alert=True)
        return

    status_filter = (
        _validate_filter_value(filter_text) if filter_text != "all" else None
    )
    if filter_text != "all" and status_filter is None:
        await callback.answer("Noto'g'ri callback data.", show_alert=True)
        return

    sort_by_score = sort_text == "True"

    rendered = await _render_leads_list(
        lead_repo=lead_repo,
        page=page,
        status_filter=status_filter,
        sort_by_score=sort_by_score,
    )
    if rendered is None:
        await callback.answer(USER_FRIENDLY_ERRORS["Leadlar yo'q"], show_alert=True)
        return

    text, keyboard = rendered
    await _safe_edit_message(  # type: ignore[union-attr]
        callback.message,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


async def _render_lead_detail(
    *,
    lead_number: int,
    lead_repo: SQLiteLeadRepo,
    conversation_repo: SQLiteConversationRepo,
    user_repo: SQLiteUserRepo,
    bot: Bot,
    settings: Settings,
    show_history: bool = False,
) -> tuple[str, InlineKeyboardMarkup, int | None, str] | None:
    """Bitta lead uchun batafsil matn va status klaviaturasini quradi.

    Returns (text, keyboard, user_telegram_id, display_name) yoki None
    agar lead_number noto'g'ri bo'lsa.
    """
    leads = await lead_repo.list_with_pagination(
        offset=0,
        limit=1000,
        status_filter=None,
        sort_by_score=False,
    )

    if lead_number < 1 or lead_number > len(leads):
        return None

    lead = leads[lead_number - 1]
    created_at = lead.created_at.strftime("%Y-%m-%d %H:%M")
    summary = re.sub(r"<[^>]+>", "", lead.topic_summary)
    contact = re.sub(r"<[^>]+>", "", lead.contact_info or "Aloqa noma'lum")

    conversation = await conversation_repo.get(lead.conversation_id)

    display_name = "Noma'lum"
    actual_name = "Noma'lum"
    user_telegram_id: int | None = None
    if conversation and conversation.user_telegram_id:
        user_telegram_id = conversation.user_telegram_id
        try:
            chat = await bot.get_chat(user_telegram_id)
            display_name = (
                f"@{chat.username}" if chat.username else f"ID: {user_telegram_id}"
            )
        except Exception:  # noqa: BLE001
            display_name = f"ID: {user_telegram_id}"

        # Get user entity for actual name
        user = await user_repo.get_by_telegram_id(user_telegram_id)
        if user:
            actual_name = _actual_name(user)

    # Fetch and format chat history
    chat_history = ""
    if conversation:
        recent_messages = await conversation_repo.get_recent_messages(
            conversation.id, limit=settings.chat_history_detail_limit
        )
        if show_history:
            chat_history = _format_chat_history(recent_messages, max_length=3000)
        else:
            chat_history = (
                "\n💬 <b>Suhbat tarixi yashirilgan. Ko'rish uchun tugmani bosing.</b>"
            )

    text = (
        f"📋 <b>Lead #{lead_number}</b>\n\n"
        f"👤 <b>Kimdan:</b> {display_name}\n"
        f"� <b>Ism:</b> {actual_name}\n"
        f"�� <b>Sana:</b> {created_at}\n"
        f"📞 <b>Aloqa:</b> {contact}\n"
        f"📝 <b>Ma'lumot:</b> {summary}\n\n"
        f"📊 <b>Hozirgi status:</b> {_lead_status_label(lead.status)}\n\n"
        f"{chat_history}\n"
        f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing — "
        f"javobingiz foydalanuvchiga yuboriladi.\n\n"
        f"Statusni o'zgartirish uchun tugmalardan birini bosing:"
    )

    prefix = f"lead_status:{lead.conversation_id}:"
    keyboard_rows = [
        [
            InlineKeyboardButton(
                text="✅ Bog'landim",
                callback_data=f"{prefix}{LeadStatus.CONTACTED.value}",
            ),
            InlineKeyboardButton(
                text="📅 Belgilandi",
                callback_data=f"{prefix}{LeadStatus.BOOKED.value}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💰 To'langan",
                callback_data=f"{prefix}{LeadStatus.PAID.value}",
            ),
            InlineKeyboardButton(
                text="❌ Yo'qolgan",
                callback_data=f"{prefix}{LeadStatus.LOST.value}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="✅ Yopilgan",
                callback_data=f"{prefix}{LeadStatus.CLOSED.value}",
            ),
        ],
    ]

    # Add history toggle button
    toggle_text = "📖 Tarixni yashirish" if show_history else "📖 Tarixni ko'rsatish"
    toggle_state = "0" if show_history else "1"
    if conversation:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"toggle_history:{conversation.id}:{toggle_state}",
                ),
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    return text, keyboard, user_telegram_id, display_name


# NOTE: lead_detail callback handler o'chirilgan emas — eski xabarlarda
# hali ham shu callback_data bo'lgan tugmalar bo'lishi mumkin (agar
# oldindan yuborilgan bo'lsa). Yangi ro'yxatlarda endi bu tugma
# chiqarilmaydi — o'rniga "Batafsil: /leads {index}" matni ko'rsatiladi.
@router.callback_query(lambda c: c.data and c.data.startswith("lead_detail:"))
async def on_lead_detail_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    lead_repo: SQLiteLeadRepo,
    conversation_repo: SQLiteConversationRepo,
    notification_registry: SQLiteNotificationRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Show detailed lead information when button is clicked (legacy)."""
    if callback.data is None or callback.message is None:
        return

    if callback.message.chat.id != settings.telegram_lead_chat_id:
        await callback.answer("Unauthorized", show_alert=True)
        return

    parts = _validate_callback_data(callback.data, 2)
    if parts is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    lead_number = _validate_page_number(parts[1])
    if lead_number is None:
        await callback.answer(
            USER_FRIENDLY_ERRORS["Noto'g'ri callback data"], show_alert=True
        )
        return

    async with session_factory() as session:
        user_repo = SQLiteUserRepo(session)
        rendered = await _render_lead_detail(
            lead_number=lead_number,
            lead_repo=lead_repo,
            conversation_repo=conversation_repo,
            user_repo=user_repo,
            bot=callback.bot,
            settings=settings,
        )
        if rendered is None:
            await callback.answer(
                USER_FRIENDLY_ERRORS["Lead topilmadi"], show_alert=True
            )
            return

        text, keyboard, user_telegram_id, display_name = rendered

        try:
            sent = await callback.message.edit_text(  # type: ignore[union-attr]
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            exc_str = str(exc).lower()
            if (
                "message is not modified" in exc_str
                or "message not modified" in exc_str
            ):
                logger.debug(
                    "Message edit skipped - content unchanged",
                    error=str(exc),
                )
                # Use existing message for notification registry
                sent = callback.message
            else:
                logger.warning(
                    "Failed to edit message",
                    error=str(exc),
                    text_preview=text[:200],
                )
                raise

    if user_telegram_id:
        await notification_registry.save(
            message_id=sent.message_id,
            user_telegram_id=user_telegram_id,
            display_name=display_name,
        )

    await callback.answer()


# ────────────────────── Oddiy matn xabarlari ──────────────────────


@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: types.Message) -> None:
    """/help komandasi."""
    await message.answer(
        "ℹ️ <b>Bot xizmati haqida:</b>\n\n"
        "Ushbu bot vaziyatingizni tez tushunish, kerakli ma'lumotlarni yig'ish va professional yuridik yordamga yo'naltirish uchun ishlab chiqilgan.\n\n"
        "⚖️ <b>Imkoniyatlar:</b>\n"
        "• Muammo turi, hudud, muddat va hujjatlarni aniqlash\n"
        "• Pullik konsultatsiyaga mos murojaatlarni advokat jamoasiga yuborish\n"
        "• Murakkab masalalarda <b>Advokat Jasurbek</b> bilan to'g'ridan-to'g'ri bog'lash\n\n"
        "💬 <i>Vaziyatingizni 2-3 gapda yozing. Maxfiy pasport yoki karta ma'lumotlarini yubormang.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_admin_stats(
    message: types.Message,
    settings: Settings,
    conversation_repo: SQLiteConversationRepo,
    lead_repo: SQLiteLeadRepo,
    user_repo: SQLiteUserRepo,
) -> None:
    """Admin guruhida qisqa operational statistikani ko'rsatida."""
    if message.chat.id != settings.telegram_lead_chat_id:
        return
    since = datetime.now(UTC) - timedelta(days=1)
    month_since = datetime.now(UTC) - timedelta(days=30)
    conversation_stats = await conversation_repo.get_stats(since=since)
    total_leads = await lead_repo.count_all()
    leads_since = await lead_repo.count_since(since)
    contacted_leads = await lead_repo.count_by_status(LeadStatus.CONTACTED)
    booked_leads = await lead_repo.count_by_status(LeadStatus.BOOKED)
    paid_leads = await lead_repo.count_by_status(LeadStatus.PAID)
    lost_leads = await lead_repo.count_by_status(LeadStatus.LOST)

    # User statistics
    total_users = await user_repo.count_all()
    monthly_users = await user_repo.count_since(month_since)

    conversion_rate = (
        total_leads / conversation_stats.total_conversations
        if conversation_stats.total_conversations
        else 0
    )

    text = (
        "📊 <b>Lead Bot Statistikasi</b>\n\n"
        f"👥 <b>Oylik foydalanuvchilar:</b> {monthly_users}\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {total_users}\n\n"
        f"🆕 <b>24 soat:</b> {conversation_stats.conversations_since} suhbat, "
        f"{leads_since} lead\n"
        f"💬 <b>Jami suhbatlar:</b> {conversation_stats.total_conversations}\n"
        f"⭐ <b>Jami leadlar:</b> {total_leads}\n"
        f"🤖 <b>AI rejimida:</b> {conversation_stats.active_ai_conversations}\n"
        f"👨‍💼 <b>Admin kutmoqda:</b> {conversation_stats.escalated_conversations}\n"
        f"📞 <b>Bog'lanilgan:</b> {contacted_leads}\n"
        f"📅 <b>Belgilangan:</b> {booked_leads}\n"
        f"💰 <b>To'langan:</b> {paid_leads}\n"
        f"❌ <b>Yo'qolgan:</b> {lost_leads}\n"
        f"✅ <b>Yopilgan:</b> {conversation_stats.closed_conversations}\n"
        f"✉️ <b>Jami xabarlar:</b> {conversation_stats.total_messages}\n"
        f"📈 <b>Lead konversiyasi:</b> {conversion_rate:.0%}"
    )
    await message.reply(text, parse_mode="HTML")


@router.message(Command("leads"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_admin_leads(
    message: types.Message,
    settings: Settings,
    lead_repo: SQLiteLeadRepo,
    conversation_repo: SQLiteConversationRepo,
    notification_registry: SQLiteNotificationRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin guruhida leadlar ro'yxatini pagination bilan ko'rsatida
    yoki bitta leadni status bilan ko'rsatida (/leads {index})."""
    if message.chat.id != settings.telegram_lead_chat_id:
        return
    args = message.text.split()

    # Check if argument is a number (lead number) -> show detail view
    if len(args) > 1:
        try:
            lead_number = int(args[1])
        except ValueError:
            lead_number = None

        if lead_number is not None:
            async with session_factory() as session:
                user_repo = SQLiteUserRepo(session)
                rendered = await _render_lead_detail(
                    lead_number=lead_number,
                    lead_repo=lead_repo,
                    conversation_repo=conversation_repo,
                    user_repo=user_repo,
                    bot=message.bot,
                    settings=settings,
                )
                if rendered is None:
                    await message.reply(
                        "❌ <b>Lead topilmadi. Raqamni tekshiring.</b>",
                        parse_mode="HTML",
                    )
                    return

                text, keyboard, user_telegram_id, display_name = rendered
                sent = await message.reply(
                    text, parse_mode="HTML", reply_markup=keyboard
                )

                if user_telegram_id:
                    await notification_registry.save(
                        message_id=sent.message_id,
                        user_telegram_id=user_telegram_id,
                        display_name=display_name,
                    )
            return

    # Original list logic with filters
    status_filter = None
    sort_by_score = False

    if len(args) > 1:
        filter_arg = args[1].lower()
        if filter_arg in ("ochiq", "yangi", "yopiq"):
            status_filter = filter_arg
        elif filter_arg == "yuqori":
            sort_by_score = True

    rendered = await _render_leads_list(
        lead_repo=lead_repo,
        page=0,
        status_filter=status_filter,
        sort_by_score=sort_by_score,
    )
    if rendered is None:
        filter_text = f" ({status_filter})" if status_filter else ""
        await message.reply(
            f"✅ <b>Leadlar yo'q{filter_text}.</b>",
            parse_mode="HTML",
        )
        return

    text, keyboard = rendered
    await message.reply(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("users"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_admin_users(
    message: types.Message,
    settings: Settings,
    user_repo: SQLiteUserRepo,
) -> None:
    """Admin guruhida bot foydalanuvchilari ro'yxatini ko'rsatida."""
    if message.chat.id != settings.telegram_lead_chat_id:
        return

    users = await user_repo.get_all()
    total_users = await user_repo.count_all()

    if not users:
        await message.reply(
            "✅ <b>Hozircha foydalanuvchilar yo'q.</b>",
            parse_mode="HTML",
        )
        return

    lines = [
        "👥 <b>Bot foydalanuvchilari</b>",
        f"\n📊 Jami: {total_users} ta foydalanuvchi\n\n",
    ]

    for i, user in enumerate(users[:20], start=1):  # Show first 20 users
        created_at = user.created_at.strftime("%d %b %H:%M")
        username = f"@{user.username}" if user.username else "Username yo'q"
        channel_status = "✅" if user.has_joined_channel else "❌"
        lines.append(
            f"{i}. {username} (ID: {user.telegram_id})\n"
            f"   {channel_status} Kanal: {'A\'zo' if user.has_joined_channel else 'A\'zo emas'}\n"
            f"   📅 {created_at}\n"
        )

    if total_users > 20:
        lines.append(f"\n... va yana {total_users - 20} ta foydalanuvchi")

    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(
    Command("close"),
    F.chat.type.in_({"group", "supergroup"}),
    F.reply_to_message,
)
async def cmd_admin_close_conversation(
    message: types.Message,
    settings: Settings,
    notifier: TelegramAdminNotifier,
    conversation_repo: SQLiteConversationRepo,
    lead_repo: SQLiteLeadRepo,
) -> None:
    """Admin bot notification'iga reply qilib suhbatni yopadi."""
    if message.chat.id != settings.telegram_lead_chat_id:
        return
    replied = message.reply_to_message
    if replied is None:
        await message.reply(
            "❌ <b>Iltimos, bot xabariga reply qiling.</b>",
            parse_mode="HTML",
        )
        return

    user_telegram_id = await notifier.get_user_id_for_message(replied.message_id)
    if user_telegram_id is None:
        await message.reply(
            "❌ <b>Bu xabar bo'yicha foydalanuvchi topilmadi.</b>",
            parse_mode="HTML",
        )
        return

    user_uuid = _stable_user_id(user_telegram_id)
    conversation = await conversation_repo.get_active_for_user(user_uuid)
    if conversation is None:
        await message.reply("✅ Faol suhbat allaqachon yo'q.", parse_mode="HTML")
        return

    conversation.close()
    await conversation_repo.save(conversation)
    await lead_repo.update_status(conversation.id, LeadStatus.CLOSED)
    await message.reply("✅ <b>Suhbat yopildi.</b>", parse_mode="HTML")


@router.message(Command("history"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_admin_history(
    message: types.Message,
    settings: Settings,
    conversation_repo: SQLiteConversationRepo,
) -> None:
    """Admin guruhida suhbat tarixini ko'rsatadi (/history {conversation_id})."""
    if message.chat.id != settings.telegram_lead_chat_id:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.reply(
            "❌ <b>Iltimos, conversation ID kiriting.</b>\n\n"
            "Foydalanish: /history {conversation_id}",
            parse_mode="HTML",
        )
        return

    conversation_id_str = args[1]
    if not _validate_uuid(conversation_id_str):
        await message.reply(
            "❌ <b>Noto'g'ri conversation ID formati.</b>",
            parse_mode="HTML",
        )
        return

    conversation_id = UUID(conversation_id_str)
    conversation = await conversation_repo.get(conversation_id)

    if conversation is None:
        await message.reply(
            "❌ <b>Suhbat topilmadi.</b>",
            parse_mode="HTML",
        )
        return

    # Fetch all messages for this conversation
    messages = await conversation_repo.get_recent_messages(conversation_id, limit=100)

    if not messages:
        await message.reply(
            "✅ <b>Suhbat tarixi bo'sh.</b>",
            parse_mode="HTML",
        )
        return

    # Format full history
    chat_history = _format_chat_history(messages, max_length=4000)

    text = (
        f"📜 <b>Suhbat tarixi</b>\n\n"
        f"🆔 <b>Conversation ID:</b> {conversation_id}\n"
        f"👤 <b>User ID:</b> {conversation.user_id}\n"
        f"📊 <b>Status:</b> {conversation.status.value}\n"
        f"{chat_history}"
    )

    await message.reply(text, parse_mode="HTML")


@router.message(F.chat.type == "private")
async def on_user_message(
    message: types.Message,
    handle_message: HandledUserMessageUseCase,
    conversation_repo: SQLiteConversationRepo,
    user_repo: SQLiteUserRepo,
    settings: Settings,
) -> None:
    """Har qanday oddiy matn xabarini qayta ishlash (asosiy pipeline)."""
    if message.from_user is None or not message.text:
        return

    # Validate message length
    if len(message.text) > MAX_MESSAGE_LENGTH:
        await message.reply(
            "❌ <b>Xabar juda uzun. Iltimos, qisqaroq xabar yuboring.</b>",
            parse_mode="HTML",
        )
        return

    # Kanal a'zoligini Telegram API orqali haqiqiy tekshirish
    is_member = await _check_channel_membership(
        message.bot,
        message.from_user.id,
        settings.required_channel_id,  # type: ignore[arg-type]
    )
    user = _resolve_user(message.from_user, has_joined_channel=is_member)

    # Foydalanuvchini saqlash
    await user_repo.save(user)

    # Foydalanuvchiga "yozmoqda..." ko'rsatish (LLM javobini kutayotganda)
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        reply_text = await handle_message.execute(user=user, message_text=message.text)
    except RateLimitExceededError as exc:
        # Use custom message if provided (for burst/violation penalties)
        if exc.message:
            reply_text = exc.message
        elif exc.reset_at is not None:
            from datetime import timezone

            tashkent = timezone(timedelta(hours=5))
            reset_local = exc.reset_at.astimezone(tashkent).strftime("%H:%M")
            reply_text = (
                f"⏳ <b>So'rovlar limiti oshib ketdi.</b>\n\n"
                f"Soatiga 60 ta xabar yuborish mumkin.\n"
                f"Limit <b>{reset_local}</b> da yangilanadi."
            )
        else:
            reply_text = (
                "⏳ <b>So'rovlar limiti oshib ketdi.</b>\n\n"
                "Soatiga 60 ta xabar yuborish mumkin. Keyinroq qayta urinib ko'ring."
            )
    except ChannelMembershipRequiredError:
        await message.answer(
            "📢 <b>Botdan to'liq foydalanish uchun rasmiy kanalimizga a'zo bo'ling:</b>",
            reply_markup=_channel_join_keyboard(settings.required_channel_username),
            parse_mode="HTML",
        )
        return
    except Exception:
        logger.exception("Unexpected error in on_user_message")
        reply_text = (
            "⚠️ <b>Texnik xatolik yuz berdi.</b>\n\n"
            "Kechirasiz, so'rovingizni qayta ishlashda uzilish bo'ldi. Iltimos, birozdan so'ng qayta urinib ko'ring."
        )

    # Eskalatsiya holatida "AI ga qaytish" tugmasini ko'rsatish
    user_id = _stable_user_id(message.from_user.id)
    conversation = await conversation_repo.get_active_for_user(user_id)

    if conversation and _is_escalated(conversation.status):
        await _safe_answer(
            message,
            reply_text,
            reply_markup=_return_to_ai_keyboard(),
        )
    else:
        await _safe_answer(message, reply_text)


# ────────────────────── Admin reply handler ──────────────────────


@router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message)
async def on_admin_reply_to_notification(
    message: types.Message,
    notifier: TelegramAdminNotifier,
    conversation_repo: SQLiteConversationRepo,
) -> None:
    """Admin bot xabariga reply qilganda foydalanuvchiga javob yuboradi.

    Admin Telegram'ning standart Reply funksiyasidan foydalanadi:
    bot yuborgan bildirishnoma xabariga reply yozadi, va
    bot javobni avtomatik foydalanuvchiga yo'naltiradi.

    Agar foydalanuvchi AI rejimiga qaytgan bo'lsa — suhbat qayta
    eskalatsiya qilinadi, shunda keyingi xabarlar admin ga boradi.
    """
    if message.from_user is None or not message.text:
        return

    replied = message.reply_to_message
    if replied is None or replied.from_user is None:
        return

    # Faqat bot xabariga reply qilingan bo'lsa ishlaydi
    if not replied.from_user.is_bot:
        return

    # Registry orqali foydalanuvchi IDsini topish (DB'dan)
    user_telegram_id = await notifier.get_user_id_for_message(replied.message_id)

    if user_telegram_id is None:
        logger.warning(
            "Admin reply: message_id not in registry (old notification or unknown)",
            replied_message_id=replied.message_id,
        )
        return

    # Ko'rsatma ism: DB'dan olamiz, fallback — matndan
    display_name = await notifier.get_display_for_message(replied.message_id)
    if not display_name:
        replied_text = replied.text or replied.caption or ""
        display_name = TelegramAdminNotifier.extract_user_display_from_notification(
            replied_text
        ) or str(user_telegram_id)

    try:
        # Foydalanuvchiga javob yuborish
        await notifier.send_reply_to_user(
            user_telegram_id=user_telegram_id,
            reply_text=message.text,
        )

        # Suhbatni eskalatsiya holatiga qaytarish (har doim)
        user_uuid = _stable_user_id(user_telegram_id)
        conversation = await conversation_repo.get_active_for_user(user_uuid)
        if conversation:
            from domain.value_objects import EscalationTarget

            conversation.escalate(EscalationTarget.LEAD)
            await conversation_repo.save(conversation)
            logger.info(
                "Conversation escalated after admin reply",
                user_id=user_telegram_id,
                conversation_id=conversation.id,
            )

        await message.reply(
            f"✅ <b>Javob {display_name} ga yuborildi.</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Error sending admin reply")
        await message.reply(f"❌ Xatolik: {exc}")
