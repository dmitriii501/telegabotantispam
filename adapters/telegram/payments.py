"""Selling the service in Telegram: trial and plan status, payment in Telegram Stars, notices before the end.

The owner of a chat pays for that chat. A payment extends the chat's `paid_until`; when it runs out the bot keeps
everything that works in code (link lists, antiflood, raid and captcha, local spam filter) and only the AI check stops.
"""

import html
import logging
import os
import re
import time
from datetime import datetime

from aiogram import F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from core.billing import DAY, PERIOD_DAYS, PLANS, TERMS, TRIAL_DAYS, Access, access, price
from core.models import DELETING_ACTIONS
from core.moderation import MOSCOW

log = logging.getLogger(__name__)

USAGE_TTL = 120  # seconds the cached count of AI checks may be old
EXPIRED_ALERT_COOLDOWN = 3 * DAY
LIMIT_ALERT_COOLDOWN = DAY
REMIND_DAYS = (3, 1)  # warn this many days before the end

PAYSUPPORT_TEXT = (
    "💬 <b>Вопросы по оплате</b>\n\n"
    "Платёж прошёл, а доступ не появился? Или сервис не подошёл и нужен возврат?\n"
    "Напишите {contact} и пришлите номер платежа из чека Telegram.\n"
    "Если сервис не подошёл, вернём Stars в течение 7 дней после оплаты."
)


def stars(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ⭐"


def number(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def date_text(ts: int) -> str:
    return datetime.fromtimestamp(ts, MOSCOW).strftime("%d.%m.%Y")


def days_text(n: int) -> str:
    last = n % 10 if n % 100 not in range(11, 15) else 0
    return f"{n} " + ("день" if last == 1 else "дня" if last in (2, 3, 4) else "дней")


def link_button(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


class PaymentsMixin:
    """Mixed into TelegramAdapter; relies on its bot, storage, dm, is_admin, is_admin_message, chat_title, _notified."""

    def _init_payments(self) -> None:
        self.bot_username: str | None = None  # set at startup; deep links to the payment screen need it
        self._usage: dict[int, tuple[float, int]] = {}
        ids = os.getenv("OPERATOR_IDS") or os.getenv("ALERT_CHAT_ID") or ""
        self.operators = {int(x) for x in re.split(r"[,\s]+", ids) if x.strip().lstrip("-").isdigit()}
        self.support_contact = os.getenv("SUPPORT_CONTACT", "").strip() or "владельцу сервиса"

    def _register_payments(self, r) -> None:
        private = F.chat.type == ChatType.PRIVATE
        r.message(Command("plan", "buy"))(self.on_plan)
        r.message(Command("paysupport"))(self.on_paysupport)
        r.message(Command("grant"), private)(self.on_grant)
        r.message(Command("refund"), private)(self.on_refund)
        r.message(Command("revenue"), private)(self.on_revenue)
        r.callback_query(F.data.startswith("plan:"))(self.on_plan_button)
        r.callback_query(F.data.startswith("buy:"))(self.on_buy_button)
        r.pre_checkout_query()(self.on_pre_checkout)
        r.message(F.successful_payment)(self.on_paid)

    # ------------------------------------------------------------------ access

    async def access_for(self, chat) -> Access:
        chat_id = chat["chat_id"]
        cached = self._usage.get(chat_id)
        if cached and time.time() - cached[0] < USAGE_TTL:
            used = cached[1]
        else:
            used = await self.storage.ai_checks(chat_id, int(time.time()) - PERIOD_DAYS * DAY)
            self._usage[chat_id] = (time.time(), used)
        return access(chat, used)

    def note_usage(self, chat_id: int, tokens: int) -> None:
        """One more comment was read by the AI: keep the cached count current between refreshes."""
        cached = self._usage.get(chat_id)
        if cached and tokens > 0:
            self._usage[chat_id] = (cached[0], cached[1] + 1)

    def pay_url(self, chat_id: int) -> str | None:
        return f"https://t.me/{self.bot_username}?start=pay_{chat_id}" if self.bot_username else None

    def pay_markup(self, chat_id: int, text: str = "💳 Выбрать тариф") -> InlineKeyboardMarkup | None:
        url = self.pay_url(chat_id)
        return link_button(text, url) if url else None

    @staticmethod
    def access_text(acc: Access) -> str:
        used = f"Проверено ИИ за 30 дней: {number(acc.used)} из {number(acc.limit)}."
        if acc.state == "paid":
            return (
                f"Тариф «{acc.title}» оплачен до {date_text(acc.until)} (осталось {days_text(acc.days_left)}). {used}"
            )
        if acc.state == "trial":
            return f"Пробный период: осталось {days_text(acc.days_left)}, до {date_text(acc.until)}. {used}"
        return (
            f"Срок закончился {date_text(acc.until)}. ИИ-проверка выключена: работают списки ссылок, антифлуд, "
            "защита от рейдов и очевидного спама."
        )

    async def billing_alert(self, chat, acc: Access) -> None:
        """A comment arrived but the AI may not read it: tell the owner once in a while, with a way to fix it."""
        if acc.state == "expired":
            key, cooldown = "billing_expired", EXPIRED_ALERT_COOLDOWN
            text = (
                f"⏸ {self.access_text(acc)}\n"
                "Чтобы вернуть умную проверку, выберите тариф. Все настройки и правила сохранены."
            )
        else:
            key, cooldown = "billing_limit", LIMIT_ALERT_COOLDOWN
            text = (
                f"⏸ Лимит ИИ-проверок исчерпан: {number(acc.used)} из {number(acc.limit)} за 30 дней. "
                "Пока работают списки ссылок, антифлуд и защита от очевидного спама. "
                "Тариф «Про» вмещает в 5 раз больше."
            )
        await self.notify_owner(chat, key, text, cooldown, self.pay_markup(chat["chat_id"]))

    # ---------------------------------------------------------------- commands

    async def on_plan(self, message: Message, command: CommandObject) -> None:
        if message.chat.type == ChatType.PRIVATE:
            await self.plan_in_private(message)
            return
        if not await self.is_admin_message(message):
            return
        chat = await self.storage.get_chat(message.chat.id)
        if not chat:
            return
        acc = await self.access_for(chat)
        markup = self.pay_markup(message.chat.id, "💳 Продлить или сменить тариф" if acc.state == "paid" else "💳 Выбрать тариф")
        await message.reply(f"💳 {self.access_text(acc)}\nID чата: <code>{message.chat.id}</code>", reply_markup=markup)

    async def plan_in_private(self, message: Message) -> None:
        chats = await self.storage.chats_of_owner(message.from_user.id)
        if not chats:
            await message.answer(
                "У вас пока нет чатов с ботом. Добавьте меня в группу обсуждений канала, сделайте админом "
                "и напишите там /rules. Первые " + days_text(TRIAL_DAYS) + " бесплатно."
            )
            return
        if len(chats) == 1:
            await self.show_plans(message.from_user.id, chats[0]["chat_id"])
            return
        rows = []
        for chat in chats:
            acc = await self.access_for(chat)
            title = await self.chat_title(chat["chat_id"])
            mark = {"paid": "✅", "trial": "🕐", "expired": "⏸"}[acc.state]
            rows.append([InlineKeyboardButton(text=f"{mark} {title}"[:60], callback_data=f"plan:{chat['chat_id']}")])
        await message.answer("Для какого чата тариф?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_plan_button(self, call: CallbackQuery) -> None:
        chat_id = int(call.data.split(":")[1])
        if not await self.is_admin(chat_id, call.from_user.id, fresh=True):
            await call.answer("Оплатить может только админ этого чата.", show_alert=True)
            return
        await self.show_plans(call.from_user.id, chat_id)
        await call.answer()

    async def start_payment(self, message: Message, raw: str) -> bool:
        """`/start pay_<chat id>`: the deep link from the group. True when the message was handled here."""
        try:
            chat_id = int(raw)
        except ValueError:
            return False
        chat = await self.storage.get_chat(chat_id)
        if not chat or not await self.is_admin(chat_id, message.from_user.id, fresh=True):
            await message.answer("Оплатить тариф может только админ этого чата. Откройте ссылку из чата, где вы админ.")
            return True
        await self.show_plans(message.from_user.id, chat_id)
        return True

    async def show_plans(self, user_id: int, chat_id: int) -> None:
        chat = await self.storage.get_chat(chat_id)
        if not chat:
            return
        acc = await self.access_for(chat)
        title = html.escape(await self.chat_title(chat_id))
        lines = [f"💳 <b>Тариф для чата «{title}»</b>", self.access_text(acc), ""]
        for plan in PLANS.values():
            lines.append(
                f"<b>{plan.title}</b>: {stars(plan.stars)} в месяц. До {number(plan.checks)} комментариев в месяц "
                f"с ИИ-разбором (около {number(plan.per_day)} в день): {plan.pitch}."
            )
        lines += [
            "",
            "Оплата звёздами Telegram. Месяц — 30 дней. За 3 месяца скидка 10%, за год 25%.",
            "Правила, настройки и журнал сохраняются при любом сроке.",
        ]
        if acc.state == "paid":
            lines.append("Продление того же тарифа добавляет время к оставшемуся. Смена тарифа начинает новый срок с сегодняшнего дня.")
        rows = []
        for plan in PLANS.values():
            for months in TERMS:
                label = {1: "1 месяц", 3: "3 месяца", 12: "12 месяцев"}[months]
                rows.append([InlineKeyboardButton(
                    text=f"{plan.title} · {label} — {stars(price(plan, months))}",
                    callback_data=f"buy:{chat_id}:{plan.key}:{months}",
                )])
        await self.dm(user_id, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_paysupport(self, message: Message) -> None:
        await message.answer(PAYSUPPORT_TEXT.format(contact=html.escape(self.support_contact)))

    # ----------------------------------------------------------------- payment

    @staticmethod
    def parse_payload(payload: str) -> tuple[int, str, int] | None:
        try:
            chat_id, plan, months = payload.split(":")
            chat_id, months = int(chat_id), int(months)
        except (ValueError, AttributeError):
            return None
        return (chat_id, plan, months) if plan in PLANS and months in TERMS else None

    async def on_buy_button(self, call: CallbackQuery) -> None:
        parsed = self.parse_payload(call.data.split(":", 1)[1])
        if not parsed:
            await call.answer("Некорректный выбор.", show_alert=True)
            return
        chat_id, plan_key, months = parsed
        if not await self.is_admin(chat_id, call.from_user.id, fresh=True):
            await call.answer("Оплатить может только админ этого чата.", show_alert=True)
            return
        plan = PLANS[plan_key]
        title = await self.chat_title(chat_id)
        try:
            await self.bot.send_invoice(
                chat_id=call.from_user.id,
                title=f"DefenceAi «{plan.title}», {months} мес."[:32],
                description=f"Модерация чата «{title}»: {months * PERIOD_DAYS} дней, до {number(plan.checks)} ИИ-проверок в месяц."[:255],
                payload=f"{chat_id}:{plan_key}:{months}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{plan.title}, {months} мес.", amount=price(plan, months))],
            )
        except TelegramAPIError as e:
            log.warning("Cannot send the invoice to %s: %s", call.from_user.id, e)
            await call.answer("Не получилось создать счёт. Попробуйте ещё раз.", show_alert=True)
            return
        await call.answer()

    async def on_pre_checkout(self, query: PreCheckoutQuery) -> None:
        """Telegram asks: may this payment go ahead? Check it is one of our offers at the right price."""
        parsed = self.parse_payload(query.invoice_payload)
        problem = None
        if not parsed:
            problem = "Неизвестный товар. Откройте тарифы заново командой /plan."
        else:
            chat_id, plan_key, months = parsed
            if query.currency != "XTR" or query.total_amount != price(PLANS[plan_key], months):
                problem = "Цена изменилась. Откройте тарифы заново командой /plan."
            elif not await self.storage.get_chat(chat_id):
                problem = "Чат не найден. Добавьте бота в чат и повторите."
        await query.answer(ok=problem is None, error_message=problem)

    async def on_paid(self, message: Message) -> None:
        pay = message.successful_payment
        parsed = self.parse_payload(pay.invoice_payload)
        if not parsed:
            log.error("Paid an unknown payload %r (charge %s)", pay.invoice_payload, pay.telegram_payment_charge_id)
            await message.answer("Платёж получен, но я не смог его привязать к чату. Напишите /paysupport, всё исправим.")
            return
        chat_id, plan_key, months = parsed
        fresh = await self.storage.record_payment(
            pay.telegram_payment_charge_id, chat_id, message.from_user.id, plan_key, months, pay.total_amount
        )
        if not fresh:
            return  # the same notice again
        self._usage.pop(chat_id, None)
        chat = await self.storage.get_chat(chat_id)
        acc = await self.access_for(chat)
        title = html.escape(await self.chat_title(chat_id))
        await message.answer(
            f"✅ Спасибо! Тариф «{PLANS[plan_key].title}» для чата «{title}» оплачен до {date_text(acc.until)}.\n"
            "Правила и настройки на месте, ИИ-проверка работает. Вопросы по оплате: /paysupport"
        )
        for operator in self.operators:
            await self.dm(operator, f"💰 Оплата: {stars(pay.total_amount)}, «{PLANS[plan_key].title}» на {months} мес., чат «{title}»")
        log.info("payment chat=%s plan=%s months=%s stars=%s", chat_id, plan_key, months, pay.total_amount)

    # ---------------------------------------------------------- the operator's tools

    def is_operator(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.operators)

    async def on_grant(self, message: Message, command: CommandObject) -> None:
        """/grant <chat id> <days> [plan]: access by hand (a card payment, a gift, a partner). Negative days take it back."""
        if not self.is_operator(message):
            return
        parts = (command.args or "").split()
        try:
            chat_id, days = int(parts[0]), int(parts[1])
            plan = parts[2] if len(parts) > 2 else "start"
            if plan not in PLANS:
                raise ValueError
        except (IndexError, ValueError):
            await message.answer("Формат: <code>/grant ID_чата дни [start|pro]</code>, например <code>/grant -1001234567890 30 start</code>")
            return
        if not await self.storage.get_chat(chat_id):
            await message.answer("Такого чата нет в базе: бота нужно сначала добавить в чат.")
            return
        until = await self.storage.grant(chat_id, plan, days)
        self._usage.pop(chat_id, None)
        await message.answer(f"Готово: «{PLANS[plan].title}» до {date_text(until)}.")

    async def on_refund(self, message: Message, command: CommandObject) -> None:
        """/refund <charge id>: give the Stars back and take the months away."""
        if not self.is_operator(message):
            return
        charge_id = (command.args or "").strip()
        payment = await self.storage.get_payment(charge_id) if charge_id else None
        if not payment or payment["refunded"]:
            await message.answer("Платёж не найден или уже возвращён. Формат: <code>/refund номер_платежа</code>")
            return
        try:
            await self.bot.refund_star_payment(user_id=payment["payer_id"], telegram_payment_charge_id=charge_id)
        except TelegramAPIError as e:
            await message.answer(f"Telegram отказал: {html.escape(str(e))}")
            return
        await self.storage.mark_refunded(charge_id)
        self._usage.pop(payment["chat_id"], None)
        await message.answer(f"Возвращено {stars(payment['stars'])}, срок уменьшен на {payment['months']} мес.")

    async def on_revenue(self, message: Message) -> None:
        if not self.is_operator(message):
            return
        now = int(time.time())
        month, total = await self.storage.revenue(now - 30 * DAY), await self.storage.revenue()
        chats = await self.storage.all_chats()
        states: dict[str, int] = {"paid": 0, "trial": 0, "expired": 0}
        for chat in chats:
            states[access(chat, 0).state] += 1
        await message.answer(
            f"💰 За 30 дней: {stars(month['stars'])} ({month['payments']} платежей)\n"
            f"Всего: {stars(total['stars'])} ({total['payments']} платежей)\n"
            f"Чатов: платят {states['paid']}, на пробе {states['trial']}, закончилось {states['expired']}"
        )

    # ------------------------------------------------------------------ notices

    async def send_billing_notices(self, now: int | None = None) -> None:
        """Once a day: warn owners 3 and 1 days before the end and tell them when it has ended.
        Each message is sent once (the chat remembers the last one), also across restarts."""
        now = now or int(time.time())
        for chat in await self.storage.all_chats():
            if not chat["owner_id"] or not chat["enabled"]:
                continue
            try:
                acc = await self.access_for(chat)
                key = None
                if acc.state == "expired":
                    key = "expired"
                elif acc.days_left in REMIND_DAYS:
                    key = f"{acc.state}:{acc.days_left}"
                if not key or key == chat["billing_notice"]:
                    continue
                chat_id = chat["chat_id"]
                title = html.escape(await self.chat_title(chat_id))
                text = f"⏰ Чат «{title}»: {self.access_text(acc)}"
                if acc.state == "trial":
                    since = now - PERIOD_DAYS * DAY
                    stats = await self.storage.stats(chat_id, since)
                    checked = sum(v for k, v in stats.items() if k != "tokens")
                    deleted = sum(stats.get(a.value, 0) for a in DELETING_ACTIONS)
                    if checked:
                        text += f"\nЗа это время я проверил {number(checked)} комментариев и убрал {number(deleted)}."
                await self.dm(chat["owner_id"], text, self.pay_markup(chat_id))
                await self.storage.set_billing_notice(chat_id, key)
            except Exception:  # one broken chat must not stop the others
                log.exception("Billing notice failed for chat %s", chat["chat_id"])
