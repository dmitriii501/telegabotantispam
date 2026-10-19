"""Trial, plans, Telegram Stars payments and what happens when the time or the limit runs out."""

import time
from unittest.mock import AsyncMock, MagicMock

from aiogram.filters import CommandObject

from core.billing import DAY, PLANS, TERMS, TRIAL_CHECKS, TRIAL_DAYS, access, extend, price
from core.models import ActionType, Author, Comment
from core.moderation import ChatConfig, Moderator
from tests.test_handlers import CHAT, OWNER, SPAM, answer, msg, sent_to

NOW = 1_800_000_000
ADMIN = 1  # the admin of tests.test_handlers


def row(**kw):
    return {"plan": kw.get("plan", ""), "paid_until": kw.get("paid_until"), "trial_until": kw.get("trial_until", NOW + 5 * DAY)}


# ------------------------------------------------------------------ pure logic


def test_trial_paid_and_expired_states():
    assert access(row(), 0, NOW).state == "trial"
    assert access(row(trial_until=NOW - 1), 0, NOW).state == "expired"
    paid = access(row(plan="pro", paid_until=NOW + 10 * DAY, trial_until=NOW - 99), 0, NOW)
    assert (paid.state, paid.limit, paid.title) == ("paid", PLANS["pro"].checks, "Про")
    assert access(row(paid_until=NOW - 1, trial_until=NOW - 99), 0, NOW).state == "expired"


def test_a_chat_with_no_date_starts_its_trial_instead_of_being_locked():
    a = access({"plan": "", "paid_until": None, "trial_until": None}, 0, NOW)
    assert a.state == "trial" and a.days_left == TRIAL_DAYS


def test_limit_stops_the_ai_but_not_the_chat():
    a = access(row(), TRIAL_CHECKS, NOW)
    assert a.over_limit and not a.ai_allowed and a.state == "trial"
    assert access(row(), TRIAL_CHECKS - 1, NOW).ai_allowed
    assert not access(row(trial_until=NOW - 1), 0, NOW).ai_allowed


def test_days_left_round_up():
    assert access(row(trial_until=NOW + 1), 0, NOW).days_left == 1
    assert access(row(trial_until=NOW + 2 * DAY + 5), 0, NOW).days_left == 3


def test_prices_and_discounts():
    start = PLANS["start"]
    assert price(start, 1) == start.stars
    assert price(start, 12) < 12 * start.stars * 0.8
    assert all(price(PLANS["pro"], m) > price(start, m) for m in TERMS)


def test_renewing_adds_time_but_switching_plans_starts_today():
    current = row(plan="start", paid_until=NOW + 10 * DAY)
    assert extend(current, "start", 1, NOW) == NOW + 40 * DAY
    assert extend(current, "pro", 1, NOW) == NOW + 30 * DAY  # no turning cheap months into dear ones
    assert extend(row(plan="start", paid_until=NOW - DAY), "start", 1, NOW) == NOW + 30 * DAY


# ------------------------------------------------------------------ storage


async def test_new_chats_get_a_trial_and_old_ones_get_one_on_migration(env):
    chat = await env.storage.get_chat(CHAT)
    assert chat["trial_until"] > time.time() + (TRIAL_DAYS - 1) * DAY
    await env.storage.db.execute("UPDATE chats SET trial_until = NULL")
    await env.storage.db.commit()
    await env.storage.close()
    await env.storage.open()
    assert (await env.storage.get_chat(CHAT))["trial_until"] > time.time()


async def test_a_payment_is_counted_once(env):
    first = await env.storage.record_payment("ch1", CHAT, OWNER, "start", 1, 399)
    again = await env.storage.record_payment("ch1", CHAT, OWNER, "start", 1, 399)
    assert first and not again
    chat = await env.storage.get_chat(CHAT)
    assert chat["plan"] == "start" and chat["paid_until"] > time.time() + 29 * DAY
    assert (await env.storage.revenue())["stars"] == 399


async def test_refund_takes_the_months_back(env):
    await env.storage.record_payment("ch1", CHAT, OWNER, "start", 3, 1077)
    await env.storage.mark_refunded("ch1")
    await env.storage.mark_refunded("ch1")  # twice changes nothing
    chat = await env.storage.get_chat(CHAT)
    assert chat["paid_until"] <= time.time() + 5
    assert (await env.storage.revenue())["stars"] == 0


async def test_usage_is_counted_per_day_and_outlives_the_log(env):
    await env.storage.add_usage(CHAT)
    await env.storage.add_usage(CHAT, 2)
    assert await env.storage.ai_checks(CHAT, 0) == 3
    old = int(time.time()) - 40 * DAY
    await env.storage.db.execute("INSERT INTO usage (chat_id, day, checks) VALUES (?, ?, 50)", (CHAT, old // DAY * DAY))
    await env.storage.db.commit()
    assert await env.storage.ai_checks(CHAT, int(time.time()) - 30 * DAY) == 3, "only the last 30 days count"
    await env.storage.purge_old(1)  # the log is wiped after a day, the usage of the month stays
    assert await env.storage.ai_checks(CHAT, int(time.time()) - 30 * DAY) == 3
    assert await env.storage.ai_checks(CHAT + 1, 0) == 0


async def test_an_old_database_gets_its_usage_from_the_log(env):
    for tokens in (0, 900, 1200):
        await env.storage.add_log(chat_id=CHAT, message_id=1, user_id=5, text="x", action="none", tokens=tokens)
    await env.storage.close()
    await env.storage.open()
    assert await env.storage.ai_checks(CHAT, 0) == 2, "only comments with token usage were read by the AI"
    await env.storage.close()
    await env.storage.open()
    assert await env.storage.ai_checks(CHAT, 0) == 2, "seeded once, not on every start"


async def test_every_ai_read_comment_is_counted_in_the_usage_table(env):
    adapter, _ = env.build(answer())
    await adapter.handle_comment(msg("Расскажите про курс подробнее"))
    assert await env.storage.ai_checks(CHAT, 0) == 1


# ------------------------------------------------------------- moderator


class CountingJev:
    def __init__(self):
        self.calls = 0

    vision = False

    async def ask(self, *a):
        self.calls += 1
        return SPAM


def comment(text, previous=10):
    return Comment(1, 1, text, Author(1, "Вася", previous_messages=previous))


async def test_without_ai_the_jev_is_not_asked_but_code_protection_stays():
    jev = CountingJev()
    mod = Moderator(jev)
    quiet = await mod.check(comment("Подскажите, когда выйдет запись?"), ChatConfig(), ai=False)
    assert quiet.action == ActionType.NONE and quiet.extra.get("no_ai") and jev.calls == 0
    listed = await mod.check(comment("глянь casino.com"), ChatConfig(blocked_domains=["casino.com"]), ai=False)
    assert listed.action != ActionType.NONE, "the owner's link lists are code, they keep working"
    spam = await mod.check(comment("Заработок без вложений, пиши в лс t.me/x_bot"), ChatConfig(), ai=False)
    assert spam.action != ActionType.NONE and jev.calls == 0


# ----------------------------------------------------------------- the bot


async def expire(storage):
    await storage.db.execute("UPDATE chats SET trial_until = ?", (int(time.time()) - DAY,))
    await storage.db.commit()


async def test_an_expired_chat_is_protected_by_code_and_the_owner_is_told_once(env):
    adapter, jev = env.build(SPAM)
    adapter.bot_username = "DefenceAiBot"
    await env.storage.set_setting(CHAT, "observe", 0)
    await expire(env.storage)
    for i in range(3):
        await adapter.handle_comment(msg("Расскажите про курс подробнее", message_id=100 + i, user_id=10 + i))
    assert not jev.calls, "no AI once the time is over"
    notices = [c for c in sent_to(env.bot, OWNER) if "Срок закончился" in c.args[1]]
    assert len(notices) == 1
    assert "start=pay_" in notices[0].kwargs["reply_markup"].inline_keyboard[0][0].url


async def test_the_limit_stops_the_ai_and_warns(env):
    adapter, jev = env.build(answer())
    await env.storage.db.execute("UPDATE chats SET trial_until = ?", (int(time.time()) + 5 * DAY,))
    await env.storage.db.commit()
    adapter._usage[CHAT] = (time.time(), TRIAL_CHECKS)  # as if the month's checks were used up
    await adapter.handle_comment(msg("Расскажите про курс подробнее"))
    assert not jev.calls
    assert any("Лимит" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_each_ai_read_comment_counts_toward_the_limit(env):
    adapter, jev = env.build(answer())
    await adapter.access_for(await env.storage.get_chat(CHAT))
    await adapter.handle_comment(msg("Расскажите про курс подробнее"))
    assert adapter._usage[CHAT][1] == 1


async def test_a_paid_chat_works_again(env):
    adapter, jev = env.build(answer())
    await expire(env.storage)
    await env.storage.record_payment("c1", CHAT, OWNER, "start", 1, 399)
    adapter._usage.clear()
    await adapter.handle_comment(msg("Расскажите про курс подробнее"))
    assert jev.calls


# ------------------------------------------------------------- Stars payments


def paid_message(payload, amount, charge="ch-1", user=OWNER):
    m = MagicMock()
    m.from_user = MagicMock(id=user)
    m.answer = AsyncMock()
    m.successful_payment = MagicMock(invoice_payload=payload, total_amount=amount, telegram_payment_charge_id=charge, currency="XTR")
    return m


def pre_checkout(payload, amount, currency="XTR"):
    q = MagicMock(invoice_payload=payload, total_amount=amount, currency=currency)
    q.answer = AsyncMock()
    return q


async def test_pre_checkout_accepts_only_the_real_price(env):
    adapter, _ = env.build()
    good = f"{CHAT}:start:1"
    q = pre_checkout(good, price(PLANS["start"], 1))
    await adapter.on_pre_checkout(q)
    q.answer.assert_awaited_once_with(ok=True, error_message=None)
    for payload, amount, currency in [(good, 1, "XTR"), (good, price(PLANS["start"], 1), "USD"), ("junk", 5, "XTR"),
                                      (f"{CHAT}:gold:1", 5, "XTR"), (f"{CHAT}:start:7", 5, "XTR"), ("-5:start:1", price(PLANS["start"], 1), "XTR")]:
        bad = pre_checkout(payload, amount, currency)
        await adapter.on_pre_checkout(bad)
        assert bad.answer.await_args.kwargs["ok"] is False, (payload, amount, currency)


async def test_successful_payment_opens_access_and_tells_the_operator(env):
    adapter, _ = env.build()
    adapter.operators = {777}
    m = paid_message(f"{CHAT}:pro:3", price(PLANS["pro"], 3))
    await adapter.on_paid(m)
    chat = await env.storage.get_chat(CHAT)
    assert chat["plan"] == "pro" and chat["paid_until"] > time.time() + 89 * DAY
    assert "оплачен" in m.answer.await_args.args[0]
    assert any("Оплата" in c.args[1] for c in sent_to(env.bot, 777))
    before = chat["paid_until"]
    await adapter.on_paid(m)  # the same notice twice
    assert (await env.storage.get_chat(CHAT))["paid_until"] == before


async def test_unknown_payload_is_not_lost_silently(env):
    adapter, _ = env.build()
    m = paid_message("garbage", 10)
    await adapter.on_paid(m)
    assert "paysupport" in m.answer.await_args.args[0]


async def test_only_chat_admins_can_start_a_payment(env):
    adapter, _ = env.build()
    env.bot.send_invoice = AsyncMock()
    call = MagicMock(data=f"buy:{CHAT}:start:1", from_user=MagicMock(id=999))
    call.answer = AsyncMock()
    await adapter.on_buy_button(call)
    env.bot.send_invoice.assert_not_awaited()
    assert call.answer.await_args.kwargs.get("show_alert")
    call = MagicMock(data=f"buy:{CHAT}:start:1", from_user=MagicMock(id=ADMIN))
    call.answer = AsyncMock()
    await adapter.on_buy_button(call)
    kw = env.bot.send_invoice.await_args.kwargs
    assert kw["currency"] == "XTR" and kw["payload"] == f"{CHAT}:start:1" and kw["prices"][0].amount == PLANS["start"].stars


async def test_deep_link_shows_the_plans_to_an_admin_only(env):
    adapter, _ = env.build()
    m = msg("/start", user_id=999)
    m.answer = AsyncMock()
    await adapter.on_start(m, CommandObject(prefix="/", command="start", args=f"pay_{CHAT}"))
    assert "только админ" in m.answer.await_args.args[0]
    m = msg("/start", user_id=ADMIN)
    await adapter.on_start(m, CommandObject(prefix="/", command="start", args=f"pay_{CHAT}"))
    shown = sent_to(env.bot, ADMIN)[-1]
    assert "Тариф для чата" in shown.args[1] and len(shown.kwargs["reply_markup"].inline_keyboard) == len(PLANS) * len(TERMS)


# ------------------------------------------------------------- operator tools


async def test_operator_tools_work_for_operators_only(env):
    adapter, _ = env.build()
    adapter.operators = {777}
    stranger = msg("/grant", user_id=5)
    stranger.answer = AsyncMock()
    await adapter.on_grant(stranger, CommandObject(prefix="/", command="grant", args=f"{CHAT} 30 pro"))
    stranger.answer.assert_not_awaited()
    boss = msg("/grant", user_id=777)
    boss.answer = AsyncMock()
    await adapter.on_grant(boss, CommandObject(prefix="/", command="grant", args=f"{CHAT} 30 pro"))
    chat = await env.storage.get_chat(CHAT)
    assert chat["plan"] == "pro" and chat["paid_until"] > time.time() + 29 * DAY
    await adapter.on_grant(boss, CommandObject(prefix="/", command="grant", args="oops"))
    assert "Формат" in boss.answer.await_args.args[0]


async def test_refund_goes_through_telegram(env):
    adapter, _ = env.build()
    adapter.operators = {777}
    env.bot.refund_star_payment = AsyncMock()
    await env.storage.record_payment("ch9", CHAT, OWNER, "start", 1, 399)
    boss = msg("/refund", user_id=777)
    boss.answer = AsyncMock()
    await adapter.on_refund(boss, CommandObject(prefix="/", command="refund", args="ch9"))
    env.bot.refund_star_payment.assert_awaited_once_with(user_id=OWNER, telegram_payment_charge_id="ch9")
    assert (await env.storage.get_payment("ch9"))["refunded"] == 1


# --------------------------------------------------------------------- notices


async def test_reminders_are_sent_once_per_step(env):
    adapter, _ = env.build()
    adapter.bot_username = "DefenceAiBot"
    await env.storage.db.execute("UPDATE chats SET trial_until = ?", (int(time.time()) + 3 * DAY - 60,))
    await env.storage.db.commit()
    await env.storage.add_log(chat_id=CHAT, message_id=1, user_id=5, text="x", action="delete_and_ban", tokens=900)
    await adapter.send_billing_notices()
    await adapter.send_billing_notices()
    notes = [c for c in sent_to(env.bot, OWNER) if "Пробный период" in c.args[1]]
    assert len(notes) == 1 and "проверил 1" in notes[0].args[1]
    await expire(env.storage)
    await adapter.send_billing_notices()
    await adapter.send_billing_notices()
    assert len([c for c in sent_to(env.bot, OWNER) if "Срок закончился" in c.args[1]]) == 1


async def test_paying_resets_the_reminders(env):
    adapter, _ = env.build()
    await env.storage.set_billing_notice(CHAT, "expired")
    await env.storage.record_payment("c2", CHAT, OWNER, "start", 1, 399)
    assert (await env.storage.get_chat(CHAT))["billing_notice"] == ""


async def test_plan_command_for_admins_in_a_group(env):
    adapter, _ = env.build()
    adapter.bot_username = "DefenceAiBot"
    m = msg("/plan", user_id=ADMIN)
    m.reply = AsyncMock()
    await adapter.on_plan(m, CommandObject(prefix="/", command="plan"))
    assert "Пробный период" in m.reply.await_args.args[0]
    stranger = msg("/plan", user_id=999)
    stranger.reply = AsyncMock()
    await adapter.on_plan(stranger, CommandObject(prefix="/", command="plan"))
    stranger.reply.assert_not_awaited()


async def test_one_invoice_description_serves_the_chat_and_the_panel(env):
    adapter, _ = env.build()
    env.bot.create_invoice_link = AsyncMock(return_value="https://t.me/$x")
    assert await adapter.create_stars_link(CHAT, "pro", 3) == "https://t.me/$x"
    kw = env.bot.create_invoice_link.await_args.kwargs
    assert kw["currency"] == "XTR" and kw["payload"] == f"{CHAT}:pro:3" and kw["prices"][0].amount == price(PLANS["pro"], 3)
    assert len(kw["title"]) <= 32
