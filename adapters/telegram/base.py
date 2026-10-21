"""State and helpers every part of the adapter uses."""

import asyncio
import contextlib
import html
import logging
import os
import time
from collections import deque

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from adapters.telegram.common import (
    ADMIN_CACHE_TTL,
    MAX_RETRY_WAIT,
    NOT_ADMIN,
    find_companions,
    media_label,
    user_of,
)
from core.metrics import FLOOD_WAITS
from core.models import Decision
from core.moderation import (
    ChatConfig,
    Moderator,
)
from core.ocr import OcrEngine
from core.storage import DAY, Storage

log = logging.getLogger(__name__)


class AdapterBase:
    """State and helpers every part of the adapter uses."""

    def __init__(self, bot: Bot, moderator: Moderator, storage: Storage, ocr: OcrEngine | None = None):
        self.ocr = ocr if ocr is not None else OcrEngine()
        self.bot = bot
        self.moderator = moderator
        self.storage = storage
        self._admins: dict[int, tuple[float, set[int]]] = {}
        self._companions: dict[int, str] = {}  # other moderation bots seen per chat
        self._titles: dict[int, str] = {}
        self._threads: dict[tuple[int, int], deque] = {}
        self._checked: dict[tuple[int, int], float] = {}
        self._alerted: dict[tuple[int, int], float] = {}
        self._flood: dict[tuple[int, int], deque] = {}
        self._notified: dict[tuple[int, str], float] = {}
        self._linked: dict[int, int | None] = {}
        self._tasks: set[asyncio.Task] = set()  # strong references: a bare create_task can be garbage collected
        self._trial: dict[tuple[int, str], deque] = {}
        self._joins: dict[int, deque] = {}
        self._raid_alerted: dict[int, float] = {}
        self._captcha: dict[tuple[int, int], dict] = {}  # open checks: (chat, user) -> answer, messages
        self._purged_day = ""
        self._pilot_tried: dict[int, str] = {}  # chat -> the day a trial report was last attempted
        self.heartbeat = time.time()  # refreshed by the daily-job loop every minute; /health reads it
        self.webapp_url = os.getenv("WEBAPP_URL", "").rstrip("/")

    # ------------------------------------------------------------------ helpers
    async def is_admin(self, chat_id: int, user_id: int, fresh: bool = False) -> bool:
        cached = self._admins.get(chat_id)
        if fresh or not cached or time.time() - cached[0] > ADMIN_CACHE_TTL:
            try:
                members = await self.bot.get_chat_administrators(chat_id)
            except TelegramAPIError:
                return False
            cached = (time.time(), {m.user.id for m in members})
            self._admins[chat_id] = cached
            await self.storage.remember_admins(chat_id, cached[1])
            await self._note_companions(chat_id, find_companions(members))
        return user_id in cached[1]

    async def _note_companions(self, chat_id: int, names: str) -> None:
        """Other moderation bots are admins too: the list of admins is where they show up."""
        if self._companions.get(chat_id) != names:
            self._companions[chat_id] = names
            await self.storage.set_companions(chat_id, names)

    async def detect_companions(self, chat_id: int) -> str:
        """Look at the chat's admins now and return the names of the other moderation bots ("Combot, Rose")."""
        await self.is_admin(chat_id, 0, fresh=True)
        return self._companions.get(chat_id, "")

    async def is_admin_message(self, message: Message) -> bool:
        # Anonymous admins post on behalf of the group itself.
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True
        if not message.from_user or not await self.is_admin(message.chat.id, user_of(message).id):
            return False
        await self.storage.claim_owner(message.chat.id, user_of(message).id)
        return True

    @property
    def sees_images(self) -> bool:
        """Pictures can be judged: by the AI, or by the local text reader."""
        return self.moderator.jev.vision or self.ocr.available

    @staticmethod
    def shown_text(message: Message, decision: Decision) -> str | None:
        text = message.text or message.caption
        image_text = decision.extra.get("image_text")
        if text:
            return text
        label = media_label(message) or "[картинка]"
        if image_text:
            return f"{label} с надписью: {image_text[:400]}"
        return f"{label} (ИИ смотрел картинку)" if decision.extra.get("image") else None

    def trial_slot(self, chat_id: int, kind: str, limit: int) -> int | None:
        """Use one of today's `limit` trial-mode messages; returns how many are left, None when none were."""
        now = time.time()
        stamps = self._trial.setdefault((chat_id, kind), deque())
        while stamps and now - stamps[0] > DAY:
            stamps.popleft()
        if len(stamps) >= limit:
            return None
        stamps.append(now)
        return limit - len(stamps)

    async def dm(self, user_id: int | None, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        if not user_id:
            return False
        try:
            await self.bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
            return True
        except TelegramAPIError as e:
            log.info("Cannot DM %s: %s", user_id, e)
            return False

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def notify_owner(self, chat, key: str, text: str, cooldown: float, markup=None) -> None:
        """Message the chat owner, but not more often than once per `cooldown` for the same `key`."""
        owner_id = chat["owner_id"] if chat else None
        if not owner_id:
            return
        ck = (chat["chat_id"], key)
        now = time.time()
        if now - self._notified.get(ck, 0) < cooldown:
            return
        self._notified[ck] = now
        await self.dm(owner_id, f"{text}\n\nЧат: «{html.escape(await self.chat_title(chat['chat_id']))}»", markup)

    async def linked_channel(self, chat_id: int) -> int | None:
        """The channel this discussion group belongs to (its own comments are never moderated)."""
        if chat_id not in self._linked:
            try:
                self._linked[chat_id] = (await self.bot.get_chat(chat_id)).linked_chat_id
            except TelegramAPIError:
                return None
        return self._linked[chat_id]

    async def delete_later(self, chat_id: int, message_id: int, delay: int) -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(TelegramAPIError):
            await self.bot.delete_message(chat_id, message_id)

    async def check_rights(self, chat_id: int) -> list[str]:
        me = await self.bot.get_chat_member(chat_id, self.bot.id)
        if me.status != ChatMemberStatus.ADMINISTRATOR:
            return [NOT_ADMIN]
        missing = []
        if not getattr(me, "can_delete_messages", False):
            missing.append("удалять сообщения")
        if not getattr(me, "can_restrict_members", False):
            missing.append("банить и ограничивать пользователей")
        return missing

    async def chat_title(self, chat_id: int) -> str:
        if chat_id not in self._titles:
            try:
                self._titles[chat_id] = (await self.bot.get_chat(chat_id)).title or str(chat_id)
            except TelegramAPIError:
                return str(chat_id)
        return self._titles[chat_id]

    def panel_markup(self, chat_id: int | None = None) -> InlineKeyboardMarkup | None:
        if not self.webapp_url:
            return None
        url = f"{self.webapp_url}/app/" + (f"?chat={chat_id}" if chat_id else "")
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть панель", web_app=WebAppInfo(url=url))]]
        )

    async def set_menu_button(self) -> None:
        if not self.webapp_url:
            return
        try:
            await self.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Панель", web_app=WebAppInfo(url=f"{self.webapp_url}/app/"))
            )
        except TelegramAPIError as e:
            log.warning("Cannot set menu button: %s", e)

    async def config(self, chat_id: int) -> ChatConfig:
        return ChatConfig.from_row(await self.storage.get_chat(chat_id))

    async def admin_args(self, message: Message, command: CommandObject) -> str | None:
        """Arguments of an admin-only command, or None when the sender is not an admin."""
        if not await self.is_admin_message(message):
            return None
        return (command.args or "").strip().lower()

    @staticmethod
    async def tg_retry(call, *args, **kwargs):
        """Run a Telegram request; when Telegram asks to slow down (flood control), wait and try once more.
        Without it a spam raid loses its removals exactly when they matter most."""
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            if e.retry_after > MAX_RETRY_WAIT:
                raise
            FLOOD_WAITS.inc()
            log.warning("Telegram flood control: waiting %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 0.5)
            return await call(*args, **kwargs)
