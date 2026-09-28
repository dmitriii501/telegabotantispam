"""Telegram adapter: turns group messages into `Comment`s and executes decisions.

The bot works in a channel's discussion group (where channel comments live)
and in ordinary groups. It needs admin rights to delete messages and ban users.
"""

import asyncio
import html
import logging
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.jev_client import JevError
from core.models import ActionType, Author, Comment, Decision
from core.moderation import Moderator
from core.storage import Storage

log = logging.getLogger(__name__)

GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
EXPLANATION_TTL = 60  # seconds before the "comment deleted" notice disappears
ADMIN_CACHE_TTL = 600

STATUS_ICON = {"allowed": "✅ можно", "forbidden": "❌ нельзя", "unspecified": "— не указано"}

HELP_TEXT = (
    "Я модерирую комментарии с помощью ИИ: понимаю смысл, а не ищу стоп-слова, "
    "поэтому ловлю рекламу, замаскированную латиницей и цифрами.\n\n"
    "<b>Как подключить</b>\n"
    "1. Добавьте меня в группу обсуждений канала (или в обычную группу).\n"
    "2. Сделайте меня админом с правами удалять сообщения и банить.\n"
    "3. В группе напишите правила обычным языком:\n"
    "<code>/rules Канал про крипту. Мат можно. Рекламу других каналов нельзя.</code>\n"
    "4. Подтвердите, что я правильно понял правила.\n\n"
    "Спам я удаляю всегда, даже без правил. Спорные комментарии, апелляции и полезные "
    "комментарии присылаю в личку тому, кто подтвердил правила, поэтому напишите мне /start.\n\n"
    "<b>Команды в группе (только для админов)</b>\n"
    "/rules [текст] — задать или показать правила\n"
    "/stats — статистика за сутки\n"
    "/off, /on — выключить или включить модерацию"
)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def message_link(chat, message_id: int) -> str:
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    return f"https://t.me/c/{str(chat.id).removeprefix('-100')}/{message_id}"


def quote(text: str, limit: int = 700) -> str:
    text = text if len(text) <= limit else text[:limit] + "…"
    return f"<blockquote>{html.escape(text)}</blockquote>"


class TelegramAdapter:
    def __init__(self, bot: Bot, moderator: Moderator, storage: Storage):
        self.bot = bot
        self.moderator = moderator
        self.storage = storage
        self._admins: dict[int, tuple[float, set[int]]] = {}
        self.router = Router()
        self._register()

    def dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        dp.include_router(self.router)
        return dp

    # ------------------------------------------------------------------ helpers

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        cached = self._admins.get(chat_id)
        if not cached or time.time() - cached[0] > ADMIN_CACHE_TTL:
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
            missing.append("банить пользователей")
        return missing

    # ----------------------------------------------------------------- handlers

    def _register(self) -> None:
        r = self.router
        r.message(CommandStart(), F.chat.type == ChatType.PRIVATE)(self.on_start)
        r.my_chat_member()(self.on_bot_added)
        r.message(Command("rules"), GROUPS)(self.on_rules)
        r.message(Command("stats"), GROUPS)(self.on_stats)
        r.message(Command("off", "on"), GROUPS)(self.on_toggle)
        r.callback_query(F.data.startswith("rules:"))(self.on_rules_button)
        r.callback_query(F.data.startswith("appeal:"))(self.on_appeal)
        r.callback_query(F.data.startswith("ap:"))(self.on_appeal_decision)
        r.callback_query(F.data.startswith("rev:"))(self.on_review_decision)
        r.message(GROUPS)(self.on_group_message)

    async def on_start(self, message: Message) -> None:
        await message.answer(HELP_TEXT)

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
                "Задайте правила обычным языком: <code>/rules мат можно, рекламу нельзя</code>"
            )
        try:
            await self.bot.send_message(event.chat.id, text)
        except TelegramAPIError:
            pass

    async def on_rules(self, message: Message, command: CommandObject) -> None:
        if not await self.is_admin_message(message):
            return
        chat_id = message.chat.id
        if not command.args:
            chat = await self.storage.get_chat(chat_id)
            rules = chat["rules"] if chat and chat["rules"] else "не заданы (удаляю только спам)"
            await message.reply(f"Текущие правила: {html.escape(rules)}\n\nИзменить: <code>/rules текст</code>")
            return
        text = command.args.strip()
        try:
            preview = await self.moderator.preview_rules(text)
        except JevError as e:
            log.error("Rules preview failed: %s", e)
            await message.reply("Не получилось разобрать правила: ИИ временно недоступен. Попробуйте позже.")
            return
        await self.storage.set_pending_rules(chat_id, text)
        lines = "\n".join(f"{label}: {STATUS_ICON[status]}" for label, status in preview)
        await message.reply(
            f"Понял так:\n{lines}\nСпам: ❌ нельзя (всегда)\n\n"
            f"Проверять буду по вашему тексту целиком, а это краткая выжимка. Всё верно?",
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
            reached = await self.dm(call.from_user.id, "Правила сохранены. Сюда буду присылать спорные и полезные комментарии.")
            note = "" if reached else "\n\nНапишите мне /start в личку, чтобы получать спорные и полезные комментарии."
            await call.message.edit_text("Правила сохранены ✅" + note)
        else:
            await self.storage.discard_pending_rules(chat_id)
            await call.message.edit_text("Хорошо, напишите правила заново: <code>/rules текст</code>")
        await call.answer()

    async def on_stats(self, message: Message) -> None:
        if not await self.is_admin_message(message):
            return
        s = await self.storage.stats(message.chat.id, int(time.time()) - 24 * 3600)
        checked = sum(v for k, v in s.items() if k != "tokens")
        deleted = sum(s.get(a.value, 0) for a in (ActionType.DELETE_AND_BAN, ActionType.DELETE_SILENT, ActionType.DELETE_AND_EXPLAIN))
        await message.reply(
            "<b>За сутки</b>\n"
            f"Проверено комментариев: {checked}\n"
            f"Удалено: {deleted} (из них с баном: {s.get(ActionType.DELETE_AND_BAN.value, 0)})\n"
            f"Отправлено вам на проверку: {s.get(ActionType.SEND_TO_REVIEW.value, 0)}\n"
            f"Полезных: {s.get(ActionType.FORWARD_USEFUL.value, 0)}\n"
            f"Токенов Jev: {s['tokens']}"
        )

    async def on_toggle(self, message: Message, command: CommandObject) -> None:
        if not await self.is_admin_message(message):
            return
        enabled = command.command == "on"
        await self.storage.set_enabled(message.chat.id, enabled)
        await message.reply("Модерация включена ✅" if enabled else "Модерация выключена ⏸")

    # --------------------------------------------------------------- moderation

    async def on_group_message(self, message: Message) -> None:
        text = message.text or message.caption
        if not text or not message.from_user or message.is_automatic_forward:
            return
        if message.sender_chat or message.from_user.is_bot:
            return  # channel posts, anonymous admins, other bots
        chat_id = message.chat.id
        chat = await self.storage.get_chat(chat_id)
        if chat and not chat["enabled"]:
            return
        if await self.is_admin(chat_id, message.from_user.id):
            return

        user = message.from_user
        messages, deletions = await self.storage.user_counters(chat_id, user.id)
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
        )
        rules = chat["rules"] if chat else ""
        try:
            decision = await self.moderator.check(comment, rules)
        except JevError as e:
            log.error("Moderation failed, letting the comment through: %s", e)
            return

        deleted = decision.action in (ActionType.DELETE_AND_BAN, ActionType.DELETE_SILENT, ActionType.DELETE_AND_EXPLAIN)
        await self.storage.count_message(chat_id, user.id, deleted)
        if decision.action == ActionType.NONE and decision.verdict is None:
            return  # skipped without calling Jev
        v = decision.verdict
        log_id = await self.storage.add_log(
            chat_id=chat_id,
            message_id=message.message_id,
            user_id=user.id,
            user_name=user.full_name,
            text=text,
            category=v.category.value if v else None,
            confidence=v.confidence if v else None,
            bot_probability=v.bot_probability if v else None,
            action=decision.action.value,
            reason=decision.reason,
            tokens=v.input_tokens if v else 0,
        )
        log.info("chat=%s user=%s action=%s %s", chat_id, user.id, decision.action.value, v)
        await self.execute(message, decision, log_id, chat)

    async def execute(self, message: Message, decision: Decision, log_id: int, chat) -> None:
        action = decision.action
        owner_id = chat["owner_id"] if chat else None
        user = message.from_user
        try:
            if action in (ActionType.DELETE_AND_BAN, ActionType.DELETE_SILENT, ActionType.DELETE_AND_EXPLAIN):
                await message.delete()
            if action == ActionType.DELETE_AND_BAN:
                await self.bot.ban_chat_member(message.chat.id, user.id)
            elif action == ActionType.DELETE_AND_EXPLAIN:
                mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'
                notice = await self.bot.send_message(
                    message.chat.id,
                    f"{mention}, ваш комментарий удалён: {html.escape(decision.reason)}.",
                    reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
                    reply_markup=kb([("Не согласен — оспорить", f"appeal:{log_id}")]),
                )
                asyncio.create_task(self.delete_later(message.chat.id, notice.message_id, EXPLANATION_TTL))
            elif action == ActionType.SEND_TO_REVIEW:
                v = decision.verdict
                await self.dm(
                    owner_id,
                    f"🤔 Не уверен насчёт комментария от {html.escape(user.full_name)} "
                    f"({html.escape(decision.reason)}, уверенность {v.confidence:.0%}):\n"
                    f"{quote(message.text or message.caption)}\n{message_link(message.chat, message.message_id)}",
                    kb([("🗑 Удалить", f"rev:del:{log_id}"), ("✅ Оставить", f"rev:keep:{log_id}")]),
                )
            elif action == ActionType.FORWARD_USEFUL:
                await self.dm(
                    owner_id,
                    f"💡 Полезный комментарий от {html.escape(user.full_name)}:\n"
                    f"{quote(message.text or message.caption)}\n{message_link(message.chat, message.message_id)}",
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

    async def on_appeal_decision(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        if call.data.startswith("ap:ok"):
            await self.storage.set_feedback(entry["id"], "not_spam")
            # A deleted message cannot be restored, so the bot reposts its text.
            try:
                await self.bot.send_message(
                    entry["chat_id"],
                    f"↩️ Комментарий {html.escape(entry['user_name'] or '')} восстановлен админом:\n{quote(entry['text'])}",
                )
            except TelegramAPIError as e:
                log.warning("Cannot restore comment: %s", e)
            await call.message.edit_text(call.message.html_text + "\n\n↩️ Комментарий восстановлен.")
        else:
            await self.storage.set_feedback(entry["id"], "confirmed")
            await call.message.edit_text(call.message.html_text + "\n\n🗑 Удаление подтверждено.")
        await call.answer()

    async def on_review_decision(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        if call.data.startswith("rev:del"):
            await self.storage.set_feedback(entry["id"], "confirmed")
            try:
                await self.bot.delete_message(entry["chat_id"], entry["message_id"])
                await self.storage.count_message(entry["chat_id"], entry["user_id"], deleted=True)
                result = "🗑 Удалено."
            except TelegramAPIError:
                result = "Не удалось удалить: сообщение уже удалено или прошло больше 48 часов."
        else:
            await self.storage.set_feedback(entry["id"], "not_spam")
            result = "✅ Оставлено."
        await call.message.edit_text(call.message.html_text + "\n\n" + result)
        await call.answer()
