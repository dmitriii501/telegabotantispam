"""Telegram adapter: turns group messages into `Comment`s and executes decisions.

The bot works in a channel's discussion group (where channel comments live)
and in ordinary groups. It needs admin rights to delete messages and ban users.
The parts live in base, newcomers, review, reports, settings and pipeline."""

import contextlib
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    ChatMemberUpdated,
    Message,
)

from adapters.telegram.common import (
    GROUPS,
    HELP_TEXT,
    NOT_ADMIN,
    PRIVACY_TEXT,
    RIGHTS_TEXT,
    user_of,
)
from adapters.telegram.pipeline import PipelineMixin
from core.moderation import (
    Moderator,
)
from core.ocr import OcrEngine
from core.storage import Storage

log = logging.getLogger(__name__)


class TelegramAdapter(PipelineMixin):
    """The Telegram side of the bot: handlers wired to the parts above."""

    def __init__(self, bot: Bot, moderator: Moderator, storage: Storage, ocr: OcrEngine | None = None):
        super().__init__(bot, moderator, storage, ocr)
        self._init_payments()
        self.router = Router()
        self._register()

    def dispatcher(self) -> Dispatcher:
        dp = Dispatcher()
        dp.include_router(self.router)
        return dp

    # ----------------------------------------------------------------- handlers
    def _register(self) -> None:
        r = self.router
        r.message(CommandStart(), F.chat.type == ChatType.PRIVATE)(self.on_start)
        self._register_payments(r)
        r.message(Command("privacy"))(self.on_privacy)
        r.message(Command("offer"))(self.on_offer)
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
        r.message(Command("captcha"), GROUPS)(self.on_captcha_command)
        r.message(Command("cleanservice"), GROUPS)(self.on_cleanservice)
        r.message(Command("night"), GROUPS)(self.on_night)
        r.message(Command("spam"), GROUPS, F.reply_to_message)(self.on_spam_report)
        r.message(Command("ocr"), GROUPS)(self.on_ocr)
        r.message(Command("firststrict"), GROUPS)(self.on_firststrict)
        r.message(Command("antiraid"), GROUPS)(self.on_antiraid)
        r.message(Command("profilecheck"), GROUPS)(self.on_profilecheck)
        r.message(Command("profile"), GROUPS)(self.on_profile)
        r.message(Command("links"), GROUPS)(self.on_links)
        r.message(Command("allowlink", "blocklink", "unlink"), GROUPS)(self.on_link_lists)
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
        r.callback_query(F.data.startswith("cap:"))(self.on_captcha_button)
        r.callback_query(F.data.startswith("raid:"))(self.on_raid_button)
        r.message(GROUPS)(self.on_group_message)
        r.edited_message(GROUPS)(self.on_edited_message)

    async def on_start(self, message: Message, command: CommandObject | None = None) -> None:
        arg = (command.args or "") if command else ""
        if arg.startswith("pay_") and await self.start_payment(message, arg[4:]):
            return
        await message.answer(HELP_TEXT, reply_markup=self.panel_markup())

    async def on_privacy(self, message: Message) -> None:
        site = self.webapp_url
        await message.answer(PRIVACY_TEXT + (f"\n\nПолный текст: {site}/privacy" if site else ""))

    async def on_offer(self, message: Message) -> None:
        if self.webapp_url:
            await message.answer(f"Публичная оферта: {self.webapp_url}/offer\nПолитика конфиденциальности: {self.webapp_url}/privacy")
        else:
            await message.answer("Оферта пока не опубликована.")

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
        sent = await self.dm(user_of(message).id, "Панель управления этим чатом:", self.panel_markup(message.chat.id))
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
                "Готов к работе. Первые 14 дней бесплатно, и в первые дни я в <b>пробном режиме</b>: ничего не удаляю, а записываю, "
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
        with contextlib.suppress(TelegramAPIError):
            await self.bot.send_message(event.chat.id, text)
