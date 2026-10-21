"""Selling the service in Telegram: trial and plan status, payment in Telegram Stars, notices before the end.

The owner of a chat pays for that chat. A payment extends the chat's `paid_until`; when it runs out the bot keeps
everything that works in code (link lists, antiflood, raid and captcha, local spam filter) and only the AI check stops.
"""

import html
import logging
import os
import re
import secrets
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

from adapters.telegram.base import AdapterBase
from adapters.telegram.common import (
    REFERRAL_DAYS,
    callback_data,
    user_of,
)
from core.billing import DAY, PERIOD_DAYS, PLANS, REFUND_DAYS, TERMS, TRIAL_DAYS, Access, access, price, price_rub
from core.metrics import PAYMENT_AMOUNT, PAYMENTS
from core.models import DELETING_ACTIONS
from core.moderation import MOSCOW
from core.tbank import EMAIL, TBank, TBankError, kopecks

log = logging.getLogger(__name__)

USAGE_TTL = 120  # seconds the cached count of AI checks may be old
EXPIRED_ALERT_COOLDOWN = 3 * DAY
LIMIT_ALERT_COOLDOWN = DAY
EMAIL_WAIT = 15 * 60  # seconds a customer has to send the e-mail for the receipt
REMIND_DAYS = (3, 1)  # warn this many days before the end

PAYSUPPORT_TEXT = (
    "💬 <b>Вопросы по оплате</b>\n\n"
    "Платёж прошёл, а доступ не появился? Или сервис не подошёл и нужен возврат?\n"
    "Напишите {contact} и пришлите номер платежа из чека Telegram.\n"
    "Если сервис не подошёл, вернём деньги в течение {days} дней после оплаты (условия в оферте: {site}/offer)."
)


def stars(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ⭐"


def number(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def money(r: dict) -> str:
    parts = [f"{r['rub']:,} ₽".replace(",", " ")] if r.get("rub") else []
    if r.get("stars") or not parts:
        parts.append(stars(r.get("stars", 0)))
    return " и ".join(parts)


def date_text(ts: int) -> str:
    return datetime.fromtimestamp(ts, MOSCOW).strftime("%d.%m.%Y")


def days_text(n: int) -> str:
    last = n % 10 if n % 100 not in range(11, 15) else 0
    return f"{n} " + ("день" if last == 1 else "дня" if last in (2, 3, 4) else "дней")


def link_button(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


class PaymentsMixin(AdapterBase):
    """Selling the service: plan status, payments, notices. Part of the adapter."""

    def _init_payments(self) -> None:
        self.bot_username: str | None = None  # set at startup; deep links to the payment screen need it
        self._usage: dict[int, tuple[float, int]] = {}
        ids = os.getenv("OPERATOR_IDS") or os.getenv("ALERT_CHAT_ID") or ""
        self.operators = {int(x) for x in re.split(r"[,\s]+", ids) if x.strip().lstrip("-").isdigit()}
        self.support_contact = os.getenv("SUPPORT_CONTACT", "").strip() or "владельцу сервиса"
        self.tbank = TBank.from_env()  # None until the terminal keys are in .env: then only Stars are offered
        self._awaiting_email: dict[int, tuple[int, str, int, float]] = {}

    def _register_payments(self, r) -> None:
        private = F.chat.type == ChatType.PRIVATE
        r.message(Command("plan", "buy"))(self.on_plan)
        r.message(Command("paysupport"))(self.on_paysupport)
        r.message(Command("grant"), private)(self.on_grant)
        r.message(Command("refund"), private)(self.on_refund)
        r.message(Command("revenue"), private)(self.on_revenue)
        r.callback_query(F.data.startswith("plan:"))(self.on_plan_button)
        r.callback_query(F.data.startswith("buy:"))(self.on_buy_button)
        r.callback_query(F.data.startswith("pm:"))(self.on_method_button)
        r.callback_query(F.data.startswith("rub:"))(self.on_rub_button)
        r.message(Command("email"), private)(self.on_email_command)
        r.message(private, F.text, self.waiting_for_email)(self.on_email_message)
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
        chats = await self.storage.chats_of_owner(user_of(message).id)
        if not chats:
            await message.answer(
                "У вас пока нет чатов с ботом. Добавьте меня в группу обсуждений канала, сделайте админом "
                "и напишите там /rules. Первые " + days_text(TRIAL_DAYS) + " бесплатно."
            )
            return
        if len(chats) == 1:
            await self.show_plans(user_of(message).id, chats[0]["chat_id"])
            return
        rows = []
        for chat in chats:
            acc = await self.access_for(chat)
            title = await self.chat_title(chat["chat_id"])
            mark = {"paid": "✅", "trial": "🕐", "expired": "⏸"}[acc.state]
            rows.append([InlineKeyboardButton(text=f"{mark} {title}"[:60], callback_data=f"plan:{chat['chat_id']}")])
        await message.answer("Для какого чата тариф?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_plan_button(self, call: CallbackQuery) -> None:
        chat_id = int(callback_data(call).split(":")[1])
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
        if not chat or not await self.is_admin(chat_id, user_of(message).id, fresh=True):
            await message.answer("Оплатить тариф может только админ этого чата. Откройте ссылку из чата, где вы админ.")
            return True
        await self.show_plans(user_of(message).id, chat_id)
        return True

    async def show_plans(self, user_id: int, chat_id: int, method: str | None = None) -> bool:
        """The plans and, when roubles are connected, first the choice between card and Stars."""
        chat = await self.storage.get_chat(chat_id)
        if not chat:
            return False
        acc = await self.access_for(chat)
        title = html.escape(await self.chat_title(chat_id))
        lines = [f"💳 <b>Тариф для чата «{title}»</b>", self.access_text(acc), ""]
        for plan in PLANS.values():
            cost = f"{plan.rub:,} ₽ или {stars(plan.stars)}".replace(",", " ") if self.tbank else stars(plan.stars)
            lines.append(
                f"<b>{plan.title}</b>: {cost} в месяц. До {number(plan.checks)} комментариев в месяц "
                f"с ИИ-разбором (около {number(plan.per_day)} в день): {plan.pitch}."
            )
        lines += ["", "Месяц — 30 дней. За 3 месяца скидка 10%, за год 25%. Правила, настройки и журнал сохраняются при любом сроке."]
        if acc.state == "paid":
            lines.append("Продление того же тарифа добавляет время к оставшемуся. Смена тарифа начинает новый срок с сегодняшнего дня.")
        if self.tbank and method is None:
            lines.append("\nКак оплатить?")
            rows = [
                [InlineKeyboardButton(text="💳 Картой или СБП, в рублях", callback_data=f"pm:{chat_id}:rub")],
                [InlineKeyboardButton(text="⭐ Звёздами Telegram", callback_data=f"pm:{chat_id}:stars")],
            ]
        else:
            method = method or "stars"
            lines.append("\n" + ("Оплата картой или через СБП. Чек придёт на почту." if method == "rub" else "Оплата звёздами Telegram."))
            rows = []
            for plan in PLANS.values():
                for months in TERMS:
                    label = {1: "1 месяц", 3: "3 месяца", 12: "12 месяцев"}[months]
                    cost = f"{price_rub(plan, months):,} ₽".replace(",", " ") if method == "rub" else stars(price(plan, months))
                    rows.append([InlineKeyboardButton(
                        text=f"{plan.title} · {label} — {cost}",
                        callback_data=f"{'rub' if method == 'rub' else 'buy'}:{chat_id}:{plan.key}:{months}",
                    )])
        return await self.dm(user_id, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    async def on_method_button(self, call: CallbackQuery) -> None:
        _, raw_chat, method = callback_data(call).split(":")
        chat_id = int(raw_chat)
        if method not in ("rub", "stars") or not await self.is_admin(chat_id, call.from_user.id, fresh=True):
            await call.answer("Оплатить может только админ этого чата.", show_alert=True)
            return
        await self.show_plans(call.from_user.id, chat_id, method)
        await call.answer()

    async def on_paysupport(self, message: Message) -> None:
        await message.answer(PAYSUPPORT_TEXT.format(
            contact=html.escape(self.support_contact), days=REFUND_DAYS, site=os.getenv("WEBAPP_URL", "").rstrip("/")
        ))

    # ----------------------------------------------------------------- payment

    @staticmethod
    def parse_payload(payload: str) -> tuple[int, str, int] | None:
        try:
            raw_chat, plan, raw_months = payload.split(":")
            chat_id, months = int(raw_chat), int(raw_months)
        except (ValueError, AttributeError):
            return None
        return (chat_id, plan, months) if plan in PLANS and months in TERMS else None

    async def invoice_fields(self, chat_id: int, plan_key: str, months: int) -> dict:
        """What Telegram needs for a Stars invoice: the same for the invoice in chat and the link of the panel."""
        plan = PLANS[plan_key]
        title = await self.chat_title(chat_id)
        return {
            "title": f"DefenceAi «{plan.title}», {months} мес."[:32],
            "description": f"Модерация чата «{title}»: {months * PERIOD_DAYS} дней, до {number(plan.checks)} ИИ-проверок в месяц."[:255],
            "payload": f"{chat_id}:{plan_key}:{months}",
            "currency": "XTR",
            "prices": [LabeledPrice(label=f"{plan.title}, {months} мес.", amount=price(plan, months))],
        }

    async def create_stars_link(self, chat_id: int, plan_key: str, months: int) -> str:
        """A link for the panel's `openInvoice`: the payment window opens inside the Mini App."""
        return await self.bot.create_invoice_link(**await self.invoice_fields(chat_id, plan_key, months))

    async def on_buy_button(self, call: CallbackQuery) -> None:
        parsed = self.parse_payload(callback_data(call).split(":", 1)[1])
        if not parsed:
            await call.answer("Некорректный выбор.", show_alert=True)
            return
        chat_id, plan_key, months = parsed
        if not await self.is_admin(chat_id, call.from_user.id, fresh=True):
            await call.answer("Оплатить может только админ этого чата.", show_alert=True)
            return
        try:
            await self.bot.send_invoice(chat_id=call.from_user.id, **await self.invoice_fields(chat_id, plan_key, months))
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

    async def complete_payment(
        self, charge_id: str, chat_id: int, payer_id: int, plan_key: str, months: int, amount: int, currency: str, email: str | None = None
    ) -> str | None:
        """Record a payment, open the access and tell the operators. Returns the thank-you text,
        or None when this charge was already processed (the bank or Telegram may send a notice twice)."""
        fresh = await self.storage.record_payment(charge_id, chat_id, payer_id, plan_key, months, amount, currency, email)
        if not fresh:
            return None
        PAYMENTS.inc(currency=currency)
        PAYMENT_AMOUNT.inc(amount, currency=currency)
        gift = await self.referral_reward(charge_id, payer_id, chat_id)
        self._usage.pop(chat_id, None)
        chat = await self.storage.get_chat(chat_id)
        acc = await self.access_for(chat)
        title = html.escape(await self.chat_title(chat_id))
        shown = f"{amount:,} ₽".replace(",", " ") if currency == "RUB" else stars(amount)
        for operator in self.operators:
            await self.dm(operator, f"💰 Оплата: {shown}, «{PLANS[plan_key].title}» на {months} мес., чат «{title}»")
        log.info("payment chat=%s plan=%s months=%s amount=%s %s", chat_id, plan_key, months, amount, currency)
        return (
            f"✅ Спасибо! Тариф «{PLANS[plan_key].title}» для чата «{title}» оплачен до {date_text(acc.until)}.\n"
            "Правила и настройки на месте, ИИ-проверка работает. Вопросы по оплате: /paysupport" + gift
        )

    async def on_paid(self, message: Message) -> None:
        pay = message.successful_payment
        if pay is None:
            return
        parsed = self.parse_payload(pay.invoice_payload)
        if not parsed:
            log.error("Paid an unknown payload %r (charge %s)", pay.invoice_payload, pay.telegram_payment_charge_id)
            await message.answer("Платёж получен, но я не смог его привязать к чату. Напишите /paysupport, всё исправим.")
            return
        chat_id, plan_key, months = parsed
        thanks = await self.complete_payment(
            pay.telegram_payment_charge_id, chat_id, user_of(message).id, plan_key, months, pay.total_amount, "XTR"
        )
        if thanks:
            await message.answer(thanks)

    # ------------------------------------------------------- roubles (T-Bank)

    async def waiting_for_email(self, message: Message) -> bool:
        entry = self._awaiting_email.get(user_of(message).id) if message.from_user else None
        if not entry or time.time() - entry[3] > EMAIL_WAIT:
            return False
        return not (message.text or "").startswith("/")

    async def on_rub_button(self, call: CallbackQuery) -> None:
        parsed = self.parse_payload(callback_data(call).split(":", 1)[1])
        if not parsed or not self.tbank:
            await call.answer("Оплата картой пока недоступна. Выберите звёзды или напишите /paysupport.", show_alert=True)
            return
        chat_id, plan_key, months = parsed
        user_id = call.from_user.id
        if not await self.is_admin(chat_id, user_id, fresh=True):
            await call.answer("Оплатить может только админ этого чата.", show_alert=True)
            return
        await call.answer()
        email = await self.storage.get_email(user_id)
        if email:
            await self.start_rub_payment(user_id, chat_id, plan_key, months, email)
            return
        self._awaiting_email[user_id] = (chat_id, plan_key, months, time.time())
        await self.dm(
            user_id,
            "Для оплаты нужен e-mail: на него придёт кассовый чек (так требует закон). Пришлите адрес одним сообщением. "
            "Он нужен только для чека.",
        )

    async def on_email_message(self, message: Message) -> None:
        user_id = user_of(message).id
        email = (message.text or "").strip()
        if not EMAIL.match(email):
            await message.answer("Не похоже на e-mail. Пришлите адрес ещё раз, например name@mail.ru")
            return
        chat_id, plan_key, months, _ = self._awaiting_email.pop(user_id)
        await self.storage.set_email(user_id, email)
        await self.start_rub_payment(user_id, chat_id, plan_key, months, email)

    async def on_email_command(self, message: Message, command: CommandObject) -> None:
        """/email shows the e-mail used for receipts, /email name@mail.ru changes it, /email delete forgets it."""
        user_id = user_of(message).id
        arg = (command.args or "").strip()
        if arg in ("delete", "удалить"):
            await self.storage.set_email(user_id, None)
            await message.answer("Адрес для чеков удалён.")
        elif arg:
            if not EMAIL.match(arg):
                await message.answer("Не похоже на e-mail. Пример: <code>/email name@mail.ru</code>")
                return
            await self.storage.set_email(user_id, arg)
            await message.answer(f"Чеки будут приходить на {html.escape(arg)}.")
        else:
            current = await self.storage.get_email(user_id)
            await message.answer(
                f"Чеки приходят на {html.escape(current)}. Сменить: <code>/email name@mail.ru</code>, забыть: <code>/email delete</code>"
                if current
                else "Адрес для чеков пока не задан: бот спросит его при первой оплате. Задать сразу: <code>/email name@mail.ru</code>"
            )

    async def make_rub_payment(self, user_id: int, chat_id: int, plan_key: str, months: int, email: str) -> str:
        """Create the order and the bank's payment page. Returns the link; TBankError when the bank refuses."""
        plan = PLANS[plan_key]
        amount = price_rub(plan, months)
        order_id = secrets.token_hex(8)
        await self.storage.create_order(order_id, chat_id, user_id, plan_key, months, amount, email)
        site = os.getenv("WEBAPP_URL", "").rstrip("/")
        if self.tbank is None:
            raise TBankError("терминал Т-Банка не подключён")
        try:
            payment_id, url = await self.tbank.create_payment(
                order_id,
                kopecks(amount),
                f"DefenceAi «{plan.title}», {months} мес.",
                email,
                notify_url=f"{site}/tbank/notify" if site else None,
                success_url=f"https://t.me/{self.bot_username}" if self.bot_username else None,
            )
        except TBankError as e:
            log.error("T-Bank refused to create a payment: %s", e)
            await self.storage.set_order(order_id, "failed")
            raise
        await self.storage.set_order(order_id, "sent", payment_id)
        return url

    async def start_rub_payment(self, user_id: int, chat_id: int, plan_key: str, months: int, email: str) -> None:
        """The same in the bot's chat: the link comes as a message with a button."""
        plan, amount = PLANS[plan_key], price_rub(PLANS[plan_key], months)
        try:
            url = await self.make_rub_payment(user_id, chat_id, plan_key, months, email)
        except TBankError as e:
            await self.dm(user_id, f"Не получилось создать счёт: {html.escape(str(e))}. Попробуйте звёзды или напишите /paysupport.")
            return
        await self.dm(
            user_id,
            f"К оплате: <b>{amount:,} ₽</b> за тариф «{plan.title}», {months} мес.".replace(",", " ")
            + f"\nОплатить можно картой или через СБП. Чек придёт на {html.escape(email)} (сменить: /email)."
            "\nПосле оплаты срок продлится сам, ничего нажимать не нужно.",
            link_button(f"Оплатить {amount:,} ₽".replace(",", " "), url),
        )

    async def handle_tbank_notification(self, data: dict) -> bool:
        """The bank tells us about a payment. False only when the signature is wrong (answered with an error,
        so the bank does not treat it as delivered); everything else is answered OK, also what we ignore."""
        if not self.tbank or not self.tbank.verify(data):
            log.warning("T-Bank notification with a bad signature for order %r", data.get("OrderId"))
            return False
        order = await self.storage.get_order(str(data.get("OrderId")))
        if not order:
            log.warning("T-Bank notification for an unknown order %r", data.get("OrderId"))
            return True
        status, payment_id = data.get("Status"), str(data.get("PaymentId"))
        charge_id = f"tbank:{payment_id}"
        if status == "CONFIRMED" and data.get("Success") in (True, "true"):
            if int(data.get("Amount") or 0) != kopecks(order["amount"]):
                log.error("T-Bank paid %s kopecks for order %s that costs %s roubles", data.get("Amount"), order["order_id"], order["amount"])
                return True
            thanks = await self.complete_payment(
                charge_id, order["chat_id"], order["payer_id"], order["plan"], order["months"], order["amount"], "RUB", order["email"]
            )
            await self.storage.set_order(order["order_id"], "paid", payment_id)
            if thanks:
                await self.dm(order["payer_id"], thanks)
        elif status in ("REFUNDED", "PARTIAL_REFUNDED", "REVERSED"):
            await self.storage.mark_refunded(charge_id)
            await self.storage.set_order(order["order_id"], status.lower(), payment_id)
        elif status in ("REJECTED", "CANCELED", "DEADLINE_EXPIRED", "AUTH_FAIL"):
            await self.storage.set_order(order["order_id"], status.lower(), payment_id)
        return True

    # ------------------------------------------------------------ invitations

    async def referral_reward(self, charge_id: str, payer_id: int, chat_id: int) -> str:
        """The first payment of an invited person: +days to their chat and to the inviter's. Returns a line for the
        thank-you text, or "" when this payment earns nothing."""
        invite = await self.storage.referral_of(payer_id)
        if not invite or invite["rewarded_at"] or await self.storage.payments_of(payer_id) != 1:
            return ""
        referrer = invite["referrer_id"]
        chats = await self.storage.chats_of_user(referrer)
        referrer_chat = chats[0]["chat_id"] if chats else None
        if not await self.storage.reward_referral(payer_id, charge_id, chat_id, referrer_chat, REFERRAL_DAYS):
            return ""
        await self.storage.add_bonus_days(chat_id, REFERRAL_DAYS)
        gift = f"+{days_text(REFERRAL_DAYS)}"
        if referrer_chat:
            await self.storage.add_bonus_days(referrer_chat, REFERRAL_DAYS)
            self._usage.pop(referrer_chat, None)
            await self.dm(referrer, f"🎁 Приглашённый вами человек оплатил первый месяц: {gift} к вашему чату «{html.escape(await self.chat_title(referrer_chat))}».")
        else:
            await self.dm(referrer, f"🎁 Приглашённый вами человек оплатил первый месяц. {gift} появятся, как только вы подключите бота к чату.")
        log.info("referral reward: inviter=%s invitee=%s chat=%s", referrer, payer_id, chat_id)
        return f"\n🎁 {gift} в подарок за то, что вас пригласили."

    async def referral_take_back(self, charge_id: str) -> None:
        """The payment that earned the gift was refunded: the days go back."""
        invite = await self.storage.referral_by_charge(charge_id)
        if not invite or not invite["days"]:
            return
        for chat in (invite["invitee_chat"], invite["referrer_chat"]):
            if chat:
                await self.storage.add_bonus_days(chat, -invite["days"])
                self._usage.pop(chat, None)
        await self.storage.take_back_referral(invite["invitee_id"])

    # ---------------------------------------------------------- the operator's tools

    def is_operator(self, message: Message) -> bool:
        return bool(message.from_user and user_of(message).id in self.operators)

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
            if payment["currency"] == "RUB":
                if not self.tbank:
                    raise TBankError("терминал Т-Банка не подключён")
                await self.tbank.cancel(
                    charge_id.removeprefix("tbank:"), kopecks(payment["stars"]),
                    f"DefenceAi «{PLANS[payment['plan']].title}», {payment['months']} мес.", payment["email"],
                )
                given = f"{payment['stars']:,} ₽".replace(",", " ")
            else:
                await self.bot.refund_star_payment(user_id=payment["payer_id"], telegram_payment_charge_id=charge_id)
                given = stars(payment["stars"])
        except (TelegramAPIError, TBankError) as e:
            await message.answer(f"Не получилось вернуть деньги: {html.escape(str(e))}")
            return
        await self.storage.mark_refunded(charge_id)
        await self.referral_take_back(charge_id)
        self._usage.pop(payment["chat_id"], None)
        await message.answer(f"Возвращено {given}, срок уменьшен на {payment['months']} мес.")

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
            f"💰 За 30 дней: {money(month)} ({month['payments']} платежей)\n"
            f"Всего: {money(total)} ({total['payments']} платежей)\n"
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
                elif acc.days_left in REMIND_DAYS and not (acc.state == "trial" and acc.days_left == 1):
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
