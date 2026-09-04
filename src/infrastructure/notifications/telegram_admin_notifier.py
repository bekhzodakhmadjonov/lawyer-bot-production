"""
TelegramAdminNotifier — NotifierPort'ning Telegram orqali amalga oshirilishi.
"""

from __future__ import annotations

import re

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import Settings
from domain.entities import Conversation, Lead, User
from domain.value_objects import LeadStatus, MessageSender
from infrastructure.persistence.sqlite_conversation_repo import SQLiteConversationRepo
from infrastructure.persistence.sqlite_notification_registry import (
    SQLiteNotificationRegistry,
)
from infrastructure.security.logging_utils import mask_message, mask_telegram_id

logger = structlog.get_logger()


class TelegramAdminNotifier:
    """NotifierPort implementatsiyasi — Telegram orqali admin guruhga xabar."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        notification_registry: SQLiteNotificationRegistry,
        conversation_repo: SQLiteConversationRepo,
    ) -> None:
        self._bot = bot
        self._settings = settings
        self._registry = notification_registry
        self._conversation_repo = conversation_repo

    @property
    def _admin_chat_id(self) -> int:
        """Admin guruh ID."""
        return self._settings.telegram_lead_chat_id

    @staticmethod
    def _user_return_keyboard() -> InlineKeyboardMarkup:
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

    @staticmethod
    def _lead_status_keyboard(
        conversation: Conversation, show_history: bool = False
    ) -> InlineKeyboardMarkup:
        """Adminlar lead pipeline'ni tez belgilashi uchun tugmalar."""
        prefix = f"lead_status:{conversation.id}:"

        keyboard = [
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
        toggle_text = (
            "📖 Tarixni yashirish" if show_history else "📖 Tarixni ko'rsatish"
        )
        toggle_state = "1" if show_history else "0"
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"toggle_history:{conversation.id}:{toggle_state}",
                ),
            ]
        )

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def _display_name(user: User) -> str:
        """Foydalanuvchining ko'rsatma nomini qaytaradi (username or ID)."""
        return f"@{user.username}" if user.username else f"ID: {user.telegram_id}"

    @staticmethod
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

    def _format_chat_history(
        self, messages: tuple, *, max_length: int = 2000, show_history: bool = True
    ) -> str:
        """Format chat history with timestamps and sender labels for notifications.

        Args:
            messages: Tuple of Message entities from conversation repo
            max_length: Maximum total length to fit in notification
            show_history: If False, show brief collapsed message

        Returns:
            Formatted chat history string
        """
        if not messages:
            return ""

        # If collapsed, show brief message
        if not show_history:
            return (
                "\n💬 <b>Suhbat tarixi yashirilgan. Ko'rish uchun tugmani bosing.</b>"
            )

        lines = ["\n💬 <b>So'nggi xabarlar:</b>\n"]

        for msg in messages:
            # Determine sender emoji and label
            if msg.sender == MessageSender.USER:
                sender_label = "👤"
            elif msg.sender == MessageSender.AI:
                sender_label = "🤖"
            elif msg.sender == MessageSender.ADMIN:
                sender_label = "👨‍💼"
            else:
                sender_label = "🔧"

            # Format timestamp
            time_str = msg.sent_at.strftime("%H:%M")

            # Truncate message if too long
            text = msg.text
            if len(text) > 200:
                text = text[:197] + "..."

            # Escape HTML special characters to prevent parsing errors
            text = text.replace("&", "&amp;")
            text = text.replace("<", "&lt;")
            text = text.replace(">", "&gt;")

            lines.append(f"{sender_label} [{time_str}]: {text}")

        # Join and truncate if still too long
        result = "\n".join(lines)
        if len(result) > max_length:
            result = result[: max_length - 20] + "\n\n..."

        return result

    async def get_user_id_for_message(self, admin_message_id: int) -> int | None:
        """Admin guruhidagi xabar IDsi bo'yicha foydalanuvchi IDsini qaytaradi."""
        return await self._registry.get_user_id(admin_message_id)

    async def get_display_for_message(self, admin_message_id: int) -> str | None:
        """Admin guruhidagi xabar IDsi bo'yicha foydalanuvchi ko'rsatma nomini qaytaradi."""
        return await self._registry.get_display_name(admin_message_id)

    @staticmethod
    def extract_user_display_from_notification(text: str) -> str | None:
        """Bot xabaridan 'Kimdan:' qatoridagi ko'rsatma nomni ajratib olish (zaxira)."""
        if not text:
            return None
        match = re.search(r"Kimdan:\s*(.+)", text)
        return match.group(1).strip() if match else None

    async def _register(
        self,
        sent_message_id: int,
        user: User,
        conversation_id: str | None = None,
        notification_type: str | None = None,
    ) -> None:
        """Registry'ga saqlash: admin xabar IDsi → foydalanuvchi telegram IDsi."""
        await self._registry.save(
            message_id=sent_message_id,
            user_telegram_id=user.telegram_id,
            display_name=self._display_name(user),
            conversation_id=conversation_id,
            notification_type=notification_type,
        )

    async def notify_escalation(
        self,
        conversation: Conversation,
        user: User,
        summary: str,
        transcript: str = "",
    ) -> None:
        """Foydalanuvchi bog'lanishga rozi bo'lganda admin guruhga xabar yuboradi."""
        display_name = self._display_name(user)
        actual_name = self._actual_name(user)

        # Fetch and format chat history (collapsed by default)
        recent_messages = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=self._settings.chat_history_notification_limit
        )
        chat_history = self._format_chat_history(
            recent_messages, max_length=1500, show_history=False
        )

        text = (
            f"🔔 <b>Yangi mijoz murojaati</b>\n\n"
            f"👤 <b>Kimdan:</b> {display_name}\n"
            f"👤 <b>Ism:</b> {actual_name}\n\n"
            f"📋 <b>Mijoz anketasi:</b>\n{summary}\n\n"
            f"{chat_history}\n\n"
            f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing — "
            f"javobingiz foydalanuvchiga yuboriladi."
        )

        sent = await self._bot.send_message(
            chat_id=self._admin_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self._lead_status_keyboard(conversation, show_history=False),
        )
        await self._register(sent.message_id, user, str(conversation.id), "escalation")

    async def notify_new_lead(
        self,
        lead: Lead,
        user: User,
        conversation: Conversation,
    ) -> None:
        """Yangi lead aniqlanganda xabar yuboradi."""
        display_name = self._display_name(user)
        actual_name = self._actual_name(user)

        # Fetch and format chat history (collapsed by default)
        recent_messages = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=self._settings.chat_history_notification_limit
        )
        chat_history = self._format_chat_history(
            recent_messages, max_length=1500, show_history=False
        )

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

        sent = await self._bot.send_message(
            chat_id=self._admin_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self._lead_status_keyboard(conversation, show_history=False),
        )
        await self._register(sent.message_id, user, str(conversation.id), "new_lead")

    async def send_reply_to_user(
        self,
        user_telegram_id: int,
        reply_text: str,
    ) -> None:
        """Admin javobini foydalanuvchiga yuboradi."""
        logger.info(
            "Sending reply to user",
            user_id=mask_telegram_id(user_telegram_id),
            reply_text=mask_message(reply_text),
        )

        text = f"👨‍💼 <b>Mutaxassis javobi:</b>\n\n{reply_text}"
        await self._bot.send_message(
            chat_id=user_telegram_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self._user_return_keyboard(),
        )
        logger.info(
            "Reply sent successfully to user",
            user_id=mask_telegram_id(user_telegram_id),
        )

    async def notify_user_followup(
        self,
        conversation: Conversation,
        user: User,
        message_text: str,
    ) -> None:
        """Eskalatsiya holatidagi foydalanuvchi yangi xabar yuborganda xabar beradi."""
        display_name = self._display_name(user)
        actual_name = self._actual_name(user)

        # Fetch and format chat history (collapsed by default)
        recent_messages = await self._conversation_repo.get_recent_messages(
            conversation.id, limit=self._settings.chat_history_notification_limit
        )
        chat_history = self._format_chat_history(
            recent_messages, max_length=1500, show_history=False
        )

        text = (
            f"💬 <b>Yangi xabar (eskalatsiya holatida)</b>\n\n"
            f"👤 <b>Kimdan:</b> {display_name}\n"
            f"👤 <b>Ism:</b> {actual_name}\n\n"
            f"✉️ <b>Xabar:</b>\n{message_text}\n"
            f"{chat_history}\n"
            f"💡 <b>Javob berish uchun:</b> Shu xabarga reply qilib yozing."
        )

        # Create keyboard with toggle button
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📖 Tarixni ko'rsatish",
                        callback_data=f"toggle_history:{conversation.id}:1",
                    ),
                ],
            ]
        )

        sent = await self._bot.send_message(
            chat_id=self._admin_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await self._register(
            sent.message_id, user, str(conversation.id), "user_followup"
        )

    async def notify_returned_to_ai(
        self,
        conversation: Conversation,
    ) -> None:
        """Foydalanuvchi AI ga qaytarilganini admin ga bildiradi."""
        text = "🤖 <b>Foydalanuvchi AI yordamchiga qaytarildi</b>"
        await self._bot.send_message(
            chat_id=self._admin_chat_id,
            text=text,
            parse_mode="HTML",
        )
