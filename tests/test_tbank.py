"""Roubles through T-Bank: signatures, the payment link, notifications, refunds, receipts."""

import hashlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject

from core.billing import DAY, PLANS, price_rub
from core.tbank import EMAIL, TBank, TBankError, kopecks, sign
from tests.test_handlers import CHAT, OWNER, env, msg, sent_to  # noqa: F401

ADMIN = 1
PW = "secret-pw"


# ------------------------------------------------------------------- signature


def test_signature_follows_the_documented_recipe():
    params = {"TerminalKey": "TK", "Amount": 100000, "OrderId": "21050", "Description": "Подарочная карта"}
    expected = hashlib.sha256("100000Подарочная карта21050secret-pwTK".encode()).hexdigest()
    assert sign(params, PW) == expected  # values in the order of the names: Amount, Description, OrderId, Password, TerminalKey


def test_signature_ignores_token_and_nested_objects_and_writes_booleans_like_the_bank():
    base = {"TerminalKey": "TK", "Success": True, "Amount": 5}
    with_extras = {**base, "Token": "zzz", "Receipt": {"Email": "a@b.ru"}, "DATA": {"x": 1}}
    assert sign(base, PW) == sign(with_extras, PW)
    assert sign({"A": True}, PW) == hashlib.sha256(b"truesecret-pw").hexdigest()


def test_notifications_are_verified():
    bank = TBank("TK", PW)
    note = {"TerminalKey": "TK", "OrderId": "o1", "Success": True, "Status": "CONFIRMED", "PaymentId": 77, "Amount": 79000}
    note["Token"] = sign(note, PW)
    assert bank.verify(note)
    assert not bank.verify({**note, "Amount": 1})  # changed after signing
    assert not bank.verify({**note, "TerminalKey": "other"})
    assert not bank.verify({k: v for k, v in note.items() if k != "Token"})
    assert not TBank("TK", "wrong").verify(note)


def test_init_payload_is_signed_and_carries_the_receipt():
    bank = TBank("TK", PW, "usn_income")
    p = bank.init_payload("o1", 79000, "DefenceAi «Старт», 1 мес.", "a@b.ru", "https://s/tbank/notify", "https://t.me/bot")
    assert p["Amount"] == 79000 and p["NotificationURL"] == "https://s/tbank/notify" and p["PayType"] == "O"
    item = p["Receipt"]["Items"][0]
    assert p["Receipt"]["Taxation"] == "usn_income" and p["Receipt"]["Email"] == "a@b.ru"
    assert (item["Price"], item["Amount"], item["Quantity"]) == (79000, 79000, 1.0)
    assert item["Tax"] == "none" and item["PaymentObject"] == "service" and item["PaymentMethod"] == "full_payment"
    assert p["Token"] == sign(p, PW) and bank.verify({**{k: v for k, v in p.items() if k != "Receipt"}})
    assert "Receipt" not in TBank("TK", PW, use_receipt=False).init_payload("o", 1, "d", "a@b.ru", None, None)


def test_the_terminal_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("TBANK_TERMINAL_KEY", raising=False)
    assert TBank.from_env() is None
    monkeypatch.setenv("TBANK_TERMINAL_KEY", "TK")
    monkeypatch.setenv("TBANK_PASSWORD", PW)
    monkeypatch.setenv("TBANK_RECEIPT", "0")
    bank = TBank.from_env()
    assert bank.terminal_key == "TK" and not bank.use_receipt


@pytest.mark.parametrize("good", ["a@b.ru", "name.surname@mail.co", "x+tag@domain.com"])
def test_emails_accepted(good):
    assert EMAIL.match(good)


@pytest.mark.parametrize("bad", ["", "no-at.ru", "a@b", "a b@c.ru", "@c.ru", "a@@c.ru"])
def test_emails_rejected(bad):
    assert not EMAIL.match(bad)


async def test_create_payment_reads_the_link_and_reports_refusals():
    bank = TBank("TK", PW)
    bank._post = AsyncMock(return_value={"Success": True, "PaymentId": 4242, "PaymentURL": "https://pay/x"})
    assert await bank.create_payment("o1", 100, "d", "a@b.ru") == ("4242", "https://pay/x")
    sent = bank._post.await_args.args
    assert sent[0] == "Init" and sent[1]["Token"]


# -------------------------------------------------------------------- the bot


def callback(data, user=ADMIN):
    c = MagicMock(data=data, from_user=MagicMock(id=user))
    c.answer = AsyncMock()
    return c


async def rub_env(env):
    adapter, jev = env.build()
    adapter.tbank = TBank("TK", PW)
    adapter.tbank._post = AsyncMock(return_value={"Success": True, "PaymentId": 4242, "PaymentURL": "https://pay/x"})
    adapter.bot_username = "DefenceAiBot"
    adapter.operators = {777}
    return adapter


def notification(order_id, status="CONFIRMED", amount=None, payment_id=4242, **kw):
    note = {"TerminalKey": "TK", "OrderId": order_id, "Success": True, "Status": status, "PaymentId": payment_id,
            "Amount": amount if amount is not None else kopecks(price_rub(PLANS["start"], 1))}
    note.update(kw)
    note["Token"] = sign(note, PW)
    return note


async def last_order(storage):
    async with storage.db.execute("SELECT * FROM orders ORDER BY ts DESC, rowid DESC LIMIT 1") as cur:
        return await cur.fetchone()


async def test_without_a_terminal_only_stars_are_offered(env):
    adapter, _ = env.build()
    await adapter.show_plans(ADMIN, CHAT)
    shown = sent_to(env.bot, ADMIN)[-1]
    assert all(b.callback_data.startswith("buy:") for row in shown.kwargs["reply_markup"].inline_keyboard for b in row)
    assert "₽" not in shown.args[1]


async def test_with_a_terminal_the_customer_chooses_how_to_pay(env):
    adapter = await rub_env(env)
    await adapter.show_plans(ADMIN, CHAT)
    shown = sent_to(env.bot, ADMIN)[-1]
    assert [b.callback_data for row in shown.kwargs["reply_markup"].inline_keyboard for b in row] == [f"pm:{CHAT}:rub", f"pm:{CHAT}:stars"]
    assert "790 ₽" in shown.args[1]
    call = callback(f"pm:{CHAT}:rub")
    await adapter.on_method_button(call)
    buttons = [b for row in sent_to(env.bot, ADMIN)[-1].kwargs["reply_markup"].inline_keyboard for b in row]
    assert buttons[0].callback_data == f"rub:{CHAT}:start:1" and "790 ₽" in buttons[0].text
    assert "₽" in buttons[-1].text and buttons[-1].callback_data == f"rub:{CHAT}:pro:12"


async def test_first_payment_asks_for_an_email_then_sends_the_link(env):
    adapter = await rub_env(env)
    await adapter.on_rub_button(callback(f"rub:{CHAT}:start:1"))
    assert "e-mail" in sent_to(env.bot, ADMIN)[-1].args[1]
    typed = msg("привет", user_id=ADMIN)
    typed.answer = AsyncMock()
    assert await adapter.waiting_for_email(typed)
    typed.text = "not an email"
    await adapter.on_email_message(typed)
    assert "Не похоже" in typed.answer.await_args.args[0] and not adapter.tbank._post.await_count
    typed.text = "owner@mail.ru"
    await adapter.on_email_message(typed)
    init = adapter.tbank._post.await_args.args[1]
    assert init["Amount"] == 79000 and init["Receipt"]["Email"] == "owner@mail.ru"
    link = sent_to(env.bot, ADMIN)[-1]
    assert link.kwargs["reply_markup"].inline_keyboard[0][0].url == "https://pay/x" and "790" in link.args[1]
    order = await last_order(env.storage)
    assert (order["chat_id"], order["plan"], order["months"], order["amount"], order["status"]) == (CHAT, "start", 1, 790, "sent")
    assert await env.storage.get_email(ADMIN) == "owner@mail.ru"
    assert not await adapter.waiting_for_email(typed)


async def test_second_payment_uses_the_saved_email_and_commands_are_not_taken_for_one(env):
    adapter = await rub_env(env)
    await env.storage.set_email(ADMIN, "owner@mail.ru")
    await adapter.on_rub_button(callback(f"rub:{CHAT}:pro:3"))
    assert adapter.tbank._post.await_args.args[1]["Amount"] == kopecks(price_rub(PLANS["pro"], 3))
    adapter._awaiting_email[ADMIN] = (CHAT, "start", 1, time.time())
    assert not await adapter.waiting_for_email(msg("/plan", user_id=ADMIN))
    adapter._awaiting_email[ADMIN] = (CHAT, "start", 1, time.time() - 3600)
    assert not await adapter.waiting_for_email(msg("owner@mail.ru", user_id=ADMIN)), "asking expires"


async def test_only_admins_can_pay_for_a_chat_and_a_bank_error_is_explained(env):
    adapter = await rub_env(env)
    stranger = callback(f"rub:{CHAT}:start:1", user=999)
    await adapter.on_rub_button(stranger)
    assert stranger.answer.await_args.kwargs.get("show_alert") and not adapter.tbank._post.await_count
    await env.storage.set_email(ADMIN, "owner@mail.ru")
    adapter.tbank._post = AsyncMock(side_effect=TBankError("терминал не найден"))
    await adapter.on_rub_button(callback(f"rub:{CHAT}:start:1"))
    assert "Не получилось создать счёт" in sent_to(env.bot, ADMIN)[-1].args[1]


async def paid_order(env, adapter):
    await env.storage.set_email(ADMIN, "owner@mail.ru")
    await adapter.on_rub_button(callback(f"rub:{CHAT}:start:1"))
    return await last_order(env.storage)


async def test_a_signed_confirmation_opens_the_access_once(env):
    adapter = await rub_env(env)
    order = await paid_order(env, adapter)
    note = notification(order["order_id"])
    assert await adapter.handle_tbank_notification(note)
    chat = await env.storage.get_chat(CHAT)
    assert chat["plan"] == "start" and chat["paid_until"] > time.time() + 29 * DAY
    assert "оплачен" in sent_to(env.bot, ADMIN)[-1].args[1]
    assert any("790 ₽" in c.args[1] for c in sent_to(env.bot, 777))
    assert (await env.storage.get_order(order["order_id"]))["status"] == "paid"
    before = chat["paid_until"]
    assert await adapter.handle_tbank_notification(note)  # the bank repeats notifications
    assert (await env.storage.get_chat(CHAT))["paid_until"] == before
    assert (await env.storage.revenue())["rub"] == 790


async def test_forged_wrong_amount_and_unknown_notifications_give_nothing(env):
    adapter = await rub_env(env)
    order = await paid_order(env, adapter)
    forged = notification(order["order_id"])
    forged["Token"] = "0" * 64
    assert not await adapter.handle_tbank_notification(forged)
    assert await adapter.handle_tbank_notification(notification(order["order_id"], amount=100))  # paid 1 rouble
    assert await adapter.handle_tbank_notification(notification("no-such-order"))
    unsigned_for_other_terminal = notification(order["order_id"], TerminalKey="other")
    assert not await adapter.handle_tbank_notification(unsigned_for_other_terminal)
    assert not (await env.storage.get_chat(CHAT))["paid_until"]


async def test_declined_and_refunded_notifications(env):
    adapter = await rub_env(env)
    order = await paid_order(env, adapter)
    assert await adapter.handle_tbank_notification(notification(order["order_id"], "REJECTED", Success=False))
    assert (await env.storage.get_order(order["order_id"]))["status"] == "rejected"
    assert not (await env.storage.get_chat(CHAT))["paid_until"]
    await adapter.handle_tbank_notification(notification(order["order_id"]))
    await adapter.handle_tbank_notification(notification(order["order_id"], "REFUNDED"))  # refunded in the bank's cabinet
    assert (await env.storage.get_payment("tbank:4242"))["refunded"] == 1
    assert (await env.storage.get_chat(CHAT))["paid_until"] <= time.time() + 5


async def test_the_operator_refunds_a_rouble_payment_through_the_bank(env):
    adapter = await rub_env(env)
    order = await paid_order(env, adapter)
    await adapter.handle_tbank_notification(notification(order["order_id"]))
    boss = msg("/refund", user_id=777)
    boss.answer = AsyncMock()
    await adapter.on_refund(boss, CommandObject(prefix="/", command="refund", args="tbank:4242"))
    cancel = adapter.tbank._post.await_args
    assert cancel.args[0] == "Cancel" and cancel.args[1]["PaymentId"] == "4242" and cancel.args[1]["Amount"] == 79000
    assert cancel.args[1]["Receipt"]["Email"] == "owner@mail.ru"
    assert "790 ₽" in boss.answer.await_args.args[0]
    assert (await env.storage.get_payment("tbank:4242"))["refunded"] == 1


async def test_a_refused_refund_changes_nothing(env):
    adapter = await rub_env(env)
    order = await paid_order(env, adapter)
    await adapter.handle_tbank_notification(notification(order["order_id"]))
    adapter.tbank._post = AsyncMock(side_effect=TBankError("нельзя"))
    boss = msg("/refund", user_id=777)
    boss.answer = AsyncMock()
    await adapter.on_refund(boss, CommandObject(prefix="/", command="refund", args="tbank:4242"))
    assert "Не получилось" in boss.answer.await_args.args[0]
    assert (await env.storage.get_payment("tbank:4242"))["refunded"] == 0


async def test_revenue_counts_roubles_and_stars_apart(env):
    adapter = await rub_env(env)
    await env.storage.record_payment("tbank:1", CHAT, ADMIN, "start", 1, 790, "RUB", "a@b.ru")
    await env.storage.record_payment("tg1", CHAT, ADMIN, "start", 1, 399)
    boss = msg("/revenue", user_id=777)
    boss.answer = AsyncMock()
    await adapter.on_revenue(boss)
    text = boss.answer.await_args.args[0]
    assert "790 ₽" in text and "399 ⭐" in text and "2 платежей" in text


async def test_email_command(env):
    adapter = await rub_env(env)
    m = msg("/email", user_id=ADMIN)
    m.answer = AsyncMock()
    await adapter.on_email_command(m, CommandObject(prefix="/", command="email", args="x@y.ru"))
    assert await env.storage.get_email(ADMIN) == "x@y.ru"
    await adapter.on_email_command(m, CommandObject(prefix="/", command="email", args="junk"))
    assert await env.storage.get_email(ADMIN) == "x@y.ru"
    await adapter.on_email_command(m, CommandObject(prefix="/", command="email", args="delete"))
    assert await env.storage.get_email(ADMIN) is None


# ------------------------------------------------------------------ certificates


def make_ca(tmp_path):
    import subprocess

    pem = tmp_path / "ca.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "k.pem"), "-out", str(pem),
         "-days", "2", "-subj", "/CN=Test Root CA", "-addext", "basicConstraints=critical,CA:TRUE"],
        check=True, capture_output=True,
    )
    return pem


def test_the_extra_root_is_added_and_verification_stays_on(tmp_path, monkeypatch):
    import ssl

    from core import tbank

    tbank._context.cache_clear()
    monkeypatch.setenv("TBANK_CA_FILE", str(tmp_path / "missing.pem"))
    plain = tbank.ssl_context()
    assert plain.verify_mode == ssl.CERT_REQUIRED and plain.check_hostname
    pem = make_ca(tmp_path)
    monkeypatch.setenv("TBANK_CA_FILE", str(pem))
    extended = tbank.ssl_context()
    assert extended.verify_mode == ssl.CERT_REQUIRED and extended.check_hostname
    assert any(c["subject"][0][0][1] == "Test Root CA" for c in extended.get_ca_certs())
    assert not any(c["subject"][0][0][1] == "Test Root CA" for c in plain.get_ca_certs())


def test_a_broken_ca_file_does_not_break_payments(tmp_path, monkeypatch):
    import ssl

    from core import tbank

    tbank._context.cache_clear()
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    monkeypatch.setenv("TBANK_CA_FILE", str(junk))
    context = tbank.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
