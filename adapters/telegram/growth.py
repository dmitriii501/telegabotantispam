"""Growth: the welcome, where people come from, follow-ups, the operator's alerts, invitations and the signature.

Everything here exists to not lose a person between pressing /start and a working bot in their chat.
"""

import html
import logging
import os
import time
from datetime import datetime

from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
    WebAppInfo,
)

from adapters.telegram.common import (
    CONNECT_RIGHTS,
    DIRECT_SOURCE,
    HELP_TEXT,
    NUDGE_AFTER,
    NUDGE_HOURS_MSK,
    REFERRAL_DAYS,
    SOURCE_PATTERN,
    signature_markup,
    user_of,
)
from adapters.telegram.payments import PaymentsMixin, days_text
from core.moderation import MOSCOW
from core.storage import DAY

log = logging.getLogger(__name__)

WELCOME_TEXT = (
    "🛡 <b>DefenceAi</b> — ИИ-модератор комментариев в Telegram.\n\n"
    "Удаляю спам и ботов, а вопросы покупателей («сколько стоит», «как записаться») присылаю вам сразу, "
    "чтобы ни один не потерялся.\n\n"
    "<b>Подключение за 2 минуты:</b> нажмите «Подключить к каналу», выберите группу обсуждений и подтвердите права. "
    "14 дней бесплатно, карта не нужна. Первые дни я только записываю, что удалил бы."
)
NUDGE_TEXT = "Помочь подключить? Это 2 минуты.\nНажмите «Подключить», выберите группу обсуждений канала, остальное я возьму на себя."
NUDGE_TEXT_LAST = "Если что-то не получилось, подскажу. " + NUDGE_TEXT


def clean_source(raw: str) -> str:
    """The label of a /start link: lowercase letters, digits, `_` and `-`, up to 32 characters."""
    label = SOURCE_PATTERN.sub("", raw.strip().lower())[:32]
    return label or DIRECT_SOURCE


def parse_start(arg: str) -> tuple[str, int | None]:
    """(label, inviter) of the text after /start. `ref_<id>` is an invitation: its label is just `ref`."""
    arg = arg.strip()
    if arg.startswith("ref_"):
        raw = arg[4:]
        return "ref", int(raw) if raw.isdigit() else None
    return (clean_source(arg) if arg else DIRECT_SOURCE), None


def person(user: User | None, fallback: int | None = None) -> str:
    if user is None:
        return str(fallback or "?")
    handle = f", @{user.username}" if user.username else ""
    return f"{html.escape(user.full_name)}{handle}, id {user.id}"


class GrowthMixin(PaymentsMixin):
    """Part of the adapter: /start, /sources, /ref, follow-ups, alerts to the operator, the signature."""

    def _init_growth(self) -> None:
        alert = os.getenv("ALERT_CHAT_ID", "").strip()
        self.alert_chat: int | None = int(alert) if alert.lstrip("-").isdigit() else None

    def _register_growth(self, r) -> None:
        private = F.chat.type == "private"
        r.message(Command("sources"), private)(self.on_sources)
        r.message(Command("ref"), private)(self.on_ref)
        r.callback_query(F.data == "help:all")(self.on_help_button)

    # ----------------------------------------------------------------- links
    def connect_url(self) -> str | None:
        """Opens Telegram's "add the bot to a group" screen with the rights it needs already ticked."""
        return f"https://t.me/{self.bot_username}?startgroup=connect&admin={CONNECT_RIGHTS}" if self.bot_username else None

    def support_url(self) -> str | None:
        contact = os.getenv("SUPPORT_CONTACT", "").strip()
        if contact.startswith("@") and " " not in contact:
            return f"https://t.me/{contact[1:]}"
        if contact.startswith(("https://", "http://")):
            return contact
        return f"https://{contact}" if contact.startswith("t.me/") else None

    def welcome_markup(self) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if url := self.connect_url():
            rows.append([InlineKeyboardButton(text="➕ Подключить к каналу", url=url)])
        second = []
        if self.webapp_url:
            second.append(InlineKeyboardButton(text="⚙️ Панель", web_app=WebAppInfo(url=f"{self.webapp_url}/app/")))
        second.append(InlineKeyboardButton(text="❓ Все команды", callback_data="help:all"))
        rows.append(second)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def nudge_markup(self) -> InlineKeyboardMarkup | None:
        row = []
        if url := self.connect_url():
            row.append(InlineKeyboardButton(text="➕ Подключить", url=url))
        if url := self.support_url():
            row.append(InlineKeyboardButton(text="💬 Написать автору", url=url))
        return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None

    # ---------------------------------------------------------------- /start
    async def on_start(self, message: Message, command: CommandObject | None = None) -> None:
        arg = (command.args or "") if command else ""
        if arg.startswith("pay_") and await self.start_payment(message, arg[4:]):
            return
        user = user_of(message)
        source, inviter = parse_start(arg)
        first = await self.storage.record_start(user.id, user.full_name, user.username, source)
        log.info("start source=%s user=%s first=%s", source, user.id, first)
        if first:
            referrer = inviter if inviter and inviter != user.id and await self.storage.get_start(inviter) else None
            if referrer:
                await self.storage.add_referral(user.id, referrer)
            await self.announce_start(user, source, referrer)
        await message.answer(WELCOME_TEXT, reply_markup=self.welcome_markup())

    async def on_help_button(self, call: CallbackQuery) -> None:
        if isinstance(call.message, Message):
            await call.message.answer(HELP_TEXT)
        await call.answer()

    # ------------------------------------------------------------- operators
    async def tell_operators(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """To ALERT_CHAT_ID, or to the operators one by one when it is not set. Never raises."""
        targets = [self.alert_chat] if self.alert_chat else sorted(self.operators)
        for target in targets:
            await self.dm(target, text, markup)

    @staticmethod
    def contact_button(user: User) -> InlineKeyboardMarkup:
        url = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать", url=url)]])

    async def announce_start(self, user: User, source: str, inviter: int | None) -> None:
        if user.id in self.operators:
            return
        extra = f"\nПригласил: {inviter}" if inviter else ""
        await self.tell_operators(
            f"👋 <b>Новый /start</b>\n{person(user)}\nМетка: <code>{html.escape(source)}</code>{extra}", self.contact_button(user)
        )

    async def announce_chat(self, event: ChatMemberUpdated) -> None:
        """The bot was added to a chat it did not know: tell the operator who to help."""
        chat, adder = event.chat, event.from_user
        try:
            members = f"{await self.bot.get_chat_member_count(chat.id)} участников"
        except TelegramAPIError:
            members = "число участников неизвестно"
        start = await self.storage.get_start(adder.id) if adder else None
        origin = f"метка <code>{html.escape(start['source'])}</code>" if start else "без /start, добавили по ссылке"
        if adder and adder.id in self.operators:
            return
        await self.tell_operators(
            f"🆕 <b>Бота добавили в чат</b> «{html.escape(chat.title or str(chat.id))}»\n{members}\n"
            f"Добавил: {person(adder)}, {origin}",
            self.contact_button(adder) if adder else None,
        )

    async def coexist_note(self, adder: User | None, chat_id: int, names: str) -> None:
        """Another moderation bot is here: tell who added ours what it will and will not do next to it."""
        if adder is None or adder.is_bot:
            return
        title = html.escape(await self.chat_title(chat_id))
        await self.dm(
            adder.id,
            f"Вижу в чате «{title}» бота: {html.escape(names)}. Чтобы не мешать ему, я не пишу своё приветствие и не включаю "
            "свою капчу, а ловлю то, что он пропустил. В отчётах пилота покажу, сколько таких сообщений. "
            "Если нужно, чтобы я работал полностью, напишите в чате /coexist off.",
        )

    async def on_sources(self, message: Message, command: CommandObject) -> None:
        """/sources [days]: how many pressed /start for each label and how many of them connected a chat."""
        if not self.is_operator(message):
            return
        arg = (command.args or "").strip()
        days = int(arg) if arg.isdigit() else 0
        rows = await self.storage.sources(int(time.time()) - days * DAY if days else 0)
        title = f"за {days_text(days)}" if days else "за всё время"
        if not rows:
            await message.answer(f"📈 Источников {title} пока нет.")
            return
        lines = [f"📈 <b>Источники</b> {title}", "метка: /start → подключили"]
        starts = sum(r[1] for r in rows)
        connected = sum(r[2] for r in rows)
        for label, count, joined in rows:
            lines.append(f"<code>{html.escape(label)}</code>: {count} → {joined} ({joined * 100 // count}%)")
        lines.append(f"\n<b>Итого:</b> {starts} → {connected} ({connected * 100 // max(starts, 1)}%)")
        ref = await self.storage.referral_stats()
        lines.append(f"🎁 Рефералы: приглашено {ref['invited']}, оплатили {ref['paid']}, выдано дней {ref['days']}")
        await message.answer("\n".join(lines))

    # ------------------------------------------------------------ follow-ups
    async def send_nudges(self, now: int | None = None) -> int:
        """After /start without a connected chat: one message after an hour and one after a day, never more.
        Only in the daytime (Moscow). Returns how many were sent."""
        now = now or int(time.time())
        hour = datetime.fromtimestamp(now, MOSCOW).hour
        if not NUDGE_HOURS_MSK[0] <= hour < NUDGE_HOURS_MSK[1]:
            return 0
        await self.storage.close_nudges_of_connected()
        sent = 0
        for row in await self.storage.due_nudges(now):
            user_id = row["user_id"]
            if user_id in self.operators:
                await self.storage.set_nudges(user_id, len(NUDGE_AFTER))
                continue
            age = now - row["ts"]
            stage = sum(age >= after for after in NUDGE_AFTER)  # 1 after the first hour, 2 after a day
            if stage <= row["nudges"]:
                continue
            ok = await self.dm(user_id, NUDGE_TEXT_LAST if stage == len(NUDGE_AFTER) else NUDGE_TEXT, self.nudge_markup())
            # A person who blocked the bot gets nothing more; a stage that was skipped (a long outage) is not made up for.
            await self.storage.set_nudges(user_id, stage if ok else len(NUDGE_AFTER))
            sent += ok
        return sent

    # ------------------------------------------------------------ invitations
    async def on_ref(self, message: Message) -> None:
        user = user_of(message)
        if not self.bot_username:
            await message.answer("Ссылка пока недоступна, попробуйте позже.")
            return
        stats = await self.storage.referral_stats(user.id)
        await message.answer(
            "🎁 <b>Пригласите коллегу</b>\n"
            f"Ваша ссылка: https://t.me/{self.bot_username}?start=ref_{user.id}\n\n"
            f"Когда приглашённый оплатит первый месяц, вам обоим +{days_text(REFERRAL_DAYS)}.\n"
            f"Приглашено: {stats['invited']}, оплатили: {stats['paid']}, получено дней: {stats['days']}."
        )

    async def claim_referral(self, user_id: int | None, chat_id: int) -> None:
        """A person who was owed days before they had a chat gets them on the first chat they connect."""
        if not user_id:
            return
        days = await self.storage.claim_referral_bonus(user_id, chat_id)
        if days:
            await self.storage.add_bonus_days(chat_id, days)
            await self.dm(user_id, f"🎁 +{days_text(days)} к чату «{html.escape(await self.chat_title(chat_id))}» за приглашённого вами человека.")

    # -------------------------------------------------------------- signature
    async def signed(self, text: str, chat) -> str:
        """The bot's own words in a group end with a small link to the bot. Paid plans may switch it off."""
        if chat is not None and not chat["signature"] and (await self.access_for(chat)).state == "paid":
            return text
        return text + signature_markup(self.bot_username)
