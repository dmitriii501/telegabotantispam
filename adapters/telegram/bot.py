"""Telegram adapter: turns group messages into `Comment`s and executes decisions.

The bot works in a channel's discussion group (where channel comments live)
and in ordinary groups. It needs admin rights to delete messages and ban users.
"""

import asyncio
import html
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from core.jev_client import JevError
from core.models import DELETING_ACTIONS, ActionType, Author, Comment, Decision
from core.moderation import MODES, TENSION_ALERT, TENSION_MIN_CONFIDENCE, ChatConfig, Moderator
from core.normalizer import example_key
from core.rules import CONTEXT, EVERYTHING, PERMISSION, Rule, rules_to_json
from core.storage import DAY, Storage

log = logging.getLogger(__name__)

GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
EXPLANATION_TTL = 60  # seconds before the "comment deleted" notice disappears
ADMIN_CACHE_TTL = 600
DIGEST_HOUR_UTC = int(os.getenv("DIGEST_HOUR_UTC", "6"))  # 06:00 UTC = 09:00 Moscow

# Conflict alerts: a busy thread is scored by Jev, at most every RECHECK seconds.
CONFLICT_WINDOW = 15 * 60  # only messages this recent count
CONFLICT_MIN_MESSAGES = 6
CONFLICT_RECHECK = 5 * 60
CONFLICT_COOLDOWN = 60 * 60  # do not warn about the same thread twice in an hour
THREAD_BUFFER = 30
AUTO_REMEMBER_CONFIDENCE = 0.95  # bans this sure are remembered, so copies of the spam skip Jev

ACTION_LABEL = {
    "ban": "удалять и банить",
    "mute": "удалять и мутить на сутки",
    "warn": "удалять с предупреждением",
    "delete": "просто удалять",
    "review": "присылать вам на проверку",
    None: "по ситуации: человеку объясню, бота уберу молча",
}
DECISION_LABEL = {
    ActionType.NONE: "оставил бы",
    ActionType.DELETE_AND_BAN: "удалил бы и забанил",
    ActionType.DELETE_AND_MUTE: "удалил бы и замутил на сутки",
    ActionType.DELETE_SILENT: "удалил бы молча",
    ActionType.DELETE_AND_EXPLAIN: "удалил бы и объяснил автору",
    ActionType.SEND_TO_REVIEW: "прислал бы вам на проверку",
    ActionType.FORWARD_USEFUL: "переслал бы вам как полезный",
}
MODE_LABEL = {
    "soft": "мягкий: сомнительное присылаю вам",
    "normal": "обычный",
    "strict": "строгий: удаляю при меньшей уверенности",
}

HELP_TEXT = (
    "Я модерирую комментарии с помощью ИИ: понимаю смысл, а не ищу стоп-слова, "
    "поэтому ловлю рекламу, замаскированную латиницей и цифрами.\n\n"
    "<b>Как подключить</b>\n"
    "1. Добавьте меня в группу обсуждений канала (или в обычную группу).\n"
    "2. Сделайте меня админом с правами удалять сообщения и банить.\n"
    "3. Напишите в группе правила обычным языком, можно сразу с действиями:\n"
    "<code>/rules Канал про крипту. Мат можно. Рекламу нельзя — бан. "
    "Политику — просто удалять. Оскорбления — предупреждать.</code>\n"
    "4. Подтвердите, что я правильно понял.\n\n"
    "Спам я удаляю всегда, даже без правил. Спорные комментарии, апелляции и сводки "
    "присылаю в личку тому, кто подтвердил правила, поэтому напишите мне /start.\n\n"
    "<b>Команды в группе (только для админов)</b>\n"
    "/rules [текст] — задать или показать правила\n"
    "/settings — все настройки\n"
    "/mode soft|normal|strict — строгость\n"
    "/lockdown on|off — режим тишины: удалять всё от не-админов\n"
    "/escalation on|off — мут и бан за повторные нарушения\n"
    "/useful digest|instant|off — полезные комментарии: в сводку, сразу или никак\n"
    "/digest on|off — ежедневная сводка\n"
    "/conflicts on|off — предупреждать, когда обсуждение накаляется\n"
    "/check текст — что бы я сделал с таким комментарием\n"
    "/trust, /untrust — ответом на сообщение: не проверять этого человека\n"
    "/panel — веб-панель: правила, настройки, журнал\n"
    "/stats — статистика за сутки, /off и /on — пауза"
)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def chat_message_link(chat_id: int, message_id: int, username: str | None = None) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}"


def quote(text: str, limit: int = 700) -> str:
    text = text if len(text) <= limit else text[:limit] + "…"
    return f"<blockquote>{html.escape(text)}</blockquote>"


def describe_rule(rule: Rule) -> str:
    text = html.escape(rule.text.rstrip(".!; "))
    if rule.kind == PERMISSION:
        return f"✅ {text}"
    if rule.kind == CONTEXT:
        return f"ℹ️ {text}"
    if rule.kind == EVERYTHING:
        return f"🚫 {text} — {ACTION_LABEL[rule.action or 'delete']} <b>все комментарии</b>"
    return f"❌ {text} — {ACTION_LABEL[rule.action]}"


def describe_rules(rules: list[Rule]) -> str:
    return "\n".join(describe_rule(r) for r in rules) or "правила не заданы"


def on_off(value: bool) -> str:
    return "включено" if value else "выключено"


class TelegramAdapter:
    def __init__(self, bot: Bot, moderator: Moderator, storage: Storage):
        self.bot = bot
        self.moderator = moderator
        self.storage = storage
        self._admins: dict[int, tuple[float, set[int]]] = {}
        self._titles: dict[int, str] = {}
        self._threads: dict[tuple[int, int], deque] = {}
        self._checked: dict[tuple[int, int], float] = {}
        self._alerted: dict[tuple[int, int], float] = {}
        self.webapp_url = os.getenv("WEBAPP_URL", "").rstrip("/")
        self.router = Router()
        self._register()

    def dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        dp.include_router(self.router)
        return dp

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
        return user_id in cached[1]

    async def is_admin_message(self, message: Message) -> bool:
        # Anonymous admins post on behalf of the group itself.
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True
        return bool(message.from_user) and await self.is_admin(message.chat.id, message.from_user.id)

    async def dm(self, user_id: int | None, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        if not user_id:
            return False
        try:
            await self.bot.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)
            return True
        except TelegramAPIError as e:
            log.info("Cannot DM %s: %s", user_id, e)
            return False

    async def delete_later(self, chat_id: int, message_id: int, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await self.bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass

    async def check_rights(self, chat_id: int) -> list[str]:
        me = await self.bot.get_chat_member(chat_id, self.bot.id)
        if me.status != ChatMemberStatus.ADMINISTRATOR:
            return ["я не админ"]
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
        url = f"{self.webapp_url}/" + (f"?chat={chat_id}" if chat_id else "")
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть панель", web_app=WebAppInfo(url=url))]]
        )

    async def set_menu_button(self) -> None:
        if not self.webapp_url:
            return
        try:
            await self.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Панель", web_app=WebAppInfo(url=f"{self.webapp_url}/"))
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

    # ----------------------------------------------------------------- handlers

    def _register(self) -> None:
        r = self.router
        r.message(CommandStart(), F.chat.type == ChatType.PRIVATE)(self.on_start)
        r.my_chat_member()(self.on_bot_added)
        r.message(Command("rules"), GROUPS)(self.on_rules)
        r.message(Command("stats"), GROUPS)(self.on_stats)
        r.message(Command("settings"), GROUPS)(self.on_settings)
        r.message(Command("mode"), GROUPS)(self.on_mode)
        r.message(Command("lockdown"), GROUPS)(self.on_lockdown)
        r.message(Command("escalation"), GROUPS)(self.on_escalation)
        r.message(Command("digest"), GROUPS)(self.on_digest)
        r.message(Command("useful"), GROUPS)(self.on_useful)
        r.message(Command("check"), GROUPS)(self.on_check)
        r.message(Command("trust", "untrust"), GROUPS)(self.on_trust)
        r.message(Command("conflicts"), GROUPS)(self.on_conflicts)
        r.message(Command("panel"))(self.on_panel)
        r.message(Command("off", "on"), GROUPS)(self.on_toggle)
        r.callback_query(F.data.startswith("rules:"))(self.on_rules_button)
        r.callback_query(F.data.startswith("appeal:"))(self.on_appeal)
        r.callback_query(F.data.startswith("ap:"))(self.on_appeal_decision)
        r.callback_query(F.data.startswith("rev:"))(self.on_review_decision)
        r.message(GROUPS)(self.on_group_message)

    async def on_start(self, message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=self.panel_markup())

    async def on_panel(self, message: Message) -> None:
        if not self.webapp_url:
            await message.reply("Веб-панель пока не настроена.")
            return
        if message.chat.type == ChatType.PRIVATE:
            await message.answer("Панель управления:", reply_markup=self.panel_markup())
            return
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or not await self.is_admin_message(message):
            return
        sent = await self.dm(message.from_user.id, "Панель управления этим чатом:", self.panel_markup(message.chat.id))
        await message.reply(
            "Отправил ссылку на панель вам в личку." if sent else "Напишите мне /start в личку, и повторите команду."
        )

    async def on_bot_added(self, event: ChatMemberUpdated) -> None:
        if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return
        if event.new_chat_member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
            return
        await self.storage.ensure_chat(event.chat.id)
        missing = await self.check_rights(event.chat.id)
        if missing:
            text = "Привет! Чтобы модерировать, мне нужны права админа: " + ", ".join(missing) + "."
        else:
            text = (
                "Готов к работе. Спам удаляю уже сейчас.\n"
                "Задайте правила обычным языком: <code>/rules мат можно, рекламу нельзя — бан</code>"
            )
        try:
            await self.bot.send_message(event.chat.id, text)
        except TelegramAPIError:
            pass

    # ------------------------------------------------------------------- rules

    async def on_rules(self, message: Message, command: CommandObject) -> None:
        if not await self.is_admin_message(message):
            return
        chat_id = message.chat.id
        if not command.args:
            config = await self.config(chat_id)
            await message.reply(
                f"<b>Правила</b>\n{describe_rules(config.rules)}\n❌ спам — всегда\n\n"
                "Изменить: <code>/rules текст</code>"
            )
            return
        text = command.args.strip()
        try:
            rules = await self.moderator.parse_rules(text)
        except JevError as e:
            log.error("Rules parsing failed: %s", e)
            await message.reply("Не получилось разобрать правила: ИИ временно недоступен. Попробуйте позже.")
            return
        if not rules:
            await message.reply("Не вижу правил в этом тексте. Напишите, например: <code>/rules рекламу нельзя</code>")
            return
        await self.storage.set_pending_rules(chat_id, text, rules_to_json(rules))
        await message.reply(
            f"Понял так:\n{describe_rules(rules)}\n❌ спам — всегда\n\n"
            "Если в правиле не названо действие, решаю сам: человеку объясняю, бота убираю молча. "
            "Всё верно?",
            reply_markup=kb([("✅ Всё верно", f"rules:ok:{chat_id}"), ("✏️ Исправить", f"rules:no:{chat_id}")]),
        )

    async def on_rules_button(self, call: CallbackQuery) -> None:
        _, verdict, chat_id = call.data.split(":")
        chat_id = int(chat_id)
        if not await self.is_admin(chat_id, call.from_user.id):
            await call.answer("Это могут сделать только админы.", show_alert=True)
            return
        if verdict == "ok":
            if not await self.storage.confirm_rules(chat_id, call.from_user.id):
                await call.answer("Эти правила уже неактуальны.")
                return
            reached = await self.dm(
                call.from_user.id, "Правила сохранены. Сюда буду присылать спорные комментарии и ежедневную сводку."
            )
            note = "" if reached else "\n\nНапишите мне /start в личку, чтобы получать спорные комментарии и сводки."
            await call.message.edit_text("Правила сохранены ✅" + note)
        else:
            await self.storage.discard_pending_rules(chat_id)
            await call.message.edit_text("Хорошо, напишите правила заново: <code>/rules текст</code>")
        await call.answer()

    # ---------------------------------------------------------------- settings

    async def on_settings(self, message: Message) -> None:
        if not await self.is_admin_message(message):
            return
        chat = await self.storage.get_chat(message.chat.id)
        config = ChatConfig.from_row(chat)
        useful = {"digest": "в ежедневную сводку", "instant": "сразу в личку", "off": "не присылать"}
        await message.reply(
            "<b>Настройки</b>\n"
            f"Модерация: {on_off(not chat or bool(chat['enabled']))}\n"
            f"Строгость: {MODE_LABEL[config.mode]}\n"
            f"Режим тишины: {on_off(config.lockdown)}\n"
            f"Мут и бан за повторные нарушения: {on_off(config.escalation)}\n"
            f"Полезные комментарии: {useful[chat['useful_mode'] if chat else 'digest']}\n"
            f"Ежедневная сводка: {on_off(not chat or bool(chat['digest']))}\n"
            f"Предупреждения о ссорах: {on_off(config.conflicts)}\n\n"
            f"<b>Правила</b>\n{describe_rules(config.rules)}\n❌ спам — всегда"
        )

    async def on_mode(self, message: Message, command: CommandObject) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        if arg not in MODES:
            await message.reply("Выберите: <code>/mode soft</code>, <code>/mode normal</code> или <code>/mode strict</code>")
            return
        await self.storage.set_setting(message.chat.id, "mode", arg)
        await message.reply(f"Строгость: {MODE_LABEL[arg]}")

    async def _switch(self, message: Message, command: CommandObject, key: str, title: str) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        if arg not in ("on", "off"):
            await message.reply(f"Напишите <code>/{command.command} on</code> или <code>/{command.command} off</code>")
            return
        await self.storage.set_setting(message.chat.id, key, int(arg == "on"))
        await message.reply(f"{title}: {on_off(arg == 'on')}")

    async def on_lockdown(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "lockdown", "Режим тишины")

    async def on_escalation(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "escalation", "Мут и бан за повторные нарушения")

    async def on_conflicts(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "conflicts", "Предупреждения о ссорах")

    async def on_digest(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "digest", "Ежедневная сводка")

    async def on_useful(self, message: Message, command: CommandObject) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        if arg not in ("digest", "instant", "off"):
            await message.reply("Выберите: <code>/useful digest</code> (в сводку), <code>instant</code> (сразу) или <code>off</code>")
            return
        await self.storage.set_setting(message.chat.id, "useful_mode", arg)
        await message.reply("Готово.")

    async def on_toggle(self, message: Message, command: CommandObject) -> None:
        if not await self.is_admin_message(message):
            return
        enabled = command.command == "on"
        await self.storage.set_enabled(message.chat.id, enabled)
        await message.reply("Модерация включена ✅" if enabled else "Модерация выключена ⏸")

    async def on_trust(self, message: Message, command: CommandObject) -> None:
        if not await self.is_admin_message(message):
            return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target:
            await message.reply("Ответьте этой командой на сообщение человека.")
            return
        trusted = command.command == "trust"
        await self.storage.set_trusted(message.chat.id, target.id, trusted, target.full_name)
        name = html.escape(target.full_name)
        await message.reply(f"{name}: {'больше не проверяю' if trusted else 'снова проверяю'}.")

    async def on_check(self, message: Message, command: CommandObject) -> None:
        if not await self.is_admin_message(message):
            return
        text = (command.args or "").strip()
        if not text:
            await message.reply("Напишите текст: <code>/check текст комментария</code>")
            return
        user = message.from_user
        comment = Comment(
            message.chat.id, message.message_id, text,
            Author(user.id if user else 0, user.full_name if user else "Админ", previous_messages=3),
        )
        try:
            d = await self.moderator.check(comment, await self.config(message.chat.id))
        except JevError as e:
            await message.reply(f"ИИ недоступен: {html.escape(str(e))}")
            return
        v = d.verdict
        lines = [f"Я бы: <b>{DECISION_LABEL[d.action]}</b>"]
        if v:
            lines.append(f"Категория: {v.category.value}, уверенность {v.confidence:.0%}, шанс что это бот {v.bot_probability:.0%}")
        if d.reason:
            lines.append(f"Причина: {html.escape(d.reason)}")
        lines.append("(Проверка без последствий: ничего не удалено.)")
        await message.reply("\n".join(lines))

    async def on_stats(self, message: Message) -> None:
        if not await self.is_admin_message(message):
            return
        s = await self.storage.stats(message.chat.id, int(time.time()) - DAY)
        checked = sum(v for k, v in s.items() if k != "tokens")
        deleted = sum(s.get(a.value, 0) for a in DELETING_ACTIONS)
        await message.reply(
            "<b>За сутки</b>\n"
            f"Проверено комментариев: {checked}\n"
            f"Удалено: {deleted} (из них с баном: {s.get(ActionType.DELETE_AND_BAN.value, 0)})\n"
            f"Отправлено вам на проверку: {s.get(ActionType.SEND_TO_REVIEW.value, 0)}\n"
            f"Полезных: {s.get(ActionType.FORWARD_USEFUL.value, 0)}\n"
            f"Токенов Jev: {s['tokens']}"
        )

    # --------------------------------------------------------------- moderation

    async def remember_post(self, message: Message) -> None:
        text = message.text or message.caption
        if text:
            await self.storage.add_post(message.chat.id, message.message_id, text)

    async def comment_context(self, message: Message) -> tuple[str | None, str | None]:
        """Text of the channel post the comment is under, and of the comment it replies to."""
        chat_id = message.chat.id
        post = None
        thread = message.message_thread_id
        if thread:
            post = await self.storage.get_post(chat_id, thread)
        parent = message.reply_to_message
        reply_to = None
        if parent:
            parent_text = parent.text or parent.caption
            if parent.is_automatic_forward:
                post = post or parent_text
            elif parent.message_id != thread:
                reply_to = parent_text
        return post, reply_to

    async def on_group_message(self, message: Message) -> None:
        if message.is_automatic_forward:
            await self.remember_post(message)
            return
        text = message.text or message.caption
        if not text or not message.from_user:
            return
        if message.sender_chat or message.from_user.is_bot:
            return  # channel posts, anonymous admins, other bots
        chat_id = message.chat.id
        chat = await self.storage.get_chat(chat_id)
        if chat and not chat["enabled"]:
            return
        user = message.from_user
        if await self.is_admin(chat_id, user.id) or await self.storage.is_trusted(chat_id, user.id):
            return

        messages, deletions = await self.storage.user_counters(chat_id, user.id)
        config = ChatConfig.from_row(chat)
        key = example_key(text)
        known = await self.storage.example_kind(chat_id, key) if key else None
        post_text, reply_to_text = (None, None)
        if not config.lockdown and not config.blanket:
            post_text, reply_to_text = await self.comment_context(message)
        comment = Comment(
            chat_id=chat_id,
            message_id=message.message_id,
            text=text,
            author=Author(
                user_id=user.id,
                display_name=user.full_name,
                username=user.username,
                is_premium=bool(user.is_premium),
                previous_messages=messages,
                previous_deletions=deletions,
            ),
            post_text=post_text,
            reply_to_text=reply_to_text,
        )
        try:
            decision = await self.moderator.check(comment, config, known)
        except JevError as e:
            log.error("Moderation failed, letting the comment through: %s", e)
            return

        deleted = decision.action in DELETING_ACTIONS
        await self.storage.count_message(chat_id, user.id, deleted)
        if not deleted:
            asyncio.create_task(self.watch_thread(message, text, config, chat))
        if decision.verdict is None:
            return  # skipped without calling Jev
        v = decision.verdict
        log_id = await self.storage.add_log(
            chat_id=chat_id,
            message_id=message.message_id,
            user_id=user.id,
            user_name=user.full_name,
            text=text,
            category=v.category.value,
            confidence=v.confidence,
            bot_probability=v.bot_probability,
            action=decision.action.value,
            reason=decision.reason,
            tokens=v.input_tokens,
        )
        log.info("chat=%s user=%s action=%s %s", chat_id, user.id, decision.action.value, v)
        if key and decision.action == ActionType.DELETE_AND_BAN and v.confidence >= AUTO_REMEMBER_CONFIDENCE:
            await self.storage.add_example(chat_id, key, "remove")
        await self.execute(message, decision, log_id, chat)

    # ---------------------------------------------------------- conflict alerts

    @staticmethod
    def thread_of(message: Message) -> int:
        parent = message.reply_to_message
        if message.message_thread_id:
            return message.message_thread_id
        return parent.message_id if parent and parent.is_automatic_forward else 0

    def _prune_threads(self, now: float) -> None:
        for key in [k for k, buf in self._threads.items() if not buf or now - buf[-1][0] > 2 * 3600]:
            self._threads.pop(key, None)
            self._checked.pop(key, None)
            self._alerted.pop(key, None)

    async def watch_thread(self, message: Message, text: str, config: ChatConfig, chat) -> None:
        """Remember the message and, when the thread is busy, ask Jev whether it is heating up."""
        try:
            chat_id = message.chat.id
            thread = (chat_id, self.thread_of(message))
            now = time.time()
            buf = self._threads.setdefault(thread, deque(maxlen=THREAD_BUFFER))
            buf.append((now, message.from_user.full_name, text[:300]))
            if len(self._threads) > 500:
                self._prune_threads(now)
            owner_id = chat["owner_id"] if chat else None
            if not config.conflicts or not owner_id:
                return
            recent = [item for item in buf if now - item[0] <= CONFLICT_WINDOW]
            if (
                len(recent) < CONFLICT_MIN_MESSAGES
                or now - self._checked.get(thread, 0) < CONFLICT_RECHECK
                or now - self._alerted.get(thread, 0) < CONFLICT_COOLDOWN
            ):
                return
            self._checked[thread] = now
            score, confidence = await self.moderator.tension(
                [{"author": name, "text": t} for _, name, t in recent[-15:]]
            )
            if score < TENSION_ALERT or confidence < TENSION_MIN_CONFIDENCE:
                return
            self._alerted[thread] = now
            post = await self.storage.get_post(chat_id, thread[1]) if thread[1] else None
            where = f" под постом «{html.escape(post[:80])}…»" if post else ""
            people = len({name for _, name, _ in recent})
            link = chat_message_link(chat_id, message.message_id, message.chat.username)
            await self.dm(
                owner_id,
                f"🔥 Разгорается конфликт{where}: {len(recent)} сообщений от {people} участников за "
                f"{CONFLICT_WINDOW // 60} минут, тон резкий.\n{link}\n\n"
                "Если нужно остановить, включите режим тишины: /lockdown on",
            )
        except Exception:  # background task: never let it crash the handler
            log.exception("Conflict check failed")

    async def execute(self, message: Message, decision: Decision, log_id: int, chat) -> None:
        action = decision.action
        owner_id = chat["owner_id"] if chat else None
        useful_mode = chat["useful_mode"] if chat else "digest"
        user = message.from_user
        shown = message.text or message.caption
        try:
            if action in DELETING_ACTIONS:
                await message.delete()
            if action == ActionType.DELETE_AND_BAN:
                await self.bot.ban_chat_member(message.chat.id, user.id)
            elif action in (ActionType.DELETE_AND_EXPLAIN, ActionType.DELETE_AND_MUTE):
                hours = decision.extra.get("mute_hours")
                if hours:
                    await self.bot.restrict_chat_member(
                        message.chat.id,
                        user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=timedelta(hours=hours),
                    )
                mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'
                tail = f" Писать в чате можно будет через {hours} ч." if hours else ""
                notice = await self.bot.send_message(
                    message.chat.id,
                    f"{mention}, ваш комментарий удалён: {html.escape(decision.reason)}.{tail}",
                    reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
                    reply_markup=kb([("Не согласен — оспорить", f"appeal:{log_id}")]),
                )
                asyncio.create_task(self.delete_later(message.chat.id, notice.message_id, EXPLANATION_TTL))
            elif action == ActionType.SEND_TO_REVIEW:
                v = decision.verdict
                await self.dm(
                    owner_id,
                    f"🤔 Комментарий от {html.escape(user.full_name)} "
                    f"({html.escape(decision.reason)}, уверенность {v.confidence:.0%}):\n"
                    f"{quote(shown)}\n{chat_message_link(message.chat.id, message.message_id, message.chat.username)}",
                    kb([("🗑 Удалить", f"rev:del:{log_id}"), ("✅ Оставить", f"rev:keep:{log_id}")]),
                )
            elif action == ActionType.FORWARD_USEFUL and useful_mode == "instant":
                await self.dm(
                    owner_id,
                    f"💡 Полезный комментарий от {html.escape(user.full_name)}:\n"
                    f"{quote(shown)}\n{chat_message_link(message.chat.id, message.message_id, message.chat.username)}",
                )
        except TelegramAPIError as e:
            log.warning("Action %s failed in chat %s: %s", action.value, message.chat.id, e)

    # ------------------------------------------------------------------ appeals

    async def on_appeal(self, call: CallbackQuery) -> None:
        log_id = int(call.data.split(":")[1])
        entry = await self.storage.get_log(log_id)
        if not entry or entry["user_id"] != call.from_user.id:
            await call.answer("Оспорить может только автор комментария.", show_alert=True)
            return
        if entry["feedback"]:
            await call.answer("Решение по этому комментарию уже принято.", show_alert=True)
            return
        if not await self.storage.can_appeal(call.from_user.id):
            await call.answer("Оспорить можно не чаще раза в сутки.", show_alert=True)
            return
        chat = await self.storage.get_chat(entry["chat_id"])
        sent = await self.dm(
            chat["owner_id"] if chat else None,
            f"⚖️ {html.escape(entry['user_name'] or '')} оспаривает удаление "
            f"({html.escape(entry['reason'] or '')}):\n{quote(entry['text'])}",
            kb([("↩️ Вернуть комментарий", f"ap:ok:{log_id}"), ("🗑 Удаление верное", f"ap:no:{log_id}")]),
        )
        if not sent:
            await call.answer("Не удалось связаться с админом. Попробуйте позже.", show_alert=True)
            return
        await self.storage.add_appeal(call.from_user.id, log_id)
        await call.answer("Апелляция отправлена админу.", show_alert=True)

    async def _owner_entry(self, call: CallbackQuery):
        log_id = int(call.data.split(":")[2])
        entry = await self.storage.get_log(log_id)
        chat = await self.storage.get_chat(entry["chat_id"]) if entry else None
        if not entry or not chat or chat["owner_id"] != call.from_user.id:
            await call.answer("Нет доступа.", show_alert=True)
            return None
        if entry["feedback"]:
            await call.answer("Уже решено.")
            return None
        return entry

    async def _lift_restrictions(self, chat_id: int, user_id: int) -> None:
        try:
            await self.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
        except TelegramAPIError as e:
            log.warning("Cannot lift restrictions: %s", e)

    async def restore_comment(self, entry) -> None:
        """The admin says the deletion was wrong: repost the text and undo the penalties."""
        chat_id, user_id = entry["chat_id"], entry["user_id"]
        await self.storage.set_feedback(entry["id"], "not_spam")
        await self.storage.reset_deletions(chat_id, user_id)
        if entry["action"] == ActionType.DELETE_AND_MUTE.value:
            await self._lift_restrictions(chat_id, user_id)
        elif entry["action"] == ActionType.DELETE_AND_BAN.value:
            try:
                await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            except TelegramAPIError as e:
                log.warning("Cannot unban: %s", e)
        # A deleted message cannot be restored, so the bot reposts its text.
        try:
            await self.bot.send_message(
                chat_id,
                f"↩️ Комментарий {html.escape(entry['user_name'] or '')} восстановлен админом:\n{quote(entry['text'])}",
            )
        except TelegramAPIError as e:
            log.warning("Cannot restore comment: %s", e)

    async def resolve_review(self, entry, delete: bool) -> str:
        """The admin decides on a comment the bot was unsure about."""
        if not delete:
            await self.storage.set_feedback(entry["id"], "not_spam")
            return "✅ Оставлено."
        await self.storage.set_feedback(entry["id"], "confirmed")
        try:
            await self.bot.delete_message(entry["chat_id"], entry["message_id"])
            await self.storage.count_message(entry["chat_id"], entry["user_id"], deleted=True)
            return "🗑 Удалено."
        except TelegramAPIError:
            return "Не удалось удалить: сообщение уже удалено или прошло больше 48 часов."

    async def on_appeal_decision(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        if call.data.startswith("ap:ok"):
            await self.restore_comment(entry)
            await call.message.edit_text(call.message.html_text + "\n\n↩️ Комментарий восстановлен.")
        else:
            await self.storage.set_feedback(entry["id"], "confirmed")
            await call.message.edit_text(call.message.html_text + "\n\n🗑 Удаление подтверждено.")
        await call.answer()

    async def on_review_decision(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        result = await self.resolve_review(entry, call.data.startswith("rev:del"))
        await call.message.edit_text(call.message.html_text + "\n\n" + result)
        await call.answer()

    # ------------------------------------------------------------------- digest

    async def build_digest(self, chat_id: int, since: int) -> str | None:
        s = await self.storage.stats(chat_id, since)
        checked = sum(v for k, v in s.items() if k != "tokens")
        if not checked:
            return None
        deleted = sum(s.get(a.value, 0) for a in DELETING_ACTIONS)
        try:
            title = html.escape((await self.bot.get_chat(chat_id)).title or "")
        except TelegramAPIError:
            title = ""
        lines = [
            f"📊 <b>Сводка за сутки</b> {title}".rstrip(),
            f"Проверено комментариев: {checked}",
            f"Удалено: {deleted}, из них с баном: {s.get(ActionType.DELETE_AND_BAN.value, 0)}",
        ]
        review = s.get(ActionType.SEND_TO_REVIEW.value, 0)
        if review:
            lines.append(f"Отправлено вам на проверку: {review}")
        reasons = await self.storage.top_reasons(chat_id, since)
        if reasons:
            lines.append("\n<b>Чаще всего удалял:</b>")
            lines += [f"• {html.escape(reason)} — {n}" for reason, n in reasons]
        useful = await self.storage.useful_comments(chat_id, since)
        if useful:
            lines.append("\n💡 <b>Полезные комментарии:</b>")
            for row in useful:
                snippet = row["text"] if len(row["text"]) <= 200 else row["text"][:200] + "…"
                link = chat_message_link(chat_id, row["message_id"])
                lines.append(f"• {html.escape(row['user_name'] or '')}: {html.escape(snippet)}\n{link}")
        return "\n".join(lines)

    async def send_digests(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        for chat in await self.storage.digest_chats():
            if chat["digest_last"] == today:
                continue
            try:
                text = await self.build_digest(chat["chat_id"], int(now.timestamp()) - DAY)
                if text:
                    await self.dm(chat["owner_id"], text)
            except Exception:  # one broken chat must not stop the others
                log.exception("Digest failed for chat %s", chat["chat_id"])
            await self.storage.mark_digest_sent(chat["chat_id"], today)

    async def digest_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if datetime.now(timezone.utc).hour == DIGEST_HOUR_UTC:
                await self.send_digests()
