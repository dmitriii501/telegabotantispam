"""What the owner decides afterwards: appeals, review of doubtful comments, complaints."""

import html
import logging
from contextlib import suppress

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    Message,
)

from adapters.telegram.common import (
    REPORT_THRESHOLD,
    append_to_message,
    callback_data,
    chat_message_link,
    kb,
    quote,
)
from adapters.telegram.newcomers import NewcomerMixin
from core.models import ActionType

log = logging.getLogger(__name__)


class ReviewMixin(NewcomerMixin):
    """What the owner decides afterwards: appeals, review of doubtful comments, complaints."""

    async def on_trial_verdict(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        right = callback_data(call).startswith("obs:ok")
        await self.storage.set_feedback(entry["id"], "confirmed" if right else "not_spam")
        if right and entry["action"] == ActionType.DELETE_AND_BAN.value and entry["category"] == "spam":
            await self.learn_avatar(entry["user_id"])
        elif not right:
            await self.forget_avatar(entry["user_id"])
        await append_to_message(
            call, "✅ Записал: бот прав." if right else "↩️ Записал: бот ошибся, такие комментарии буду пропускать."
        )
        await call.answer()

    async def on_spam_report(self, message: Message) -> None:
        """A member replies `/spam` to a comment. Enough different people complaining sends it to the owner."""
        target, user = message.reply_to_message, message.from_user
        with suppress(TelegramAPIError):
            await message.delete()  # the complaint itself should not stay in the chat
        if not target or not user or target.from_user is None and target.sender_chat is None:
            return
        offender = target.from_user
        if (
            target.message_id == message.message_id
            or (offender and (offender.id == user.id or offender.is_bot))
            or (offender and await self.is_admin(message.chat.id, offender.id))
            or (target.sender_chat and target.sender_chat.id == message.chat.id)
        ):
            return
        if offender and await self.storage.is_trusted(message.chat.id, offender.id):
            return
        count = await self.storage.add_report(message.chat.id, target.message_id, user.id)
        if count != REPORT_THRESHOLD:  # exactly once, when the threshold is reached
            return
        chat = await self.storage.get_chat(message.chat.id)
        text = target.text or target.caption or f"[{getattr(target.content_type, 'value', target.content_type)}]"
        if target.sender_chat is not None:
            author_id, name = target.sender_chat.id, target.sender_chat.title or "Канал"
        elif offender is not None:
            author_id, name = offender.id, offender.full_name
        else:
            return
        log_id = await self.storage.add_log(
            chat_id=message.chat.id,
            message_id=target.message_id,
            user_id=author_id,
            user_name=name,
            text=text,
            category="normal",
            confidence=0.0,
            bot_probability=0.0,
            action=ActionType.SEND_TO_REVIEW.value,
            reason=f"жалобы участников ({count})",
        )
        title = html.escape(await self.chat_title(message.chat.id))
        await self.dm(
            chat["owner_id"] if chat else None,
            f"🚩 На комментарий в чате «{title}» пожаловались {count} участника.\n"
            f"Автор: {html.escape(name)}:\n{quote(text)}\n"
            f"{chat_message_link(message.chat.id, target.message_id, message.chat.username)}",
            kb([("🗑 Удалить", f"rev:del:{log_id}"), ("✅ Оставить", f"rev:keep:{log_id}")]),
        )

    # ------------------------------------------------------------------ appeals
    async def on_appeal(self, call: CallbackQuery) -> None:
        log_id = int(callback_data(call).split(":")[1])
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
        log_id = int(callback_data(call).split(":")[2])
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
        await self.forget_avatar(user_id)
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
        if callback_data(call).startswith("ap:ok"):
            await self.restore_comment(entry)
            await append_to_message(call, "↩️ Комментарий восстановлен.")
        else:
            await self.storage.set_feedback(entry["id"], "confirmed")
            await append_to_message(call, "🗑 Удаление подтверждено.")
        await call.answer()

    async def on_review_decision(self, call: CallbackQuery) -> None:
        entry = await self._owner_entry(call)
        if not entry:
            return
        result = await self.resolve_review(entry, callback_data(call).startswith("rev:del"))
        await append_to_message(call, result)
        await call.answer()
