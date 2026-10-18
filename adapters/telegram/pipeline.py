"""A comment from arrival to action."""

import asyncio
import html
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ChatPermissions,
    Message,
    User,
)

from adapters.telegram.common import (
    AUTO_REMEMBER_CONFIDENCE,
    CONFLICT_COOLDOWN,
    CONFLICT_MIN_MESSAGES,
    CONFLICT_RECHECK,
    CONFLICT_WINDOW,
    DECISION_LABEL,
    EXPLANATION_TTL,
    FLOOD_LIMITS,
    FLOOD_MESSAGES,
    FLOOD_WINDOW,
    JEV_ALERT_COOLDOWN,
    OCR_TIMEOUT,
    PROFILE_TIMEOUT,
    RIGHTS_ALERT_COOLDOWN,
    SERVICE_KINDS,
    THREAD_BUFFER,
    TRIAL_ADMIN_TESTS_PER_DAY,
    TRIAL_NOTICES_PER_DAY,
    chat_message_link,
    download_bytes,
    human_duration,
    kb,
    media_label,
    quote,
    survived,
    user_of,
)
from adapters.telegram.settings import SettingsMixin
from core.jev_client import JevBusy, JevError
from core.metrics import COMMENT_SECONDS, COMMENTS, TELEGRAM_ERRORS
from core.models import DELETING_ACTIONS, ActionType, Author, Comment, Decision, Verdict
from core.moderation import (
    NEWCOMER_MESSAGES,
    TENSION_ALERT,
    TENSION_MIN_CONFIDENCE,
    ChatConfig,
)
from core.normalizer import example_key

log = logging.getLogger(__name__)


@dataclass
class Incoming:
    """A member's message and who wrote it (a person or a channel)."""

    message: Message
    edited: bool
    text: str
    chat: "Mapping[str, Any] | sqlite3.Row | None"  # the chat's row in the database; None for a chat the bot has not stored yet
    author_id: int
    author_name: str
    username: str | None
    premium: bool
    is_channel: bool


@dataclass
class Prepared:
    """What was gathered about the comment before deciding."""

    comment: Comment
    key: str | None  # the normalized text, the key of the bot's memory of decisions
    known: str | None  # what was decided about this exact text before
    previous: int  # messages of the author in this chat so far
    image_text: str | None


class PipelineMixin(SettingsMixin):
    """A comment from arrival to action."""

    @staticmethod
    def image_source(message: Message) -> tuple[str, str] | None:
        """(file_id, file_unique_id) of a picture worth reading: a photo, a still sticker or a preview frame."""
        kind = getattr(message.content_type, "value", message.content_type)
        if kind == "photo" and message.photo:
            biggest = message.photo[-1]
            return biggest.file_id, biggest.file_unique_id
        if kind == "sticker" and message.sticker:
            sticker = message.sticker
            still = not (sticker.is_animated or sticker.is_video)
            picture = sticker if still else sticker.thumbnail
            return (picture.file_id, picture.file_unique_id) if picture else None
        if kind == "animation" and message.animation and message.animation.thumbnail:
            thumb = message.animation.thumbnail
            return thumb.file_id, thumb.file_unique_id
        return None

    async def read_image_text(self, message: Message) -> str | None:
        """Text found on the message's picture. Every picture is read once: the result is remembered by file."""
        source = self.image_source(message)
        if not source:
            return None
        file_id, unique_id = source
        try:
            cached = await self.storage.get_ocr(unique_id)
            if cached is not None:
                return cached or None
            data = await download_bytes(self.bot, file_id)
            if data is None:
                return None
            text = await asyncio.wait_for(self.ocr.read(data), OCR_TIMEOUT)
        except Exception as e:  # a picture we cannot read is simply treated as having no text
            log.info("ocr: cannot read %s: %s", unique_id, e)
            return None
        await self.storage.save_ocr(unique_id, text)
        return text or None

    # --------------------------------------------------------------- moderation
    async def remember_post(self, message: Message) -> None:
        text = message.text or message.caption or ""  # a photo post without a caption still has a publication time
        date = message.date
        await self.storage.add_post(
            message.chat.id, message.message_id, text, int(date.timestamp()) if isinstance(date, datetime) else None
        )

    async def seconds_after_post(self, message: Message) -> int | None:
        """How long after the channel post the comment was written, if the post is known."""
        parent = message.reply_to_message
        post_id = message.message_thread_id or (
            parent.message_id if parent is not None and parent.is_automatic_forward is True else None
        )
        published = await self.storage.get_post_ts(message.chat.id, post_id) if post_id else None
        if published is None:
            return None
        written = message.date.timestamp() if isinstance(message.date, datetime) else time.time()
        return max(int(written - published), 0)

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
        """One comment from arrival to action. Each step is a method of its own and may end the story early."""
        text = await self._text_to_judge(message, edited)
        if text is None:
            return
        incoming = await self._identify(message, edited, text)
        if incoming is None:
            return
        started = time.perf_counter()
        config = ChatConfig.from_row(incoming.chat)
        prepared = await self._prepare(incoming, config)
        decision = await self._decide(incoming, prepared, config)
        await self._conclude(incoming, prepared, config, decision)
        COMMENT_SECONDS.observe(time.perf_counter() - started)

    async def _text_to_judge(self, message: Message, edited: bool) -> str | None:
        """The text of a member's message, or None when there is nothing to judge (posts, service messages, edits of media)."""
        if message.is_automatic_forward:
            if not edited:
                await self.remember_post(message)
            return None
        kind = getattr(message.content_type, "value", message.content_type)
        if kind in SERVICE_KINDS:
            if not edited:
                await self.handle_service(message, kind)
            return None
        text = message.text or message.caption
        if not text and (edited or not media_label(message)):
            return None  # nothing to judge, and not a member's own message either
        return text or ""  # stickers, photos and the like still count for the antiflood

    async def _identify(self, message: Message, edited: bool, text: str) -> "Incoming | None":
        """Who wrote it. None for everything that is not moderated: a disabled chat, admins, the chat's own channel,
        other bots and trusted people."""
        chat_id = message.chat.id
        chat = await self.storage.get_chat(chat_id)
        if chat and not chat["enabled"]:
            return None
        sender, user = message.sender_chat, message.from_user
        if sender:
            # Posted on behalf of a channel. Anonymous admins (the group itself) and the group's own
            # channel are fine; any other channel is a stranger and is moderated like a person.
            if sender.id == chat_id:
                await self._admin_wrote(message, chat, edited)
                return None
            if sender.id == await self.linked_channel(chat_id):
                return None
            who = Incoming(message, edited, text, chat, sender.id, sender.title or "Канал", sender.username, False, True)
        elif user and not user.is_bot:
            if await self.is_admin(chat_id, user.id):
                await self._admin_wrote(message, chat, edited)
                return None
            who = Incoming(message, edited, text, chat, user.id, user.full_name, user.username, bool(user.is_premium), False)
        else:
            return None  # other bots
        if await self.storage.is_trusted(chat_id, who.author_id):
            return None
        return who

    async def _admin_wrote(self, message: Message, chat, edited: bool) -> None:
        await self.note_admin_reply(message, edited)
        await self.trial_test(message, chat, edited)

    async def _prepare(self, who: "Incoming", config: ChatConfig) -> "Prepared":
        """Everything the decision needs: history of the author, picture text, memory of earlier decisions,
        the post and the message replied to, the profile of a newcomer."""
        message, chat_id = who.message, who.message.chat.id
        previous = await self.storage.message_count(chat_id, who.author_id)
        newcomer = previous < NEWCOMER_MESSAGES
        plain = not config.lockdown and not config.blanket
        image_text = None
        if config.image_ocr and self.ocr.available and not who.is_channel and not who.edited and newcomer and plain:
            image_text = await self.read_image_text(message)
        key = example_key(who.text or image_text or "")
        known = await self.storage.example_kind(chat_id, key) if key else None
        post_text, reply_to_text = await self.comment_context(message) if plain else (None, None)
        profile = None
        if config.profile_check and not who.is_channel and newcomer and not who.edited:
            try:
                profile = await asyncio.wait_for(self.fetch_profile(who.author_id), PROFILE_TIMEOUT)
            except asyncio.TimeoutError:  # a different class from the builtin before Python 3.11
                log.info("profile lookup of %s timed out", who.author_id)
        comment = Comment(
            chat_id=chat_id,
            message_id=message.message_id,
            text=who.text,
            author=Author(
                user_id=who.author_id,
                display_name=who.author_name,
                username=who.username,
                is_premium=who.premium,
                previous_messages=previous,
                previous_deletions=await self.storage.recent_deletions(chat_id, who.author_id),
                profile=profile,
            ),
            post_text=post_text,
            reply_to_text=reply_to_text,
            links=self.extract_links(message),
            seconds_after_post=await self.seconds_after_post(message) if config.first_strict and not who.edited else None,
            image_text=image_text,
        )
        return Prepared(comment, key, known, previous, image_text)

    async def _decide(self, who: "Incoming", prepared: "Prepared", config: ChatConfig) -> Decision:
        """Antiflood first (it costs nothing), then the moderator; the local filter when the AI is down."""
        chat_id = who.message.chat.id
        if config.antiflood and not who.edited and self.flood_hit(chat_id, who.author_id, config.flood_messages, config.flood_window):
            return self.moderator.flood_decision(config.flood_mute_minutes)
        access = await self.access_for(who.chat) if who.chat else None
        try:
            decision = await self.moderator.check(
                prepared.comment, config, prepared.known, ai=access is None or access.ai_allowed
            )
        except JevBusy as e:  # a raid fills the line: the local filter does the work, the owner is not alarmed
            log.warning("Jev line too long for chat %s, using the local fallback filter: %s", chat_id, e)
            return self.moderator.fallback(prepared.comment, config)
        except JevError as e:
            log.error("Jev unavailable, using the local fallback filter: %s", e)
            await self.notify_owner(
                who.chat,
                "jev_down",
                "⚠️ ИИ сейчас недоступен. Пока он не вернётся, работает упрощённая защита: "
                "только заведомый спам со ссылками. Остальные комментарии проходят без проверки.",
                JEV_ALERT_COOLDOWN,
            )
            return self.moderator.fallback(prepared.comment, config)
        if decision.extra.get("no_ai") and access is not None:
            await self.billing_alert(who.chat, access)
        return decision

    async def _conclude(self, who: "Incoming", prepared: "Prepared", config: ChatConfig, decision: Decision) -> None:
        """Count the message, write the log, and carry the decision out (or only record it in the trial mode)."""
        message, chat_id = who.message, who.message.chat.id
        COMMENTS.inc(action=decision.action.value)
        if not who.edited:
            await self.storage.count_message(chat_id, who.author_id)
            if who.text and decision.action not in DELETING_ACTIONS:
                self.spawn(self.watch_thread(message, who.text, config, who.chat))
        if prepared.image_text:
            decision.extra["image_text"] = prepared.image_text
        verdict = decision.verdict
        if verdict is None:  # nothing to record: skipped without asking Jev
            await self._challenge_if_due(who, prepared, config, decision, observe_blocks=True)
            return
        log_id = await self._write_log(who, decision, verdict)
        log.info("chat=%s author=%s action=%s edited=%s %s", chat_id, who.author_id, decision.action.value, who.edited, verdict)
        if config.observe:
            await self.storage.set_executed(log_id, False)  # a dry run: recorded, not carried out
            if decision.action in DELETING_ACTIONS:
                await self.trial_notice(message, decision, log_id, who.chat, who.author_name)
            return
        if prepared.key and decision.action == ActionType.DELETE_AND_BAN and verdict.confidence >= AUTO_REMEMBER_CONFIDENCE:
            await self.storage.add_example(chat_id, prepared.key, "remove")
        if not await self.execute(message, decision, log_id, who.chat):
            await self.storage.set_executed(log_id, False)
        elif decision.action == ActionType.DELETE_AND_BAN and verdict.category.value == "spam" and not who.is_channel:
            await self.learn_avatar(who.author_id)
        await self._challenge_if_due(who, prepared, config, decision)

    async def _write_log(self, who: "Incoming", decision: Decision, verdict: Verdict) -> int:
        message = who.message
        log_id = await self.storage.add_log(
            chat_id=message.chat.id,
            message_id=message.message_id,
            user_id=who.author_id,
            user_name=who.author_name,
            text=who.text or self.shown_text(message, decision) or media_label(message) or "[сообщение без текста]",
            category=verdict.category.value,
            confidence=verdict.confidence,
            bot_probability=verdict.bot_probability,
            action=decision.action.value,
            reason=decision.reason,
            tokens=verdict.input_tokens,
            kind=verdict.kind,
            lead=verdict.lead,
            needs_answer=verdict.needs_answer,
            sentiment=verdict.sentiment,
            violation=verdict.violation_probability,
        )
        if verdict.input_tokens > 0:
            await self.storage.add_usage(message.chat.id)
        self.note_usage(message.chat.id, verdict.input_tokens)
        return log_id

    async def _challenge_if_due(
        self, who: "Incoming", prepared: "Prepared", config: ChatConfig, decision: Decision, observe_blocks: bool = False
    ) -> None:
        """The "I am a human" check for a newcomer whose message stayed."""
        if not config.captcha or who.is_channel or who.edited or not survived(decision):
            return
        if observe_blocks and config.observe:
            return
        await self.maybe_challenge(who.message, who.author_id, prepared.previous)

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
            f"{quote(self.shown_text(message, decision))}\n"
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
        target = chat["owner_id"] if anonymous or user is None else user.id
        if await self.dm(target, answer):
            return
        try:  # the admin has not started the bot: answer in the chat and tidy up soon
            note = await message.reply(answer)
            self.spawn(self.delete_later(chat_id, note.message_id, EXPLANATION_TTL))
        except TelegramAPIError as e:
            log.info("Trial answer not delivered: %s", e)

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
            buf.append((now, user_of(message).full_name, text[:300]))
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
                [{"author": name, "text": t} for _, name, t in recent[-15:]], key=chat_id
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
        sender = message.sender_chat
        user = cast(User, message.from_user)  # a comment from a channel has a sender chat, every other message a user
        name = (sender.title if sender else user.full_name) or ""
        shown = message.text or message.caption
        try:
            if action in DELETING_ACTIONS:
                await self.tg_retry(message.delete)
            # A channel cannot be warned or muted, so any removal of its comment bans the channel.
            if action == ActionType.DELETE_AND_BAN or (sender and action in DELETING_ACTIONS):
                if sender:
                    await self.tg_retry(self.bot.ban_chat_sender_chat, message.chat.id, sender.id)
                else:
                    await self.tg_retry(self.bot.ban_chat_member, message.chat.id, user.id)
            elif action in (ActionType.DELETE_AND_EXPLAIN, ActionType.DELETE_AND_MUTE) and not sender:
                hours = decision.extra.get("mute_hours")
                if hours:
                    await self.tg_retry(
                        self.bot.restrict_chat_member,
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
                    f"Бот уверен на {(v.confidence if v else 0):.0%}:\n"
                    f"{quote(self.shown_text(message, decision))}\n{chat_message_link(message.chat.id, message.message_id, message.chat.username)}",
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
            TELEGRAM_ERRORS.inc(action=action.value)
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
