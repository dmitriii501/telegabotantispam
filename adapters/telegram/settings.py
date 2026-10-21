"""Commands that change a chat's rules and settings."""

import html
import logging

from aiogram.filters import CommandObject
from aiogram.types import (
    CallbackQuery,
    Message,
)

from adapters.telegram.common import (
    ANTIFLOOD_HELP,
    FLOOD_LIMITS,
    LINK_MODE_LABEL,
    LINKS_HELP,
    MODE_LABEL,
    USEFUL_LABEL,
    callback_data,
    describe_antiflood,
    describe_night,
    describe_rules,
    edit_pressed_message,
    kb,
    on_off,
)
from adapters.telegram.reports import ReportsMixin
from core.jev_client import JevError
from core.links import MAX_ENTRIES, normalize_entry
from core.moderation import (
    MODES,
    ChatConfig,
)
from core.rules import rules_to_json

log = logging.getLogger(__name__)


class SettingsMixin(ReportsMixin):
    """Commands that change a chat's rules and settings."""

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
        _, verdict, raw_chat = callback_data(call).split(":")
        chat_id = int(raw_chat)
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
            await edit_pressed_message(call, "Правила сохранены ✅" + note)
        else:
            await self.storage.discard_pending_rules(chat_id)
            await edit_pressed_message(call, "Хорошо, напишите правила заново: <code>/rules текст</code>")
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
            f"Ссылки: {LINK_MODE_LABEL[config.links_mode]}\n"
            f"Проверка профиля новичков: {on_off(config.profile_check)}\n"
            f"Удаление служебных сообщений («вошёл», «вышел»): {on_off(config.clean_service)}\n"
            f"Предупреждения о рейдах: {on_off(config.antiraid)}\n"
            f"Строгая проверка первых комментариев под постом: {on_off(config.first_strict)}\n"
            f"Ночной режим: {describe_night(config)}\n"
            f"Проверка «я человек» для новичков: {on_off(config.captcha)}\n"
            f"Вопросы покупателей сразу в личку: {on_off(config.leads_instant)}\n"
            f"Подпись «DefenceAi» под сообщениями бота: {on_off(config.signature)}\n"
            f"Рядом другие боты: {config.companions + (', я им не мешаю' if config.yields_to_others else '') if config.companions else 'не вижу'}\n"
            f"Проверка картинок и стикеров новичков: {on_off(config.image_ocr and self.sees_images)}\n"
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

    async def on_signature(self, message: Message, command: CommandObject) -> None:
        """/signature on|off: the small "🛡 DefenceAi" line under the bot's messages in the group. Paid plans only."""
        chat = await self.storage.get_chat(message.chat.id)
        if chat and (command.args or "").strip().lower() == "off" and (await self.access_for(chat)).state != "paid":
            if await self.is_admin_message(message):
                await message.reply("Подпись можно отключить на платном тарифе: /plan")
            return
        await self._switch(message, command, "signature", "Подпись «DefenceAi» под сообщениями бота")

    async def on_leads(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "leads_instant", "Вопросы покупателей сразу в личку")

    async def on_coexist(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "coexist", "Уступать другому боту капчу и приветствия")

    async def on_lockdown(self, message: Message, command: CommandObject) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        if arg not in ("on", "off"):
            await message.reply("Напишите <code>/lockdown on</code> или <code>/lockdown off</code>")
            return
        await self.storage.set_lockdown(message.chat.id, arg == "on")
        await message.reply(f"Удалять всё от участников: {on_off(arg == 'on')}")

    async def on_escalation(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "escalation", "Мут и бан за повторные нарушения")

    async def on_conflicts(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "conflicts", "Предупреждения о ссорах")

    async def on_captcha_command(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "captcha", "Проверка «я человек» для новичков")

    async def on_profilecheck(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "profile_check", "Проверка профиля новичков")

    async def on_links(self, message: Message, command: CommandObject) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        chat_id = message.chat.id
        if arg:
            if arg not in LINK_MODE_LABEL:
                await message.reply(LINKS_HELP)
                return
            config = await self.config(chat_id)
            await self.storage.set_links(chat_id, arg, config.allowed_domains, config.blocked_domains)
        await message.reply(self.describe_links(await self.config(chat_id)))

    async def on_link_lists(self, message: Message, command: CommandObject) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        entry = normalize_entry(arg) if arg else None
        if not entry:
            await message.reply("Напишите адрес сайта или канала, например <code>example.com</code> или <code>t.me/mychannel</code>.")
            return
        chat_id = message.chat.id
        config = await self.config(chat_id)
        allowed = [e for e in config.allowed_domains if e != entry]
        blocked = [e for e in config.blocked_domains if e != entry]
        if command.command == "allowlink":
            allowed.append(entry)
        elif command.command == "blocklink":
            blocked.append(entry)
        if len(allowed) > MAX_ENTRIES or len(blocked) > MAX_ENTRIES:
            await message.reply(f"В каждом списке может быть не больше {MAX_ENTRIES} адресов.")
            return
        await self.storage.set_links(chat_id, config.links_mode, allowed, blocked)
        await message.reply(self.describe_links(await self.config(chat_id)))

    @staticmethod
    def describe_links(config: ChatConfig) -> str:
        allowed = ", ".join(config.allowed_domains) or "пусто"
        blocked = ", ".join(config.blocked_domains) or "пусто"
        return (
            f"<b>Ссылки:</b> {LINK_MODE_LABEL[config.links_mode]}\n"
            f"Разрешённые: {html.escape(allowed)}\n"
            f"Запрещённые: {html.escape(blocked)}\n\n{LINKS_HELP}"
        )

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

    async def on_cleanservice(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "clean_service", "Удаление служебных сообщений")

    async def on_ocr(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "image_ocr", "Проверка картинок и стикеров новичков")

    async def on_firststrict(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "first_strict", "Строгая проверка первых комментариев")

    async def on_night(self, message: Message, command: CommandObject) -> None:
        arg = await self.admin_args(message, command)
        if arg is None:
            return
        parts = arg.split()
        chat_id = message.chat.id
        if len(parts) == 1 and parts[0] in ("on", "off"):
            await self.storage.set_setting(chat_id, "night_mode", int(parts[0] == "on"))
        elif len(parts) == 2 and all(p.isdigit() and 0 <= int(p) <= 23 for p in parts):
            await self.storage.set_setting(chat_id, "night_from", int(parts[0]))
            await self.storage.set_setting(chat_id, "night_to", int(parts[1]))
            await self.storage.set_setting(chat_id, "night_mode", 1)
        else:
            await message.reply(
                "Напишите <code>/night on</code>, <code>/night off</code> или часы по Москве: "
                "<code>/night 0 7</code> (с полуночи до 7 утра)"
            )
            return
        config = await self.config(chat_id)
        await message.reply(f"Ночной режим: {describe_night(config)}")

    async def on_antiraid(self, message: Message, command: CommandObject) -> None:
        await self._switch(message, command, "antiraid", "Предупреждения о рейдах")
