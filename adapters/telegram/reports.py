"""Numbers for the owner: stats, the report, the daily digest and the daily jobs."""

import asyncio
import html
import logging
import time
from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject
from aiogram.types import (
    Message,
)

from adapters.telegram.common import (
    CATEGORY_LABEL,
    DECISION_LABEL,
    DIGEST_HOUR_UTC,
    JEV_PRICE_PER_MTOK,
    KIND_LABEL,
    KIND_SINGLE,
    RETENTION_DAYS,
    chat_message_link,
)
from adapters.telegram.review import ReviewMixin
from core.jev_client import JevError
from core.metrics import METRICS
from core.models import DELETING_ACTIONS, ActionType, Author, Comment
from core.storage import DAY

log = logging.getLogger(__name__)


class ReportsMixin(ReviewMixin):
    """Numbers for the owner: stats, the report, the daily digest and the daily jobs."""

    async def on_report(self, message: Message) -> None:
        if not await self.is_admin_message(message):
            return
        config = await self.config(message.chat.id)
        text = await self.build_digest(message.chat.id, int(time.time()) - 7 * DAY, config.observe, "за 7 дней")
        await message.reply(text or "За неделю пока нечего показать: бот ещё ничего не проверял.")

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

    def _log_metrics(self) -> None:
        """Every five minutes one line with the counters, for those who read the log and not Prometheus."""
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 5:
            return
        totals = METRICS.snapshot()
        if totals != getattr(self, "_last_totals", None):
            self._last_totals = totals
            log.info("metrics %s", " ".join(f"{k}={v:g}" for k, v in sorted(totals.items())))

    async def digest_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.heartbeat = time.time()
            self._log_metrics()
            now = datetime.now(timezone.utc)
            if now.hour == DIGEST_HOUR_UTC:
                try:
                    await self.send_digests(now)
                    await self.send_billing_notices()
                    await self.purge_expired(now.strftime("%Y-%m-%d"))
                except Exception:
                    log.exception("Daily job failed")
