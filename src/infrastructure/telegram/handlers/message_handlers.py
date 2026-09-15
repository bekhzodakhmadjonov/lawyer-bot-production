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

import html
import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from infrastructure.persistence.postgres_conversation_repo import (
    PostgresConversationRepo,
)
from infrastructure.persistence.postgres_lead_repo import PostgresLeadRepo
from infrastructure.persistence.postgres_notification_registry import (
    PostgresNotificationRegistry,
)
from infrastructure.persistence.postgres_user_repo import PostgresUserRepo

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


TELEGRAM_MAX_LENGTH = 4000  # Leave buffer from Telegram's 4096 limit


def _split_long_text(text: str, max_len: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Uzun matnni Telegram limitiga mos bo'laklarga ajratadi."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at paragraph boundary
        split_at = text.rfind("\n\n", 0, max_len)
        if split_at == -1:
            # Try line boundary
            split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            # Force split at max length
            split_at = max_len
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


async def _safe_answer(
    message: types.Message,
    text: str,
    **kwargs,
) -> None:
    """HTML bilan xabar yuboradi; HTML xato bo'lsa teg'siz qayta yuboradi.
    Uzun xabarlarni avtomatik bo'laklarga ajratib yuboradi.
    reply_markup faqat oxirgi bo'lakga qo'shiladi.
    """
    chunks = _split_long_text(text)
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        # reply_markup faqat oxirgi bo'lakda bo'lsin
        chunk_kwargs = dict(kwargs)
        if i < total - 1:
            chunk_kwargs.pop("reply_markup", None)
        try:
            await message.answer(chunk, parse_mode="HTML", **chunk_kwargs)
        except TelegramBadRequest as exc:
            exc_str = str(exc).lower()
            if "can't parse entities" in exc_str or "parse entities" in exc_str:
                plain = re.sub(r"<[^>]+>", "", chunk).strip()
                if not plain:
                    plain = "\u26a0\ufe0f Javob tayyorlandi, lekin formatlashda xatolik bo'ldi."
                logger.warning(
                    "HTML parse error \u2014 sending as plain text",
                    error=str(exc),
                    text_preview=chunk[:200],
                )
                await message.answer(plain, parse_mode=None, **chunk_kwargs)
            elif "message is too long" in exc_str:
                # Emergency split if a single chunk is still too long
                logger.warning("Message still too long after split, force-splitting")
                sub_chunks = _split_long_text(chunk, max_len=2000)
                for j, sub in enumerate(sub_chunks):
                    sub_kwargs = dict(kwargs)
                    if i < total - 1 or j < len(sub_chunks) - 1:
                        sub_kwargs.pop("reply_markup", None)
                    await message.answer(sub, parse_mode=None, **sub_kwargs)
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
    allowed_filters = {"all", "ochiq", "yangi", "yopiq", "yuqori"}
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
    """Engaging welcome message for subscribed users."""
    return (
        "⚖️ <b>Advokat Jasurbek jamoasining AI Huquqiy Yordamchisi</b>\n\n"
        "Assalomu alaykum! 👋\n\n"
        "Siz huquqiy masalangiz bo‘yicha tezkor va tushunarli ma’lumot olishingiz mumkin.\n\n"
        "🤖 <b>Men sizga:</b>\n"
        "• huquqiy savollaringizga javob beraman;\n"
        "• vaziyatingiz bo‘yicha tegishli qonun va tartiblarni tushuntiraman;\n"
        "• keyingi qadamlar bo‘yicha yo‘nalish beraman;\n"
        "• agar masalangizda advokat yordami kerak bo‘lsa, sizni Advokat Jasurbek jamoasiga bog‘lashga yordam beraman.\n\n"
        "💬 <b>Savolingizni oddiy tilda yozing.</b>\n\n"
        "<b>Masalan:</b>\n"
        "▫️ <i>“Ish beruvchim meni ogohlantirmasdan ishdan bo‘shatdi. Nima qilishim mumkin?”</i>\n\n"
        "▫️ <i>“2 nafar farzandim uchun aliment qancha bo‘ladi?”</i>\n\n"
        "▫️ <i>“Menga shartnoma bo‘yicha da’vo kelgan, nima qilishim kerak?”</i>\n\n"
        "▫️ <i>“Merosni qanday rasmiylashtirish mumkin?”</i>\n\n"
        "📌 <i>Vaziyatingizni imkon qadar aniq yozing: nima bo‘ldi, qachon bo‘ldi va qanday hujjatlar mavjud.</i>\n\n"
        "👇 <b>Savolingizni yozing:</b>"
    )


def _subscription_required_message() -> str:
    """Welcome text shown before channel subscription."""
    return (
        "⚖️ <b>Huquqiy yordamchidan foydalanishni boshlang</b>\n\n"
        "Assalomu alaykum! 👋\n\n"
        "Bu — <b>Advokat Jasurbek</b> jamoasining AI Huquqiy Yordamchisi.\n\n"
        "🤖 <b>Bu yerda siz:</b>\n"
        "• huquqiy savollaringizga javob olishingiz;\n"
        "• qonun va huquqiy tartiblarni tushunishingiz;\n"
        "• vaziyatingiz bo‘yicha keyingi qadamlarni aniqlashingiz;\n"
        "• zarur bo‘lsa, advokat jamoasi bilan bog‘lanishingiz mumkin.\n\n"
        "📢 <b>Botdan foydalanishni davom ettirish uchun kanalimizga a’zo bo‘ling.</b>\n\n"
        "Kanalimizda huquqiy maslahatlar, qonunchilikdagi yangiliklar va amaliy tavsiyalar berib boriladi.\n\n"
        "👇 A’zo bo‘ling va <b>“✅ Qo‘shildim”</b> tugmasini bosing."
    )


def _subscription_confirmed_message() -> str:
    """Message shown after the channel membership check succeeds."""
    return (
        "✅ <b>A’zolik tasdiqlandi!</b>\n\n"
        "Endi huquqiy savolingizni yozishingiz mumkin.\n\n"
        "🤖 Men vaziyatingizni tahlil qilib, imkon qadar tushunarli javob beraman.\n\n"
        "📌 <b>Yaxshiroq javob olish uchun:</b>\n"
        "<i>Nima bo‘ldi? → Qachon bo‘ldi? → Qanday hujjatlaringiz bor?</i>\n\n"
        "<b>Masalan:</b>\n"
        "<i>“Ish beruvchim meni 3 kun oldin ishdan bo‘shatdi. Hech qanday ogohlantirish berilmagan. Menda mehnat shartnomasi bor.”</i>\n\n"
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
        LeadStatus.DELETED: "O'chirilgan",
    }
    return labels[status]


def _lead_score_label(score_value: float) -> str:
    """Lead score uchun vizual indikator."""
    if score_value >= 0.7:
        return "⭐ Yuqori"
    elif score_value >= 0.4:
        return "⚡ O'rtacha"
    else:
        return "💡 Past"



def _format_date(dt: datetime) -> str:
    """Sana formatini chiroyli ko'rsatish (masalan: 14-Sen)."""
    uzbek_months = {
        1: "Yan",
        2: "Fev",
        3: "Mar",
        4: "Apr",
        5: "May",
        6: "Iyun",
        7: "Iyul",
        8: "Avg",
        9: "Sen",
        10: "Okt",
        11: "Noy",
        12: "Dek",
    }
    return f"{dt.day}-{uzbek_months.get(dt.month, str(dt.month))}"


def _extract_lead_fields(summary: str) -> dict[str, str]:
    """Extract individual lead fields from the summary text."""
    fields = {
        "name": "Noma'lum",
        "location": "Noma'lum",
        "phone": "Noma'lum",
        "category": "Noma'lum",
        "urgency": "Noma'lum",
        "documents": "Noma'lum",
        "problem": "Noma'lum",
    }

    # Strip ALL HTML tags from summary first so tags like <b> or </b> never leak into extracted values
    clean_summary = re.sub(r"<[^>]+>", "", summary or "")

    def _clean_val(val: str) -> str:
        return re.sub(r"<[^>]+>", "", val).strip()

    # Pattern: "👤 Ism: value" or "Ism: value"
    name_match = re.search(r"👤?\s*Ism:\s*([^\n]+)", clean_summary)
    if name_match:
        fields["name"] = _clean_val(name_match.group(1))

    # Pattern: "📍 Hudud: value" or "Hudud: value"
    location_match = re.search(r"📍?\s*Hudud:\s*([^\n]+)", clean_summary)
    if location_match:
        fields["location"] = _clean_val(location_match.group(1))

    # Pattern: "📞 Telefon: value" or "Telefon: value"
    phone_match = re.search(r"📞?\s*Telefon:\s*([^\n]+)", clean_summary)
    if phone_match:
        fields["phone"] = _clean_val(phone_match.group(1))

    # Pattern: "⚖️ Sohasi: value" or "Sohasi: value"
    category_match = re.search(r"⚖️?\s*Sohasi:\s*([^\n]+)", clean_summary)
    if category_match:
        fields["category"] = _clean_val(category_match.group(1))

    # Pattern: "🔥 Muhimlik: value" or "Muhimlik: value"
    urgency_match = re.search(r"🔥?\s*Muhimlik:\s*([^\n]+)", clean_summary)
    if urgency_match:
        fields["urgency"] = _clean_val(urgency_match.group(1))

    # Pattern: "📄 Hujjatlar: value" or "Hujjatlar: value"
    documents_match = re.search(r"📄?\s*Hujjatlar:\s*([^\n]+)", clean_summary)
    if documents_match:
        fields["documents"] = _clean_val(documents_match.group(1))

    # Pattern: "📝 Muammo: value" or "Muammo: value"
    problem_match = re.search(r"📝?\s*Muammo:\s*([^\n]+)", clean_summary)
    if problem_match:
        fields["problem"] = _clean_val(problem_match.group(1))

    # Map urgency to Uzbek words if in English
    raw_urgency = fields.get("urgency", "").lower().strip()
    urgency_map = {
        "high": "Yuqori 🔴",
        "medium": "O'rtacha 🟡",
        "low": "Oddiy 🟢",
    }
    for eng_val, uz_val in urgency_map.items():
        if eng_val in raw_urgency:
            fields["urgency"] = uz_val
            break

    return fields


def _format_lead_lines(leads: list, *, offset: int) -> list[str]:
    """Leadlar ro'yxatini zamonaviy CRM karta ko'rinishida formatlaydi."""
    lines: list[str] = []
    
    number_emojis = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    def get_number_emoji(num: int) -> str:
        if num <= 10:
            return number_emojis[num]
        return f"{num}."

    for index, lead in enumerate(leads, start=offset + 1):
        created_at = _format_date(lead.created_at)
        summary_raw = lead.topic_summary or ""

        # Extract structured fields using helper
        fields = _extract_lead_fields(summary_raw)

        # Extract clean legal problem description
        problem = fields.get("problem")
        if not problem or problem == "Noma'lum":
            cleaned = re.sub(
                r"(👤|📛|📞|📍|🔥|📄|⚖️)\s*[^:\n]+:\s*[^\n]+", "", summary_raw
            )
            problem = cleaned.strip() if cleaned.strip() else summary_raw

        # Strip HTML tags and normalize spaces
        problem = re.sub(r"<[^>]+>", "", problem)
        problem = re.sub(r"\s+", " ", problem).strip()
        if len(problem) > 90:
            problem = problem[:87] + "..."
        if not problem:
            problem = "Huquqiy maslahat so'ralgan"

        # Determine user header: Name (@username) or @username or Name
        name = fields.get("name")
        contact = (lead.contact_info or "").strip()
        contact = re.sub(r"<[^>]+>", "", contact).strip()
        if (
            contact
            and not contact.startswith("@")
            and not contact.startswith("+")
            and not contact.isdigit()
        ):
            contact = f"@{contact}"

        if name and name != "Noma'lum":
            clean_name = re.sub(r"<[^>]+>", "", name).strip()
            if contact and contact != "Noma'lum" and contact != clean_name:
                user_header = f"{clean_name} ({contact})"
            else:
                user_header = f"{clean_name}"
        elif contact and contact != "Noma'lum":
            user_header = f"{contact}"
        else:
            user_header = "Mijoz"

        # Extract other fields cleanly
        phone = fields.get("phone", "Noma'lum")
        if phone != "Noma'lum":
            phone = re.sub(r"<[^>]+>", "", phone).strip()

        location = fields.get("location", "Noma'lum")
        if location != "Noma'lum":
            location = re.sub(r"<[^>]+>", "", location).strip()

        star = " ⭐️" if lead.score.value >= 0.7 else ""
        status_label = _lead_status_label(lead.status)
        num_emoji = get_number_emoji(index)

        card = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{num_emoji} {user_header}{star}\n"
            f"📁 Holat: {status_label} | 📅 {created_at}\n"
            f"📞 Tel: {phone}\n"
            f"📍 Manzil: {location}\n"
            f"📝 Muammo: {problem}\n"
        )
        lines.append(card)

    if lines:
        lines.append("━━━━━━━━━━━━━━━━━━━━\n")

    return lines


def _leads_list_keyboard(
    *,
    page: int,
    total_pages: int,
    status_filter: str | None,
    sort_by_score: bool = False,
    leads_count: int = 0,
    offset: int = 0,
) -> InlineKeyboardMarkup:
    """Leadlar ro'yxati uchun navigatsiya, tezkor ochish va filtr klaviaturasi."""
    keyboard: list[list[InlineKeyboardButton]] = []

    # 1. Tezkor ochish tugmalari (bitta bosishda xabarni almashtirib ochadi)
    if leads_count > 0:
        lead_btns: list[InlineKeyboardButton] = []
        chunk_size = 2 if leads_count <= 4 else 5
        for i in range(offset + 1, offset + leads_count + 1):
            btn_text = f"🔍 #{i} Ochish" if leads_count <= 4 else f"🔍 #{i}"
            lead_btns.append(
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"lead_detail:{i}",
                )
            )
        for chunk in [lead_btns[i : i + chunk_size] for i in range(0, len(lead_btns), chunk_size)]:
            keyboard.append(chunk)

    # 2. Navigatsiya qatori
    if total_pages > 1:
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

    # 3. Status filtrlari (1-qator)
    filter_row_1 = [
        InlineKeyboardButton(
            text="📋 Hammasi" if status_filter in (None, "all") else "Hammasi",
            callback_data=f"leads_filter:all:{page}:{sort_by_score}",
        ),
        InlineKeyboardButton(
            text="🟢 Ochiq" if status_filter == "ochiq" else "Ochiq",
            callback_data=f"leads_filter:ochiq:{page}:{sort_by_score}",
        ),
        InlineKeyboardButton(
            text="🆕 Yangi" if status_filter == "yangi" else "Yangi",
            callback_data=f"leads_filter:yangi:{page}:{sort_by_score}",
        ),
    ]
    keyboard.append(filter_row_1)

    # 4. Muhimlik filtri (2-qator, keng tugma - hech qachon kesilmaydi)
    filter_row_2 = [
        InlineKeyboardButton(
            text="⭐️ Faqat Yuqori"
            if status_filter == "yuqori"
            else "⭐️ Yuqori",
            callback_data=f"leads_filter:yuqori:{page}:{sort_by_score}",
        ),
    ]
    keyboard.append(filter_row_2)

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
            sender_label = "👨‍💼 Advokat"
        else:
            sender_label = "🔧 Tizim"

        # Format timestamp
        time_str = msg.sent_at.strftime("%H:%M")

        # Strip all HTML tags from the message text so <b> doesn't show as literal text
        clean_text = re.sub(r"<[^>]+>", "", msg.text)

        # Truncate message if too long
        if len(clean_text) > 300:
            clean_text = clean_text[:297] + "..."

        # Escape HTML special characters to prevent parsing errors
        clean_text = html.escape(clean_text)

        lines.append(f"{sender_label} [{time_str}]:\n{clean_text}\n")

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
    filter_labels = {
        "ochiq": "🟢 Ochiq",
        "yangi": "🆕 Yangi",
        "yuqori": "⭐️ Yuqori",
        "yopiq": "📁 Yopiq",
    }
    filter_desc = (
        f" ({filter_labels.get(status_filter, status_filter)})"
        if status_filter
        else ""
    )
    return [
        f"📋 <b>Leadlar ro'yxati</b>{filter_desc}",
        f"📊 <b>Sahifa:</b> {page + 1}/{total_pages} (Jami: {total_count} ta)\n",
    ]


async def _render_leads_list(
    *,
    lead_repo: PostgresLeadRepo,
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
        leads_count=len(leads),
        offset=offset,
    )

    return "\n".join(lines), keyboard


# ────────────────────── /start komandasi ──────────────────────


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(
    message: types.Message,
    settings: Settings,
    conversation_repo: PostgresConversationRepo,
    user_repo: PostgresUserRepo,
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
    conversation_repo: PostgresConversationRepo,
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
    conversation_repo: PostgresConversationRepo,
    lead_repo: PostgresLeadRepo,
    notification_registry: PostgresNotificationRegistry,
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
        user_repo = PostgresUserRepo(session)

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
        message_text = callback.message.html_text or ""
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
                logger.warning(
                    "Notification type not found in registry",
                    message_id=callback.message.message_id,
                    conversation_id=conversation_id_str,
                )
                await callback.answer("Xatolik yuz berdi. Iltimos, qayta urinib ko'ring.", show_alert=True)
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
                        f"⭐ <b>Yangi mijoz!</b>\n\n"
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
                        f"⭐ <b>Yangi mijoz!</b>\n\n"
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

            try:
                await callback.message.edit_text(
                    text, parse_mode="HTML", reply_markup=keyboard
                )
            except TelegramBadRequest:
                # Message content unchanged, skip edit
                pass
            await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("lead_status:"))
async def on_lead_status_callback(
    callback: types.CallbackQuery,
    settings: Settings,
    lead_repo: PostgresLeadRepo,
    conversation_repo: PostgresConversationRepo,
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

    # Re-render the message with updated status
    # Check if this is a lead detail view or escalation notification
    message_text = callback.message.text or ""
    if "Lead #" in message_text:
        # This is a lead detail view - extract lead number
        match = re.search(r"Lead #(\d+)", message_text)
        if match:
            lead_number = int(match.group(1))
            async with session_factory() as session:
                user_repo = PostgresUserRepo(session)
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
            user_repo = PostgresUserRepo(session)
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
    lead_repo: PostgresLeadRepo,
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
    lead_repo: PostgresLeadRepo,
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
    lead_repo: PostgresLeadRepo,
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
    lead_repo: PostgresLeadRepo,
    conversation_repo: PostgresConversationRepo,
    user_repo: PostgresUserRepo,
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

    # Extract individual fields from summary
    lead_fields = _extract_lead_fields(summary)

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
        f"👤 <b>Ism:</b> {actual_name}\n"
        f"📅 <b>Sana:</b> {created_at}\n"
        f"📍 <b>Hudud:</b> {lead_fields['location']}\n"
        f"📞 <b>Telefon:</b> {lead_fields['phone']}\n"
        f"🔥 <b>Muhimlik:</b> {lead_fields['urgency']}\n"
        f"📄 <b>Hujjatlar:</b> {lead_fields['documents']}\n\n"
        f"📝 <b>Muammo:</b> {lead_fields['problem']}\n\n"
        f"📊 <b>Hozirgi status:</b> {_lead_status_label(lead.status)}\n\n"
        f"{chat_history}\n\n"
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
            InlineKeyboardButton(
                text="🗑 O'chirish",
                callback_data=f"{prefix}{LeadStatus.DELETED.value}",
            ),
        ],
    ]

    # Add history toggle button and back button
    toggle_text = "📖 Tarixni yashirish" if show_history else "📖 Tarixni ko'rsatish"
    toggle_state = "0" if show_history else "1"
    bottom_row: list[InlineKeyboardButton] = []
    if conversation:
        bottom_row.append(
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=f"toggle_history:{conversation.id}:{toggle_state}",
            )
        )
    bottom_row.append(
        InlineKeyboardButton(
            text="◀ Ro'yxat",
            callback_data="leads_page:0:all:False",
        )
    )
    keyboard_rows.append(bottom_row)

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
    lead_repo: PostgresLeadRepo,
    conversation_repo: PostgresConversationRepo,
    notification_registry: PostgresNotificationRegistry,
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
        user_repo = PostgresUserRepo(session)
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
    """/help komandasi — foydalanuvchiga botdan foydalanish bo'yicha to'liq qo'llanma."""
    text = (
        "⚖️ <b>Advokat Jasurbek jamoasi — AI Huquqiy Yordamchi</b>\n\n"
        "Ushbu bot fuqarolarga huquqiy masalalarda tezkor tushuntirish berish va "
        "zarur hollarda professional advokatga bog'lash uchun mo'ljallangan.\n\n"
        "📖 <b>Botdan foydalanish tartibi:</b>\n\n"
        "1️⃣ <b>Savolingizni yozing:</b>\n"
        "Vaziyatingizni batafsil yozing: nima bo‘ldi, qachon bo‘ldi va qo‘lingizda qanday hujjatlar mavjud.\n\n"
        "2️⃣ <b>Dastlabki huquqiy tahlil:</b>\n"
        "AI yordamchi O‘zbekiston Respublikasi qonunchiligi asosida vaziyatingizni tahlil qilib, "
        "tegishli moddalar va tartiblarni tushuntiradi.\n\n"
        "3️⃣ <b>Advokatga yo‘naltirish:</b>\n"
        "Agar ishingiz sud, da’vo arizasi yozish yoki shaxsiy himoyani talab qilsa, "
        "ma’lumotlaringiz <b>Advokat Jasurbek</b> jamoasiga yetkaziladi.\n\n"
        "🔘 <b>Buyruqlar:</b>\n"
        "• /start — Suhbatni yangidan boshlash\n"
        "• /help — Bot bo‘yicha qo‘llanma\n\n"
        "🔒 <b>Xavfsizlik eslatmasi:</b>\n"
        "<i>Bank kartasi parollari yoki maxfiy shaxsiy ma’lumotlarni yubormang.</i>\n\n"
        "👇 <b>Savolingiz bo‘lsa, to‘g‘ridan-to‘g‘ri yozib yuborishingiz mumkin!</b>"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.message(Command("stats"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_admin_stats(
    message: types.Message,
    settings: Settings,
    conversation_repo: PostgresConversationRepo,
    lead_repo: PostgresLeadRepo,
    user_repo: PostgresUserRepo,
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


@router.message(
    Command("leads", re.compile(r"^leads(?:_(\d+))?$")),
    F.chat.type.in_({"group", "supergroup"}),
)
async def cmd_admin_leads(
    message: types.Message,
    settings: Settings,
    lead_repo: PostgresLeadRepo,
    conversation_repo: PostgresConversationRepo,
    notification_registry: PostgresNotificationRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    command: CommandObject | None = None,
) -> None:
    """Admin guruhida leadlar ro'yxatini pagination bilan ko'rsatadi
    yoki bitta leadni status bilan ko'rsatadi (/leads {index} yoki /leads_{index})."""
    if message.chat.id != settings.telegram_lead_chat_id:
        return

    lead_number: int | None = None
    status_filter: str | None = None

    # Check /leads_1 or /leads 1 via CommandObject
    if command and command.regexp_match and command.regexp_match.group(1):
        try:
            lead_number = int(command.regexp_match.group(1))
        except ValueError:
            lead_number = None
    elif command and command.args:
        first_arg = command.args.split()[0]
        if first_arg.isdigit():
            lead_number = int(first_arg)
        elif first_arg.lower() in ("ochiq", "yangi", "yopiq", "yuqori"):
            status_filter = first_arg.lower()
    else:
        # Fallback text parsing
        raw_text = (message.text or "").strip()
        u_match = re.match(r"^/leads_(\d+)", raw_text)
        if u_match:
            lead_number = int(u_match.group(1))
        else:
            args = raw_text.split()
            if len(args) > 1:
                if args[1].isdigit():
                    lead_number = int(args[1])
                elif args[1].lower() in ("ochiq", "yangi", "yopiq", "yuqori"):
                    status_filter = args[1].lower()

    # Check if argument is a number (lead number) -> show detail view
    if lead_number is not None:
        async with session_factory() as session:
            user_repo = PostgresUserRepo(session)
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
    sort_by_score = False

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
    user_repo: PostgresUserRepo,
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
    conversation_repo: PostgresConversationRepo,
    lead_repo: PostgresLeadRepo,
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
    conversation_repo: PostgresConversationRepo,
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
    conversation_repo: PostgresConversationRepo,
    user_repo: PostgresUserRepo,
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

    # Concurrent message lock - prevent multiple messages being processed simultaneously
    rate_limiter = handle_message._rate_limiter
    lock_acquired = await rate_limiter.acquire_processing_lock(user.id)
    if not lock_acquired:
        await message.answer(
            "⏳ Avvalgi xabaringizga javob tayyorlanmoqda, iltimos kuting..."
        )
        return

    try:
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

        # Show "AI ga qaytish" button if conversation is in escalated state
        # Detect from the reply text pattern to avoid extra DB query
        is_escalated_reply = "mutaxassisga yuborildi" in reply_text or "AI yordamchiga qaytarildi" in reply_text
        if is_escalated_reply:
            await _safe_answer(
                message,
                reply_text,
                reply_markup=_return_to_ai_keyboard(),
            )
        else:
            await _safe_answer(message, reply_text)
    finally:
        await rate_limiter.release_processing_lock(user.id)


# ────────────────────── Admin reply handler ──────────────────────


@router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message)
async def on_admin_reply_to_notification(
    message: types.Message,
    notifier: TelegramAdminNotifier,
    conversation_repo: PostgresConversationRepo,
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

        # Admin javobini suhbat tarixiga saqlash
        user_uuid = _stable_user_id(user_telegram_id)
        conversation = await conversation_repo.get_active_for_user(user_uuid)
        if conversation:
            from domain.entities import Message as DomainMessage
            from domain.value_objects import EscalationTarget

            # Save admin message to conversation history
            admin_msg = DomainMessage.new(
                conversation_id=conversation.id,
                sender=MessageSender.ADMIN,
                text=message.text,
            )
            await conversation_repo.add_message(admin_msg)

            # Keep conversation in escalated state
            if not _is_escalated(conversation.status):
                conversation.escalate(EscalationTarget.LEAD)
                await conversation_repo.save(conversation)

            logger.info(
                "Admin reply saved and sent",
                user_id=user_telegram_id,
                conversation_id=conversation.id,
            )

        await message.reply(
            f"✅ <b>Javob {display_name} ga yuborildi.</b>",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Error sending admin reply")
        await message.reply("❌ Xabar yuborishda xatolik yuz berdi. Qayta urinib ko'ring.")
