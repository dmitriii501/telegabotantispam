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
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
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
# Trial mode is silent by nature, so the owner gets a few live examples to see that the bot works.
TRIAL_NOTICES_PER_DAY = 5
TRIAL_ADMIN_TESTS_PER_DAY = 20
AUTO_REMEMBER_CONFIDENCE = 0.95  # bans this sure are remembered, so copies of the spam skip Jev

# Antiflood defaults; every chat can change them. The limits keep the settings sane.
FLOOD_MESSAGES = 6
FLOOD_WINDOW = 20
FLOOD_MUTE_MINUTES = 30
FLOOD_LIMITS = {"messages": (3, 30), "window": (5, 120), "mute": (1, 10080)}
JEV_ALERT_COOLDOWN = 60 * 60  # tell the owner about a Jev outage at most once an hour
RIGHTS_ALERT_COOLDOWN = 6 * 60 * 60
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))  # comment texts are deleted after this long
KIND_LABEL = {
    "question": "вопросы",
    "complaint": "жалобы",
    "praise": "похвала",
    "suggestion": "предложения",
    "bug": "ошибки в постах",
    "chat": "общение",
}

ACTION_LABEL = {
    "ban": "удалять и банить",
    "mute": "удалять и мутить на сутки",
    "warn": "удалять с предупреждением",
    "delete": "просто удалять",
    "review": "присылать вам на проверку",
    None: "автоматически: спам-бота забаню, человеку объясню, если не уверен — спрошу вас",
}
DECISION_LABEL = {
    ActionType.NONE: "оставил бы",
    ActionType.DELETE_AND_BAN: "удалил бы и забанил",
    ActionType.DELETE_AND_MUTE: "удалил бы и замутил",
    ActionType.DELETE_SILENT: "удалил бы молча",
    ActionType.DELETE_AND_EXPLAIN: "удалил бы и объяснил автору",
    ActionType.SEND_TO_REVIEW: "прислал бы вам на проверку",
    ActionType.FORWARD_USEFUL: "переслал бы вам как полезный",
}
MODE_LABEL = {
    "soft": "мягкий: почти всё сомнительное присылаю вам",
    "normal": "обычный: уверенное удаляю, сомнительное присылаю вам",
    "strict": "строгий: удаляю смелее, вам присылаю реже",
}
CATEGORY_LABEL = {"spam": "спам", "rule_violation": "нарушение правил", "useful": "полезный", "normal": "обычный"}
KIND_SINGLE = {
    "question": "вопрос",
    "complaint": "жалоба",
    "praise": "похвала",
    "suggestion": "предложение",
    "bug": "ошибка в посте",
    "chat": "общение",
}
USEFUL_LABEL = {
    "digest": "полезные комментарии пойдут в ежедневную сводку",
    "instant": "полезные комментарии буду присылать сразу",
    "off": "полезные комментарии присылать не буду",
}
JEV_PRICE_PER_MTOK = 0.042  # dollars per million input tokens
NOT_ADMIN = "я не админ"
RIGHTS_TEXT = (
    "Чтобы модерировать, сделайте меня админом группы с правами «Удалять сообщения» "
    "и «Блокировать пользователей»."
)

HELP_TEXT = (
    "Я слежу за комментариями под постами: убираю спам и рекламу (в том числе замаскированную), "
    "нарушения ваших правил и подсказываю, кому вы не ответили.\n\n"
    "<b>Как подключить</b>\n"
    "1. Добавьте меня в группу обсуждений канала (или в обычную группу).\n"
    "2. Сделайте меня админом с правами «Удалять сообщения» и «Блокировать пользователей».\n"
    "3. Напишите в группе правила обычным языком, можно сразу с действиями:\n"
    "<code>/rules Канал про крипту. Мат можно. Рекламу нельзя — бан. "
    "Политику — просто удалять. Оскорбления — предупреждать.</code>\n"
    "4. Подтвердите, что я понял правильно.\n\n"
    "Новый чат начинается в <b>пробном режиме</b>: я ничего не удаляю, а записываю, что сделал бы "
    "(итог: /report). Первые находки я пришлю вам в личку с кнопками «Верно» и «Ошибка». "
    "Чтобы проверить меня самому, напишите в чат тестовую рекламу: я отвечу, что сделал бы. "
    "Когда результат устроит, включите настоящую модерацию: /observe off.\n\n"
    "Проще всего всё настраивать в <b>панели</b>: /panel. Спорные комментарии, апелляции и сводки "
    "я присылаю в личку тому, кто подтвердил правила, поэтому напишите мне /start.\n\n"
    "<b>Команды в группе (только для админов)</b>\n"
    "/panel — панель: правила, настройки, журнал, аналитика\n"
    "/rules [текст] — задать или показать правила\n"
    "/settings — все настройки одним сообщением\n"
    "/report — отчёт за 7 дней\n"
    "/check текст — что бы я сделал с таким комментарием (ничего не удаляя)\n"
    "/observe on|off — пробный режим: ничего не удалять, только записывать\n"
    "/mode soft|normal|strict — строгость: soft чаще спрашивает вас, strict удаляет смелее\n"
    "/lockdown on|off — удалять всё от участников (на время рейда)\n"
    "/escalation on|off — мут на сутки за 3-е нарушение и бан за 5-е (за 30 дней)\n"
    "/antiflood [5 10 60|off] — мут за флуд: сообщений, секунд, минут мута; без чисел покажет настройку\n"
    "/conflicts on|off — предупреждать, когда обсуждение накаляется\n"
    "/analytics on|off — типы и тон комментариев, «ждёт ответа»\n"
    "/useful digest|instant|off — полезные комментарии: в сводку, сразу или не присылать\n"
    "/digest on|off — ежедневная сводка\n"
    "/trust, /untrust — ответом на сообщение: не проверять этого человека\n"
    "/off и /on — поставить модерацию на паузу и вернуть"
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


ANTIFLOOD_HELP = (
    "Изменить: <code>/antiflood 5 10 60</code>: 5 сообщений за 10 секунд, мут на 60 минут. "
    "Выключить: <code>/antiflood off</code>."
)


def describe_antiflood(config: ChatConfig) -> str:
    if not config.antiflood:
        return "выключен"
    return (
        f"{config.flood_messages} сообщений за {config.flood_window} с "
        f"→ мут на {human_duration(config.flood_mute_minutes / 60)}"
    )


def human_duration(hours: float) -> str:
    minutes = round(hours * 60)
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч" if minutes % 60 == 0 else f"{minutes // 60} ч {minutes % 60} мин"


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
        self._flood: dict[tuple[int, int], deque] = {}
        self._notified: dict[tuple[int, str], float] = {}
        self._linked: dict[int, int | None] = {}
        self._tasks: set[asyncio.Task] = set()  # strong references: a bare create_task can be garbage collected
        self._trial: dict[tuple[int, str], deque] = {}
        self._purged_day = ""
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
        if not message.from_user or not await self.is_admin(message.chat.id, message.from_user.id):
            return False
        await self.storage.claim_owner(message.chat.id, message.from_user.id)
        return True

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

    async def notify_owner(self, chat, key: str, text: str, cooldown: float) -> None:
        """Message the chat owner, but not more often than once per `cooldown` for the same `key`."""
        owner_id = chat["owner_id"] if chat else None
        if not owner_id:
            return
        ck = (chat["chat_id"], key)
        now = time.time()
        if now - self._notified.get(ck, 0) < cooldown:
            return
        self._notified[ck] = now
        await self.dm(owner_id, f"{text}\n\nЧат: «{html.escape(await self.chat_title(chat['chat_id']))}»")

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
        try:
            await self.bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass

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
        r.message(Command("observe"), GROUPS)(self.on_observe)
        r.message(Command("antiflood"), GROUPS)(self.on_antiflood)
        r.message(Command("analytics"), GROUPS)(self.on_analytics)
        r.message(Command("report"), GROUPS)(self.on_report)
        r.message(Command("panel"))(self.on_panel)
        r.message(Command("help"))(self.on_help)
        r.message(Command("off", "on"), GROUPS)(self.on_toggle)
        r.callback_query(F.data.startswith("rules:"))(self.on_rules_button)
        r.callback_query(F.data.startswith("appeal:"))(self.on_appeal)
        r.callback_query(F.data.startswith("ap:"))(self.on_appeal_decision)
        r.callback_query(F.data.startswith("rev:"))(self.on_review_decision)
        r.callback_query(F.data.startswith("obs:"))(self.on_trial_verdict)
        r.message(GROUPS)(self.on_group_message)
        r.edited_message(GROUPS)(self.on_edited_message)

    async def on_start(self, message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=self.panel_markup())

    async def on_help(self, message: Message) -> None:
        await message.answer(HELP_TEXT, reply_markup=self.panel_markup() if message.chat.type == ChatType.PRIVATE else None)

    async def set_commands(self) -> None:
        """The "/" menu in Telegram: short lists, so people find the commands without reading the manual."""
        private = [BotCommand(command="start", description="Как подключить бота"),
                   BotCommand(command="panel", description="Панель управления")]
        admins = [
            BotCommand(command="panel", description="Панель: правила, настройки, журнал"),
            BotCommand(command="rules", description="Правила чата"),
            BotCommand(command="settings", description="Все настройки"),
            BotCommand(command="report", description="Отчёт за 7 дней"),
            BotCommand(command="check", description="Проверить текст комментария"),
            BotCommand(command="observe", description="Пробный режим: вкл или выкл"),
            BotCommand(command="help", description="Все команды"),
        ]
        try:
            await self.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
            await self.bot.set_my_commands(admins, scope=BotCommandScopeAllChatAdministrators())
        except TelegramAPIError as e:
            log.warning("Cannot set the command menu: %s", e)

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
        created = await self.storage.ensure_chat(event.chat.id)
        if created:
            await self.storage.set_setting(event.chat.id, "observe", 1)
        missing = await self.check_rights(event.chat.id)
        if missing == [NOT_ADMIN]:
            text = "Привет! " + RIGHTS_TEXT
        elif missing:
            text = "Привет! Мне не хватает прав: " + ", ".join(missing) + ". Включите их в настройках админа."
        elif created:
            text = (
                "Готов к работе. Первые дни я в <b>пробном режиме</b>: ничего не удаляю, а записываю, "
                "что сделал бы. Через несколько дней <code>/report</code> покажет результат, "
                "<code>/observe off</code> включит настоящую модерацию.\n"
                "Проверить меня можно сразу: напишите в чат тестовое рекламное сообщение, и я отвечу, "
                "что сделал бы.\n"
                "Задайте правила обычным языком: <code>/rules мат можно, рекламу нельзя — бан</code>\n"
                "Или откройте панель: /panel"
            )
        else:
            text = (
                "Готов к работе. Задайте правила обычным языком: "
                "<code>/rules мат можно, рекламу нельзя — бан</code>"
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
                f"<b>Правила</b>\n{describe_rules(config.rules)}\n❌ спам и мошенничество — всегда запрещены\n\n"
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
            f"Понял так:\n{describe_rules(rules)}\n❌ спам и мошенничество — всегда запрещены\n\n"
            "Если в правиле не названо действие, решаю сам: спам-бота баню, человеку объясняю, "
            "если не уверен — спрашиваю вас. Всё верно?",
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
            f"Удалять всё от участников: {on_off(config.lockdown)}\n"
            f"Мут и бан за повторные нарушения: {on_off(config.escalation)}\n"
            f"Полезные комментарии: {useful[chat['useful_mode'] if chat else 'digest']}\n"
            f"Ежедневная сводка: {on_off(not chat or bool(chat['digest']))}\n"
            f"Предупреждения о ссорах: {on_off(config.conflicts)}\n"
            f"Антифлуд: {describe_antiflood(config)}\n"
            f"Аналитика комментариев: {on_off(config.analytics)}\n"
            f"Пробный режим (ничего не удаляю): {on_off(config.observe)}\n\n"
            f"<b>Правила</b>\n{describe_rules(config.rules)}\n❌ спам и мошенничество — всегда запрещены"
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
        await self._switch(message, command, "lockdown", "Удалять всё от участников")

    async def on_escalation(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "escalation", "Мут и бан за повторные нарушения")

    async def on_conflicts(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "conflicts", "Предупреждения о ссорах")

    async def on_observe(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "observe", "Пробный режим")

    async def on_antiflood(self, message: Message, command: CommandObject) -> None:
        """/antiflood — show; /antiflood on|off; /antiflood 5 10 60 — 5 messages in 10 s → mute for 60 min."""
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        chat_id = message.chat.id
        if arg in ("on", "off"):
            await self.storage.set_setting(chat_id, "antiflood", int(arg == "on"))
        elif arg:
            try:
                messages, window, mute = (int(x) for x in arg.split())
            except ValueError:
                await message.reply(ANTIFLOOD_HELP)
                return
            for value, (low, high), title in (
                (messages, FLOOD_LIMITS["messages"], "сообщений"),
                (window, FLOOD_LIMITS["window"], "секунд"),
                (mute, FLOOD_LIMITS["mute"], "минут мута"),
            ):
                if not low <= value <= high:
                    await message.reply(f"Число {title} должно быть от {low} до {high}.\n\n{ANTIFLOOD_HELP}")
                    return
            await self.storage.set_setting(chat_id, "flood_messages", messages)
            await self.storage.set_setting(chat_id, "flood_window", window)
            await self.storage.set_setting(chat_id, "flood_mute", mute)
            await self.storage.set_setting(chat_id, "antiflood", 1)
        await message.reply(f"Антифлуд: {describe_antiflood(await self.config(chat_id))}\n\n{ANTIFLOOD_HELP}")

    async def on_analytics(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "analytics", "Аналитика комментариев")

    async def on_report(self, message: Message) -> None:
        if not await self.is_admin_message(message):
            return
        config = await self.config(message.chat.id)
        text = await self.build_digest(message.chat.id, int(time.time()) - 7 * DAY, config.observe, "за 7 дней")
        await message.reply(text or "За неделю пока нечего показать: бот ещё ничего не проверял.")

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
        await message.reply(f"Готово: {USEFUL_LABEL[arg]}.")

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
            lines.append(
                f"Оценка: {CATEGORY_LABEL[v.category.value]}. Бот уверен на {v.confidence:.0%}, "
                f"шанс, что писал бот-спамер: {v.bot_probability:.0%}"
            )
            if v.kind:
                lines.append(f"Тип комментария: {KIND_SINGLE[v.kind]}")
            if v.lead >= 0.6:
                lines.append("🛒 Похоже на клиента: хочет купить или узнать цену")
            if v.needs_answer >= 0.6:
                lines.append("📨 Автор ждёт ответа")
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
            f"Расход ИИ: около ${s['tokens'] / 1e6 * JEV_PRICE_PER_MTOK:.2f}"
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

    @staticmethod
    def extract_links(message: Message) -> list[str]:
        """Links Jev cannot see in the plain text: hidden behind a word, or written as a url."""
        text = message.text or message.caption or ""
        links = []
        for entity in message.entities or message.caption_entities or []:
            if entity.type == "text_link" and entity.url:
                links.append(f"«{entity.extract_from(text)}» → {entity.url}")
            elif entity.type == "url":
                links.append(entity.extract_from(text))
            elif entity.type == "text_mention" and entity.user:
                links.append(f"«{entity.extract_from(text)}» → профиль {entity.user.id}")
        return links[:10]

    def flood_hit(self, chat_id: int, author_id: int, limit: int = FLOOD_MESSAGES, window: int = FLOOD_WINDOW) -> bool:
        """Register a message; True when this author posted `limit` messages within `window` seconds."""
        now = time.time()
        stamps = self._flood.setdefault((chat_id, author_id), deque(maxlen=FLOOD_LIMITS["messages"][1]))
        stamps.append(now)
        if len(self._flood) > 5000:
            for key in [k for k, v in self._flood.items() if not v or now - v[-1] > FLOOD_LIMITS["window"][1]]:
                del self._flood[key]
        return sum(1 for t in stamps if now - t <= window) >= limit

    async def on_group_message(self, message: Message) -> None:
        await self.handle_comment(message)

    async def on_edited_message(self, message: Message) -> None:
        # A classic trick: post something harmless, then edit it into an advert.
        await self.handle_comment(message, edited=True)

    async def handle_comment(self, message: Message, edited: bool = False) -> None:
        if message.is_automatic_forward:
            if not edited:
                await self.remember_post(message)
            return
        text = message.text or message.caption
        if not text:
            return
        chat_id = message.chat.id
        chat = await self.storage.get_chat(chat_id)
        if chat and not chat["enabled"]:
            return

        sender, user = message.sender_chat, message.from_user
        if sender:
            # Posted on behalf of a channel. Anonymous admins (the group itself) and the group's own
            # channel are fine; any other channel is a stranger and is moderated like a person.
            if sender.id == chat_id:
                await self.note_admin_reply(message, edited)
                await self.trial_test(message, chat, edited)
                return
            if sender.id == await self.linked_channel(chat_id):
                return
            author_id, author_name, username, premium = sender.id, sender.title or "Канал", sender.username, False
        elif user and not user.is_bot:
            if await self.is_admin(chat_id, user.id):
                await self.note_admin_reply(message, edited)
                await self.trial_test(message, chat, edited)
                return
            author_id, author_name, username, premium = user.id, user.full_name, user.username, bool(user.is_premium)
        else:
            return  # other bots
        if await self.storage.is_trusted(chat_id, author_id):
            return

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
                user_id=author_id,
                display_name=author_name,
                username=username,
                is_premium=premium,
                previous_messages=await self.storage.message_count(chat_id, author_id),
                previous_deletions=await self.storage.recent_deletions(chat_id, author_id),
            ),
            post_text=post_text,
            reply_to_text=reply_to_text,
            links=self.extract_links(message),
        )

        if (
            config.antiflood
            and not edited
            and self.flood_hit(chat_id, author_id, config.flood_messages, config.flood_window)
        ):
            decision = self.moderator.flood_decision(config.flood_mute_minutes)
        else:
            try:
                decision = await self.moderator.check(comment, config, known)
            except JevError as e:
                log.error("Jev unavailable, using the local fallback filter: %s", e)
                await self.notify_owner(
                    chat,
                    "jev_down",
                    "⚠️ ИИ сейчас недоступен. Пока он не вернётся, работает упрощённая защита: "
                    "только заведомый спам со ссылками. Остальные комментарии проходят без проверки.",
                    JEV_ALERT_COOLDOWN,
                )
                decision = self.moderator.fallback(comment, config)

        if not edited:
            await self.storage.count_message(chat_id, author_id)
            if decision.action not in DELETING_ACTIONS:
                self.spawn(self.watch_thread(message, text, config, chat))
        v = decision.verdict
        if v is None:
            return  # nothing to record: skipped without asking Jev
        log_id = await self.storage.add_log(
            chat_id=chat_id,
            message_id=message.message_id,
            user_id=author_id,
            user_name=author_name,
            text=text,
            category=v.category.value,
            confidence=v.confidence,
            bot_probability=v.bot_probability,
            action=decision.action.value,
            reason=decision.reason,
            tokens=v.input_tokens,
            kind=v.kind,
            lead=v.lead,
            needs_answer=v.needs_answer,
            sentiment=v.sentiment,
            violation=v.violation_probability,
        )
        log.info("chat=%s author=%s action=%s edited=%s %s", chat_id, author_id, decision.action.value, edited, v)
        if config.observe:
            await self.storage.set_executed(log_id, False)  # a dry run: recorded, not carried out
            if decision.action in DELETING_ACTIONS:
                await self.trial_notice(message, decision, log_id, chat, author_name)
            return
        if key and decision.action == ActionType.DELETE_AND_BAN and v.confidence >= AUTO_REMEMBER_CONFIDENCE:
            await self.storage.add_example(chat_id, key, "remove")
        if not await self.execute(message, decision, log_id, chat):
            await self.storage.set_executed(log_id, False)

    async def trial_notice(self, message: Message, decision: Decision, log_id: int, chat, name: str) -> None:
        """Trial mode: show the owner the first few things the bot would remove, with a verdict button."""
        owner_id = chat["owner_id"] if chat else None
        left = self.trial_slot(message.chat.id, "owner", TRIAL_NOTICES_PER_DAY) if owner_id else None
        if left is None:
            return
        title = html.escape(await self.chat_title(message.chat.id))
        text = (
            f"🔎 <b>Пробный режим</b>, чат «{title}»: я {DECISION_LABEL[decision.action]} комментарий от "
            f"{html.escape(name)}.\nПричина: {html.escape(decision.reason)}.\n"
            f"{quote(message.text or message.caption)}\n"
            f"{chat_message_link(message.chat.id, message.message_id, message.chat.username)}"
        )
        if left == 0:
            text += "\n\nЭто последняя подсказка на сегодня, остальное покажет /report."
        await self.dm(owner_id, text, kb([("✅ Верно", f"obs:ok:{log_id}"), ("↩️ Ошибка, это нормально", f"obs:no:{log_id}")]))

    async def trial_test(self, message: Message, chat, edited: bool) -> None:
        """Trial mode: an admin's own message is checked as if a member wrote it, so the bot can be tested by hand.

        Nothing is recorded and nothing is deleted; the answer goes to the admin privately (or briefly into the chat).
        """
        text = message.text or message.caption
        if edited or not chat or not chat["observe"] or not text or text.startswith("/"):
            return
        chat_id = message.chat.id
        if self.trial_slot(chat_id, "admin", TRIAL_ADMIN_TESTS_PER_DAY) is None:
            return
        user = message.from_user
        anonymous = bool(message.sender_chat)
        comment = Comment(
            chat_id, message.message_id, text,
            Author(user.id if user and not anonymous else 0, "Тест", previous_messages=1),
            links=self.extract_links(message),
        )
        try:
            decision = await self.moderator.check(comment, ChatConfig.from_row(chat))
        except JevError:
            return
        if decision.action in (ActionType.NONE, ActionType.FORWARD_USEFUL):
            return
        answer = (
            f"🔎 <b>Пробный режим:</b> если бы это написал участник, я {DECISION_LABEL[decision.action]}.\n"
            f"Причина: {html.escape(decision.reason)}."
        )
        target = chat["owner_id"] if anonymous else user.id
        if await self.dm(target, answer):
            return
        try:  # the admin has not started the bot: answer in the chat and tidy up soon
            note = await message.reply(answer)
            self.spawn(self.delete_later(chat_id, note.message_id, EXPLANATION_TTL))
        except TelegramAPIError as e:
            log.info("Trial answer not delivered: %s", e)

    async def on_trial_verdict(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        right = call.data.startswith("obs:ok")
        await self.storage.set_feedback(entry["id"], "confirmed" if right else "not_spam")
        await call.message.edit_text(
            call.message.html_text
            + ("\n\n✅ Записал: бот прав." if right else "\n\n↩️ Записал: бот ошибся, такие комментарии буду пропускать.")
        )
        await call.answer()

    async def note_admin_reply(self, message: Message, edited: bool) -> None:
        """An admin answered a comment: it stops counting as unanswered."""
        parent = message.reply_to_message
        if parent and not edited:
            await self.storage.mark_answered(message.chat.id, parent.message_id)

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
                "Если нужно остановить, включите удаление всех сообщений участников: /lockdown on",
            )
        except Exception:  # background task: never let it crash the handler
            log.exception("Conflict check failed")

    async def execute(self, message: Message, decision: Decision, log_id: int, chat) -> bool:
        """Carry the decision out. Returns False when Telegram refused (usually missing admin rights)."""
        action = decision.action
        owner_id = chat["owner_id"] if chat else None
        useful_mode = chat["useful_mode"] if chat else "digest"
        sender, user = message.sender_chat, message.from_user
        name = sender.title if sender else user.full_name
        shown = message.text or message.caption
        try:
            if action in DELETING_ACTIONS:
                await message.delete()
            # A channel cannot be warned or muted, so any removal of its comment bans the channel.
            if action == ActionType.DELETE_AND_BAN or (sender and action in DELETING_ACTIONS):
                if sender:
                    await self.bot.ban_chat_sender_chat(message.chat.id, sender.id)
                else:
                    await self.bot.ban_chat_member(message.chat.id, user.id)
            elif action in (ActionType.DELETE_AND_EXPLAIN, ActionType.DELETE_AND_MUTE) and not sender:
                hours = decision.extra.get("mute_hours")
                if hours:
                    await self.bot.restrict_chat_member(
                        message.chat.id,
                        user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=timedelta(hours=hours),
                    )
                mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name)}</a>'
                tail = f" Писать в чате можно будет через {human_duration(hours)}." if hours else ""
                notice = await self.bot.send_message(
                    message.chat.id,
                    f"{mention}, ваш комментарий удалён: {html.escape(decision.reason)}.{tail}",
                    reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
                    reply_markup=kb([("Не согласен — оспорить", f"appeal:{log_id}")]),
                )
                self.spawn(self.delete_later(message.chat.id, notice.message_id, EXPLANATION_TTL))
            elif action == ActionType.SEND_TO_REVIEW:
                v = decision.verdict
                title = html.escape(await self.chat_title(message.chat.id))
                await self.dm(
                    owner_id,
                    f"🤔 Не уверен насчёт комментария в чате «{title}»\n"
                    f"Автор: {html.escape(name)}. Причина: {html.escape(decision.reason)}. "
                    f"Бот уверен на {v.confidence:.0%}:\n"
                    f"{quote(shown)}\n{chat_message_link(message.chat.id, message.message_id, message.chat.username)}",
                    kb([("🗑 Удалить", f"rev:del:{log_id}"), ("✅ Оставить", f"rev:keep:{log_id}")]),
                )
            elif action == ActionType.FORWARD_USEFUL and useful_mode == "instant":
                title = html.escape(await self.chat_title(message.chat.id))
                await self.dm(
                    owner_id,
                    f"💡 Полезный комментарий в чате «{title}» от {html.escape(name)}:\n"
                    f"{quote(shown)}\n{chat_message_link(message.chat.id, message.message_id, message.chat.username)}",
                )
            return True
        except TelegramAPIError as e:
            log.warning("Action %s failed in chat %s: %s", action.value, message.chat.id, e)
            if any(word in str(e).lower() for word in ("rights", "admin", "forbidden", "kicked")):
                await self.notify_owner(
                    chat,
                    "rights",
                    "⚠️ Мне не хватает прав в чате: не могу удалять сообщения или банить. "
                    "Сделайте меня админом с правами «Удалять сообщения» и «Блокировать пользователей».",
                    RIGHTS_ALERT_COOLDOWN,
                )
            return False

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
            f"⚖️ {html.escape(entry['user_name'] or '')} оспаривает удаление в чате "
            f"«{html.escape(await self.chat_title(entry['chat_id']))}» "
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

    async def build_digest(
        self, chat_id: int, since: int, observe: bool = False, period: str = "за сутки"
    ) -> str | None:
        s = await self.storage.stats(chat_id, since)
        checked = sum(v for k, v in s.items() if k != "tokens")
        if not checked:
            return None
        deleted = sum(s.get(a.value, 0) for a in DELETING_ACTIONS)
        bans = s.get(ActionType.DELETE_AND_BAN.value, 0)
        try:
            title = html.escape((await self.bot.get_chat(chat_id)).title or "")
        except TelegramAPIError:
            title = ""
        if observe:
            lines = [
                f"🔎 <b>Отчёт пробного режима {period}</b> {title}".rstrip(),
                "Ничего не удалялось: бот записывал, что сделал бы.",
                f"Проверено комментариев: {checked}",
                f"Удалил бы: {deleted}, из них забанил бы: {bans}",
            ]
        else:
            lines = [
                f"📊 <b>Сводка {period}</b> {title}".rstrip(),
                f"Проверено комментариев: {checked}",
                f"Удалено: {deleted}, из них с баном: {bans}",
            ]
        review = s.get(ActionType.SEND_TO_REVIEW.value, 0)
        if review:
            lines.append(f"Спорных, отправленных вам на проверку: {review}")
        reasons = await self.storage.top_reasons(chat_id, since)
        if reasons:
            lines.append("\n<b>Чаще всего удалял:</b>" if not observe else "\n<b>Чаще всего удалил бы:</b>")
            lines += [f"• {html.escape(reason)} — {n}" for reason, n in reasons]

        kinds = await self.storage.kind_counts(chat_id, since)
        if kinds:
            parts = [f"{KIND_LABEL.get(k, k)} {n}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])]
            lines.append("\n<b>О чём пишут:</b> " + ", ".join(parts))
        mood = await self.storage.sentiment_summary(chat_id, since)
        if mood["n"] >= 5:
            lines.append(f"Тон: негативных {mood['negative']}, позитивных {mood['positive']} из {mood['n']}")
        waiting = await self.storage.unanswered(chat_id, since, limit=5)
        if waiting:
            lines.append("\n📨 <b>Ждут ответа:</b>")
            for row in waiting:
                snippet = row["text"] if len(row["text"]) <= 160 else row["text"][:160] + "…"
                mark = "🛒 " if (row["lead"] or 0) >= 0.6 else ""
                link = chat_message_link(chat_id, row["message_id"])
                lines.append(f"• {mark}{html.escape(row['user_name'] or '')}: {html.escape(snippet)}\n{link}")
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
                text = await self.build_digest(
                    chat["chat_id"], int(now.timestamp()) - DAY, bool(chat["observe"])
                )
                if text:
                    await self.dm(chat["owner_id"], text)
            except Exception:  # one broken chat must not stop the others
                log.exception("Digest failed for chat %s", chat["chat_id"])
            await self.storage.mark_digest_sent(chat["chat_id"], today)

    async def purge_expired(self, today: str) -> None:
        """Once a day: comment texts and names older than RETENTION_DAYS are deleted."""
        if self._purged_day == today:
            return
        self._purged_day = today
        removed = await self.storage.purge_old(RETENTION_DAYS)
        log.info("Retention: removed %d log rows older than %d days", removed, RETENTION_DAYS)

    async def digest_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            if now.hour == DIGEST_HOUR_UTC:
                try:
                    await self.send_digests(now)
                    await self.purge_expired(now.strftime("%Y-%m-%d"))
                except Exception:
                    log.exception("Daily job failed")
