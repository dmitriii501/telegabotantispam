"""Newcomers: the profile check, the "I am human" button, joins and raids."""

import asyncio
import contextlib
import html
import logging
import time
from collections import deque
from contextlib import suppress
from datetime import timedelta

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    Message,
)

from adapters.telegram.common import (
    CAPTCHA_MUTE_HOURS,
    CAPTCHA_PICTURES,
    CAPTCHA_TIMEOUT,
    CLEANABLE_SERVICE,
    PROFILE_TTL,
    RAID_COOLDOWN,
    RAID_FREEZE_MINUTES,
    RAID_JOINS,
    RAID_WINDOW,
    SECURE_RANDOM,
    append_to_message,
    callback_data,
    delete_pressed_message,
    download_bytes,
    kb,
    media_label,
    on_off,
    user_of,
)
from adapters.telegram.growth import GrowthMixin
from core.avatar import dhash
from core.models import ActionType, Profile
from core.moderation import (
    ChatConfig,
)
from core.storage import DAY

log = logging.getLogger(__name__)


class NewcomerMixin(GrowthMixin):
    """Newcomers: the profile check, the "I am human" button, joins and raids."""

    async def _load_profile(self, user_id: int):
        """Ask Telegram for the bio and the picture and store what came. Failures only mean "unknown"."""
        bio = has_photo = avatar_hash = None
        try:
            info = await self.bot.get_chat(user_id)
            bio = info.bio if isinstance(getattr(info, "bio", None), str) else None
        except Exception as e:  # any failure means "bio unknown"; moderation must never depend on it
            log.info("profile: bio of %s unavailable: %s", user_id, e)
        try:
            photos = await self.bot.get_user_profile_photos(user_id, limit=1)
            has_photo = photos.total_count > 0
            if has_photo:
                sizes = photos.photos[0]
                data = await download_bytes(self.bot, sizes[min(1, len(sizes) - 1)].file_id)
                avatar_hash = dhash(data) if data else None
        except Exception as e:
            log.info("profile: picture of %s unavailable: %s", user_id, e)
        await self.storage.save_profile(user_id, bio, has_photo, avatar_hash)
        log.info("profile: user=%s bio=%s photo=%s fingerprint=%s", user_id, bio is not None, has_photo, bool(avatar_hash))
        return await self.storage.get_profile(user_id, PROFILE_TTL)

    async def fetch_profile(self, user_id: int) -> Profile | None:
        """Bio, picture and whether the picture belongs to a known spammer; cached for a few days. Never raises."""
        try:
            row = await self.storage.get_profile(user_id, PROFILE_TTL) or await self._load_profile(user_id)
            if row is None:
                return None
            match = bool(row["avatar_hash"]) and await self.storage.avatar_banned(row["avatar_hash"])
            return Profile(
                bio=row["bio"], has_photo=None if row["has_photo"] is None else bool(row["has_photo"]), avatar_match=match
            )
        except Exception:
            log.exception("profile lookup failed for %s", user_id)
            return None

    async def _avatar_hash(self, user_id: int) -> str | None:
        row = await self.storage.get_profile(user_id, 30 * DAY) or await self._load_profile(user_id)
        return row["avatar_hash"] if row else None

    async def learn_avatar(self, user_id: int) -> None:
        """A spam bot was banned: remember its picture, so the next account with it is caught at once."""
        if user_id > 0 and (fingerprint := await self._avatar_hash(user_id)):
            await self.storage.add_avatar_ban(fingerprint)

    async def forget_avatar(self, user_id: int) -> None:
        """The admin says the person was not a spammer: their picture must not count against anybody."""
        if user_id > 0 and (fingerprint := await self._avatar_hash(user_id)):
            await self.storage.remove_avatar_ban(fingerprint)

    # ------------------------------------------------------------ "I am human" check
    async def maybe_challenge(self, message: Message, user_id: int, previous_messages: int) -> None:
        """A newcomer's first message got through: ask them to press the right button, or lose the message."""
        chat_id = message.chat.id
        if previous_messages > 0 or (chat_id, user_id) in self._captcha or await self.storage.is_verified(chat_id, user_id):
            return
        chat = await self.storage.get_chat(chat_id)
        if ChatConfig.from_row(chat).yields_to_others:
            return  # another bot already asks newcomers to prove they are human: one check is enough
        options = SECURE_RANDOM.sample(range(len(CAPTCHA_PICTURES)), 4)  # not guessable from earlier checks
        answer = SECURE_RANDOM.choice(options)
        user = user_of(message)
        mention = f'<a href="tg://user?id={user_id}">{html.escape(user.first_name)}</a>'
        markup = kb([(CAPTCHA_PICTURES[i][0], f"cap:{chat_id}:{user_id}:{i}") for i in options])
        try:
            notice = await self.bot.send_message(
                chat_id,
                await self.signed(
                    f"{mention}, подтвердите, что вы человек: нажмите на {CAPTCHA_PICTURES[answer][1]}. "
                    f"Иначе ваше первое сообщение будет удалено.",
                    chat,
                ),
                reply_to_message_id=message.message_id,
                reply_markup=markup,
            )
        except TelegramAPIError as e:
            log.info("Cannot post the human check: %s", e)
            return
        self._captcha[(chat_id, user_id)] = {
            "answer": answer,
            "message_id": message.message_id,
            "notice_id": notice.message_id,
            "name": user.full_name,
            "text": message.text or message.caption or media_label(message) or "[сообщение без текста]",
        }
        self.spawn(self._captcha_timeout(chat_id, user_id))

    async def _captcha_timeout(self, chat_id: int, user_id: int) -> None:
        await asyncio.sleep(CAPTCHA_TIMEOUT)
        if (chat_id, user_id) in self._captcha:
            await self.fail_captcha(chat_id, user_id, "не ответил вовремя")

    async def fail_captcha(self, chat_id: int, user_id: int, why: str) -> None:
        pending = self._captcha.pop((chat_id, user_id), None)
        if not pending:
            return
        for message_id in (pending["notice_id"], pending["message_id"]):
            with contextlib.suppress(TelegramAPIError):
                await self.bot.delete_message(chat_id, message_id)
        try:
            await self.bot.restrict_chat_member(
                chat_id, user_id, permissions=ChatPermissions(can_send_messages=False),
                until_date=timedelta(hours=CAPTCHA_MUTE_HOURS),
            )
        except TelegramAPIError as e:
            log.warning("Cannot mute after a failed human check: %s", e)
        await self.storage.add_log(
            chat_id=chat_id, message_id=pending["message_id"], user_id=user_id, user_name=pending["name"],
            text=pending["text"], category="captcha", confidence=1.0, bot_probability=1.0,
            action=ActionType.DELETE_AND_MUTE.value, reason=f"не прошёл проверку «я человек»: {why}", tokens=0,
        )

    async def on_captcha_button(self, call: CallbackQuery) -> None:
        _, raw_chat, raw_user, raw_picked = callback_data(call).split(":")
        chat_id, user_id, picked = int(raw_chat), int(raw_user), int(raw_picked)
        if call.from_user.id != user_id:
            await call.answer("Эта кнопка не для вас.", show_alert=True)
            return
        pending = self._captcha.get((chat_id, user_id))
        if pending is None:
            await call.answer("Проверка устарела. Просто напишите сообщение ещё раз.", show_alert=True)
            with contextlib.suppress(TelegramAPIError):
                await delete_pressed_message(call)
            return
        if picked != pending["answer"]:
            await call.answer("Неверно.", show_alert=True)
            await self.fail_captcha(chat_id, user_id, "нажал не ту кнопку")
            return
        self._captcha.pop((chat_id, user_id), None)
        await self.storage.set_verified(chat_id, user_id)
        with contextlib.suppress(TelegramAPIError):
            await delete_pressed_message(call)
        await call.answer("Спасибо, проверка пройдена!")

    async def on_profile(self, message: Message) -> None:
        """Ответом на сообщение: что бот видит в профиле этого человека (заодно проверка, что Telegram отдаёт боту)."""
        if not await self.is_admin_message(message):
            return
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not target:
            await message.reply("Ответьте этой командой на сообщение человека.")
            return
        await self.storage.forget_profile(target.id)  # always a fresh look
        profile = await self.fetch_profile(target.id)
        if profile is None:
            await message.reply("Не удалось получить профиль.")
            return
        bio = "недоступно боту" if profile.bio is None else (html.escape(profile.bio) or "пусто")
        photo = {None: "недоступно боту", True: "есть", False: "нет"}[profile.has_photo]
        base = "совпадает с аватаркой известного спамера" if profile.avatar_match else "нет совпадений"
        await message.reply(
            f"<b>{html.escape(target.full_name)}</b> "
            f"{'@' + html.escape(target.username) if target.username else '(без ника)'}\n"
            f"Описание профиля: {bio}\n"
            f"Фото профиля: {photo}\n"
            f"База аватарок спамеров: {base}\n"
            f"Проверка профиля новичков: {on_off((await self.config(message.chat.id)).profile_check)}"
        )

    # ------------------------------------------------- service messages and raids
    async def handle_service(self, message: Message, kind: str) -> None:
        chat_id = message.chat.id
        chat = await self.storage.get_chat(chat_id)
        if not chat or not chat["enabled"]:
            return
        config = ChatConfig.from_row(chat)
        if kind == "new_chat_members" and config.antiraid:
            joined = len(message.new_chat_members) if isinstance(message.new_chat_members, list) else 1
            count = self.raid_count(chat_id, max(joined, 1))
            if count:
                await self.raid_alert(chat_id, chat, count)
        if config.clean_service and not config.observe and kind in CLEANABLE_SERVICE:
            with suppress(TelegramAPIError):  # no right to delete, or already gone
                await message.delete()

    def raid_count(self, chat_id: int, joined: int) -> int | None:
        """Register joins; the number of people who joined in the last minute once it looks like a raid, else None."""
        now = time.time()
        stamps = self._joins.setdefault(chat_id, deque(maxlen=200))
        stamps.extend([now] * joined)
        recent = sum(1 for t in stamps if now - t <= RAID_WINDOW)
        if recent < RAID_JOINS or now - self._raid_alerted.get(chat_id, 0) < RAID_COOLDOWN:
            return None
        self._raid_alerted[chat_id] = now
        return recent

    async def raid_alert(self, chat_id: int, chat, count: int) -> None:
        title = html.escape(await self.chat_title(chat_id))
        await self.dm(
            chat["owner_id"],
            f"🚨 Возможный рейд в чате «{title}»: {count} новых участников за минуту.\n"
            f"Если это не ожидаемый наплыв (например, после большого поста), можно на {RAID_FREEZE_MINUTES} минут "
            "включить удаление всех сообщений участников: спамеры не смогут ничего написать.",
            kb([(f"🔒 Заморозить на {RAID_FREEZE_MINUTES} мин", f"raid:on:{chat_id}"), ("Это нормально", f"raid:no:{chat_id}")]),
        )

    async def on_raid_button(self, call: CallbackQuery) -> None:
        _, action, raw_chat = callback_data(call).split(":")
        chat_id = int(raw_chat)
        if not await self.is_admin(chat_id, call.from_user.id, fresh=True):
            await call.answer("Это могут сделать только админы.", show_alert=True)
            return
        if action == "on":
            until = int(time.time()) + RAID_FREEZE_MINUTES * 60
            await self.storage.set_lockdown(chat_id, True, until)
            self.spawn(self._end_freeze(chat_id, until))
            note = f"🔒 Заморозил на {RAID_FREEZE_MINUTES} минут: все сообщения участников удаляются. Снять раньше: /lockdown off"
        else:
            note = "Хорошо, ничего не меняю."
        await append_to_message(call, note)
        await call.answer()

    async def _end_freeze(self, chat_id: int, until: int) -> None:
        await asyncio.sleep(max(until - time.time(), 0))
        chat = await self.storage.get_chat(chat_id)
        if chat and chat["lockdown"] and chat["lockdown_until"] == until:
            await self.storage.set_lockdown(chat_id, False)
            await self.dm(chat["owner_id"], f"🔓 Заморозка чата «{html.escape(await self.chat_title(chat_id))}» снята.")
