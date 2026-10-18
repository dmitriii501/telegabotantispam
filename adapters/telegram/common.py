"""Constants, texts and small helpers shared by the Telegram adapter."""

import html
import io
import os
import random
import sqlite3
from typing import Any, cast

from aiogram import Bot, F
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from core.models import ActionType, Decision
from core.moderation import (
    ChatConfig,
)
from core.rules import CONTEXT, EVERYTHING, PERMISSION, Rule

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
# "I am human" check: the newcomer presses the button with the named picture.
CAPTCHA_TIMEOUT = 120  # seconds
CAPTCHA_MUTE_HOURS = 24
CAPTCHA_PICTURES = [
    ("🍎", "яблоко"), ("🐱", "кота"), ("🚗", "машину"), ("⭐", "звезду"),
    ("🌵", "кактус"), ("🎈", "шар"), ("🍕", "пиццу"), ("🐟", "рыбу"),
]
# Raid: many people joining within a minute.
RAID_JOINS = 8
RAID_WINDOW = 60
RAID_COOLDOWN = 600  # one alert per ten minutes
RAID_FREEZE_MINUTES = 30
SERVICE_KINDS = {
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "pinned_message", "video_chat_started", "video_chat_ended", "video_chat_scheduled",
    "video_chat_participants_invited", "message_auto_delete_timer_changed", "forum_topic_created",
    "forum_topic_closed", "forum_topic_reopened", "migrate_to_chat_id", "migrate_from_chat_id",
}
CLEANABLE_SERVICE = SERVICE_KINDS - {"migrate_to_chat_id", "migrate_from_chat_id", "forum_topic_created"}
OCR_TIMEOUT = 15.0  # seconds a newcomer's picture may wait for its text
PROFILE_TTL = 3 * 24 * 3600  # how long a fetched profile is reused
PROFILE_TIMEOUT = 4.0  # seconds a newcomer's first message may wait for the profile lookup

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
SECURE_RANDOM = random.SystemRandom()
MAX_RETRY_WAIT = 30  # seconds: a raid makes Telegram say "slow down"; waiting this long for a removal is worth it
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
LINK_MODE_LABEL = {
    "ai": "решает ИИ: подозрительные ссылки удаляются, остальные можно",
    "block": "разрешены только ссылки из списка",
    "newcomers": "новичкам ссылки нельзя, остальным решает ИИ",
}
LINKS_HELP = (
    "Изменить: <code>/links ai</code>, <code>/links block</code> или <code>/links newcomers</code>; "
    "<code>/allowlink адрес</code> разрешает сайт или канал (например <code>t.me/mychannel</code>), "
    "<code>/blocklink адрес</code> запрещает, <code>/unlink адрес</code> убирает из списков."
)
JEV_PRICE_PER_MTOK = 0.042  # dollars per million input tokens
NOT_ADMIN = "я не админ"
RIGHTS_TEXT = (
    "Чтобы модерировать, сделайте меня админом группы с правами «Удалять сообщения» "
    "и «Блокировать пользователей»."
)

REPORT_THRESHOLD = 3  # different members complaining about one comment before the owner is asked


PRIVACY_TEXT = (
    """<b>Какие данные обрабатывает DefenceAi</b>

<b>Что читает бот.</b> Только сообщения в чатах, куда его добавил администратор. Личные переписки участников он не видит.

<b>Что сохраняется.</b>
• Текст комментария, имя и номер автора, решение бота и причина: в журнале {days} дней, потом удаляется автоматически.
• Отпечаток аватарки (короткий хеш, не картинка) и описание профиля новичка: до {days} дней; отпечатки аватарок забаненных спам-ботов хранятся в общей базе без имён и текстов.
• Текст, прочитанный с картинок и стикеров новичков: до {days} дней.
• Правила и настройки чата: пока бот в чате.

<b>Куда передаётся.</b> Для проверки текст комментария, текст поста, под которым он написан, имя автора и (у новичков) описание профиля отправляются в ИИ-сервис TypeSafe. Картинки никуда не отправляются: текст на них читается на нашем сервере. Другим третьим лицам данные не передаются и не продаются.

<b>Оплата.</b> Платежи проходят через Telegram Stars. Мы храним номер платежа, сумму, срок и номер плательщика, а при оплате картой ещё e-mail для кассового чека; номеров карт у нас нет (платёж принимает банк).

<b>Ваши права.</b> Если бот удалил ваш комментарий, нажмите «Оспорить» под уведомлением. Чтобы удалить ваши данные из журнала раньше срока, напишите администратору чата или владельцу сервиса.

Бот не принимает решений вне чата и не делится репутацией участников между чатами, кроме отпечатков аватарок спам-ботов."""
).replace("{days}", str(RETENTION_DAYS))


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
    "/links [ai|block|newcomers] — ссылки: /allowlink, /blocklink, /unlink адрес — списки сайтов и каналов\n"
    "/firststrict on|off — новички, написавшие в первые 2 минуты после поста (так боты занимают первое место), проверяются строже\n"
    "/spam — участники отвечают этой командой на плохой комментарий; после 3 жалоб бот спросит владельца\n"
    "/plan — тариф, оставшийся срок и оплата\n"
    "/privacy — какие данные хранит бот, /offer — оферта\n"
    "/night on|off|0 7 — ночью (часы по Москве) проверка строже, а новичкам нельзя ссылки\n"
    "/antiraid on|off — предупреждать о массовых заходах; /cleanservice on|off — убирать «вошёл» и «вышел»\n"
    "/ocr on|off — читать текст на картинках и стикерах новичков (рекламу в виде картинки)\n"
    "/captcha on|off — новичок после первого сообщения нажимает нужную кнопку, иначе сообщение удаляется и мут на сутки\n"
    "/profilecheck on|off — проверять профиль новичков; /profile ответом на сообщение покажет, что бот видит\n"
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


# Message types a member can post without any text; service messages ("joined", "pinned") are not among them.
MEDIA_LABEL = {
    "sticker": "[стикер]",
    "photo": "[фото]",
    "video": "[видео]",
    "animation": "[гиф]",
    "voice": "[голосовое]",
    "video_note": "[кружок]",
    "audio": "[аудио]",
    "document": "[файл]",
    "poll": "[опрос]",
    "dice": "[кубик]",
    "contact": "[контакт]",
    "location": "[геопозиция]",
}


def media_label(message) -> str | None:
    kind = getattr(message.content_type, "value", message.content_type)
    return MEDIA_LABEL.get(kind) if isinstance(kind, str) else None


def quote(text: str | None, limit: int = 700) -> str:
    text = text or "[сообщение без текста]"
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


def describe_night(config: ChatConfig) -> str:
    if not config.night_mode:
        return "выключен"
    state = "сейчас идёт" if config.night_active() else "сейчас день"
    return f"с {config.night_from}:00 до {config.night_to}:00 по Москве, {state}"


def describe_antiflood(config: ChatConfig) -> str:
    if not config.antiflood:
        return "выключен"
    return (
        f"{config.flood_messages} сообщений за {config.flood_window} с "
        f"→ мут на {human_duration(config.flood_mute_minutes / 60)}"
    )


def survived(decision: Decision) -> bool:
    """The message stays in the chat and nobody is waiting to decide about it."""
    return decision.action in (ActionType.NONE, ActionType.FORWARD_USEFUL)


def human_duration(hours: float) -> str:
    minutes = round(hours * 60)
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч" if minutes % 60 == 0 else f"{minutes // 60} ч {minutes % 60} мин"


def on_off(value: bool) -> str:
    return "включено" if value else "выключено"


def user_of(message: Message) -> User:
    """The sender of a message. Group and private messages always have one; a missing sender is a bug worth a log line."""
    if message.from_user is None:
        raise ValueError("the message has no sender")
    return message.from_user


def callback_data(call: CallbackQuery) -> str:
    return call.data or ""


async def edit_pressed_message(call: CallbackQuery, text: str) -> None:
    """Replace the text of the message that carried the pressed button (nothing for a message too old to be edited)."""
    message = call.message
    if message is not None and hasattr(message, "edit_text"):
        await cast(Message, message).edit_text(text)


async def append_to_message(call: CallbackQuery, note: str) -> None:
    """Add a line under the message that carried the pressed button."""
    message = call.message
    if message is not None and hasattr(message, "edit_text"):
        message = cast(Message, message)
        await message.edit_text(message.html_text + "\n\n" + note)


async def delete_pressed_message(call: CallbackQuery) -> None:
    message = call.message
    if message is not None and hasattr(message, "delete"):
        await cast(Message, message).delete()


async def download_bytes(bot: Bot, file_id: str) -> bytes | None:
    """The content of a Telegram file, or None when it cannot be fetched."""
    result = await bot.download(file_id)
    if result is None:
        return None
    return (cast(io.BytesIO, result).getvalue() if hasattr(result, "getvalue") else result.read()) or None


RowLike = sqlite3.Row | dict[str, Any]  # a row of the database (or a dict standing in for it)
