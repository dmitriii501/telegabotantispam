"""The first week of sales: the welcome, labels, alerts, follow-ups, trial reports, signature, invitations, neighbours."""

import time
from unittest.mock import AsyncMock, MagicMock

from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandObject
from aiogram.types import Message

from adapters.telegram.common import find_companions
from adapters.telegram.growth import clean_source, parse_start
from core.billing import DAY, PILOT_KEYS, access, pilot_report_due
from tests.test_handlers import CHAT, OWNER, SPAM, SPAM_BOT, answer, joined_event, msg, sent_to
from tests.test_webapp import CHAT as WEB_CHAT
from tests.test_webapp import h

OPERATOR = 777
ALERTS = -1009
NOW = 1_800_000_000  # 11:00 in Moscow: follow-ups may be sent


def private(text="/start", user_id=900, name="Анна Петрова", username="anna"):
    m = msg(text, user_id=user_id)
    m.chat.type = "private"
    m.from_user.full_name, m.from_user.username = name, username
    m.answer = AsyncMock()
    return m


def command(name, args=None):
    return CommandObject(prefix="/", command=name, args=args)


def adapter_of(env, *responses):
    adapter, jev = env.build(*responses)
    adapter.operators = {OPERATOR}
    adapter.alert_chat = ALERTS
    return adapter, jev


def told(env, chat_id):
    return [c.args[1] for c in sent_to(env.bot, chat_id)]


# ------------------------------------------------------------------- labels and the welcome


def test_start_labels_are_cleaned_and_invitations_recognised():
    assert parse_start("") == ("direct", None)
    assert parse_start("ads_bp") == ("ads_bp", None)
    assert parse_start("  YT-Video 7!! ") == ("yt-video7", None)
    assert parse_start("ref_12345") == ("ref", 12345)
    assert parse_start("ref_abc") == ("ref", None)
    assert clean_source("😀") == "direct" and len(clean_source("a" * 99)) == 32


async def test_start_shows_the_short_welcome_with_the_three_buttons(env):
    adapter, _ = adapter_of(env)
    m = private()
    await adapter.on_start(m, command("start", "ads_bp"))
    text, markup = m.answer.await_args.args[0], m.answer.await_args.kwargs["reply_markup"]
    assert "DefenceAi" in text and len(text) < 700
    buttons = [b for row in markup.inline_keyboard for b in row]
    connect = buttons[0]
    assert connect.text == "➕ Подключить к каналу"
    assert connect.url == "https://t.me/DefenceAiBot?startgroup=connect&admin=delete_messages+restrict_members"
    assert [b.text for b in buttons[1:]] == ["❓ Все команды"], "the panel button appears when the site is configured"
    assert buttons[1].callback_data == "help:all"


async def test_the_panel_button_is_there_when_the_site_is_configured(env):
    adapter, _ = adapter_of(env)
    adapter.webapp_url = "https://x.example"
    m = private()
    await adapter.on_start(m, command("start"))
    buttons = [b for row in m.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["➕ Подключить к каналу", "⚙️ Панель", "❓ Все команды"]
    assert buttons[1].web_app.url == "https://x.example/app/"


async def test_all_commands_button_sends_the_full_list(env):
    adapter, _ = adapter_of(env)
    call = MagicMock(data="help:all")
    call.message = MagicMock(spec=Message)
    call.message.answer = AsyncMock()
    call.answer = AsyncMock()
    await adapter.on_help_button(call)
    assert "/rules" in call.message.answer.await_args.args[0]


async def test_the_first_label_wins_and_each_person_is_announced_once(env, caplog):
    adapter, _ = adapter_of(env)
    caplog.set_level("INFO")
    await adapter.on_start(private(), command("start", "ads_bp"))
    await adapter.on_start(private(), command("start", "yt"))
    assert (await env.storage.get_start(900))["source"] == "ads_bp"
    assert "start source=ads_bp" in caplog.text and "start source=yt" in caplog.text
    alerts = told(env, ALERTS)
    assert len(alerts) == 1
    assert "Новый /start" in alerts[0] and "Анна Петрова" in alerts[0] and "@anna" in alerts[0] and "ads_bp" in alerts[0]


async def test_operators_own_start_is_not_announced(env):
    adapter, _ = adapter_of(env)
    await adapter.on_start(private(user_id=OPERATOR), command("start"))
    assert told(env, ALERTS) == []


async def test_alerts_go_to_the_operators_when_there_is_no_alert_chat(env):
    adapter, _ = adapter_of(env)
    adapter.alert_chat = None
    await adapter.on_start(private(), command("start", "dm"))
    assert any("Новый /start" in t for t in told(env, OPERATOR))


# ------------------------------------------------------------------- /sources


async def test_sources_count_starts_and_connections_per_label(env):
    adapter, _ = adapter_of(env)
    for user, label in [(1001, "ads_bp"), (1002, "ads_bp"), (1003, "ads_bp"), (1004, "yt")]:
        await adapter.on_start(private(user_id=user), command("start", label))
    await env.storage.set_added_by(CHAT, 1001)  # one of the three connected a chat
    boss = private("/sources", user_id=OPERATOR)
    await adapter.on_sources(boss, command("sources"))
    text = boss.answer.await_args.args[0]
    assert "ads_bp</code>: 3 → 1 (33%)" in text and "yt</code>: 1 → 0 (0%)" in text and "Итого:</b> 4 → 1 (25%)" in text
    stranger = private("/sources", user_id=5)
    await adapter.on_sources(stranger, command("sources"))
    stranger.answer.assert_not_awaited()


async def test_sources_can_look_at_a_period(env):
    adapter, _ = adapter_of(env)
    await adapter.on_start(private(user_id=1001), command("start", "old"))
    await env.storage.db.execute("UPDATE starts SET ts = ?", (int(time.time()) - 10 * DAY,))
    await env.storage.db.commit()
    await adapter.on_start(private(user_id=1002), command("start", "fresh"))
    boss = private("/sources", user_id=OPERATOR)
    await adapter.on_sources(boss, command("sources", "7"))
    text = boss.answer.await_args.args[0]
    assert "fresh" in text and "old" not in text and "за 7 дней" in text


# ------------------------------------------------------------------- the bot is added to a chat


async def test_the_operator_hears_about_a_new_chat_with_its_numbers_and_the_adder(env):
    adapter, _ = adapter_of(env)
    await adapter.on_start(private(user_id=1001, name="Борис", username="boris"), command("start", "cat"))
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.ADMINISTRATOR, can_delete_messages=True, can_restrict_members=True))
    env.bot.id = 1
    await adapter.on_bot_added(joined_event(-100777, adder_id=1001, title="Котики"))
    notice = [t for t in told(env, ALERTS) if "добавили в чат" in t][0]
    assert "Котики" in notice and "120 участников" in notice and "@chan_admin" in notice and "id 1001" in notice and "cat" in notice
    assert (await env.storage.get_chat(-100777))["added_by"] == 1001
    await adapter.on_bot_added(joined_event(-100777, adder_id=1001, title="Котики"))
    assert len([t for t in told(env, ALERTS) if "добавили в чат" in t]) == 1, "re-adding the bot is not news"


async def test_an_adder_without_start_is_still_reported(env):
    adapter, _ = adapter_of(env)
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.ADMINISTRATOR, can_delete_messages=True, can_restrict_members=True))
    await adapter.on_bot_added(joined_event(-100778, adder_id=555))
    assert "без /start" in [t for t in told(env, ALERTS) if "добавили в чат" in t][0]


# ------------------------------------------------------------------- follow-ups


async def started(env, adapter, user_id=1001, label="ads_bp", age=0):
    await adapter.on_start(private(user_id=user_id), command("start", label))
    await env.storage.db.execute("UPDATE starts SET ts = ? WHERE user_id = ?", (NOW - age, user_id))
    await env.storage.db.commit()


async def test_a_person_who_did_not_connect_gets_one_nudge_after_an_hour_and_one_after_a_day(env):
    adapter, _ = adapter_of(env)
    await started(env, adapter, age=30 * 60)
    assert await adapter.send_nudges(NOW) == 0, "half an hour: too early"
    await env.storage.db.execute("UPDATE starts SET ts = ?", (NOW - 3700,))
    await env.storage.db.commit()
    assert await adapter.send_nudges(NOW) == 1
    first = sent_to(env.bot, 1001)[-1]
    assert "Помочь подключить? Это 2 минуты" in first.args[1]
    labels = [b.text for b in first.kwargs["reply_markup"].inline_keyboard[0]]
    assert labels == ["➕ Подключить"], "no support contact is configured"
    assert await adapter.send_nudges(NOW + 600) == 0, "not twice in the same stage"
    assert await adapter.send_nudges(NOW + 24 * 3600) == 1
    assert await adapter.send_nudges(NOW + 5 * 24 * 3600) == 0, "never more than two"
    assert len(sent_to(env.bot, 1001)) == 2


async def test_the_nudge_has_a_button_to_write_to_the_author(env, monkeypatch):
    monkeypatch.setenv("SUPPORT_CONTACT", "@defenceai_support")
    adapter, _ = adapter_of(env)
    await started(env, adapter, age=7200)
    await adapter.send_nudges(NOW)
    buttons = sent_to(env.bot, 1001)[-1].kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["➕ Подключить", "💬 Написать автору"]
    assert buttons[1].url == "https://t.me/defenceai_support"


async def test_nobody_is_nudged_who_connected_a_chat_or_blocked_the_bot(env):
    adapter, _ = adapter_of(env)
    await started(env, adapter, user_id=1001, age=7200)
    await env.storage.set_added_by(CHAT, 1001)
    await started(env, adapter, user_id=1002, age=7200)
    await started(env, adapter, user_id=OPERATOR, age=7200)
    env.bot.send_message = AsyncMock(side_effect=lambda chat_id, *a, **k: (_ for _ in ()).throw(__import__("aiogram").exceptions.TelegramForbiddenError(MagicMock(), "blocked")) if chat_id == 1002 else MagicMock(message_id=1))
    assert await adapter.send_nudges(NOW) == 0
    assert (await env.storage.get_start(1002))["nudges"] == 2, "a person who blocked the bot is left alone"
    assert (await env.storage.get_start(1001))["nudges"] == 2
    assert (await env.storage.get_start(OPERATOR))["nudges"] == 2


async def test_nudges_wait_for_the_daytime(env):
    adapter, _ = adapter_of(env)
    await started(env, adapter, age=7200)
    assert await adapter.send_nudges(NOW - 8 * 3600) == 0, "03:00 in Moscow"
    assert await adapter.send_nudges(NOW + 12 * 3600) == 0, "23:00 in Moscow"
    assert await adapter.send_nudges(NOW) == 1


# ------------------------------------------------------------------- trial reports


def test_trial_report_schedule():
    start, until = NOW, NOW + 14 * DAY
    assert pilot_report_due(start, until, NOW + 1 * DAY) is None
    assert pilot_report_due(start, until, NOW + 2 * DAY + 100) == "d3"
    assert pilot_report_due(start, until, NOW + 2 * DAY + 100, "d3") is None
    assert pilot_report_due(start, until, NOW + 6 * DAY + 100, "d3") == "d7"
    assert pilot_report_due(start, until, NOW + 12 * DAY + 100, "d7") == "d13"
    assert pilot_report_due(start, until, NOW + 13 * DAY + 100, "d13") == "final"
    assert pilot_report_due(start, until, NOW + 13 * DAY + 100, "final") is None
    assert pilot_report_due(start, until, NOW + 14 * DAY + 1) is None, "after the end the expiry notice takes over"
    assert pilot_report_due(start, until, NOW + 13 * DAY + 100, "") == "final", "the outage skipped d3..d13: only the latest is sent"
    assert PILOT_KEYS == ("d3", "d7", "d13", "final")


async def lead_log(env, text="Сколько стоит?", lead=0.9, **extra):
    fields = {
        "chat_id": CHAT, "message_id": extra.pop("message_id", 11), "user_id": 5, "user_name": "Ира", "text": text,
        "category": "useful", "confidence": 0.9, "bot_probability": 0.05, "action": "forward_useful", "lead": lead,
    }
    fields.update(extra)
    return await env.storage.add_log(**fields)


async def test_the_owner_gets_the_trial_reports_with_numbers_and_examples(env):
    adapter, _ = adapter_of(env)
    for i in range(3):
        await env.storage.add_log(chat_id=CHAT, message_id=i, user_id=7, user_name="Бот", text="спам", category="spam", confidence=0.99,
                                  bot_probability=0.9, action="delete_and_ban")
    await env.storage.add_log(chat_id=CHAT, message_id=9, user_id=8, user_name="Ян", text="спам2", category="spam", confidence=0.9,
                              bot_probability=0.1, action="delete_silent", feedback="not_spam")
    await lead_log(env, "Сколько стоит?", 0.95, message_id=21)
    await lead_log(env, "Как записаться?", 0.9, message_id=22)
    await lead_log(env, "Есть ли доставка?", 0.85, message_id=23)
    await lead_log(env, "Просто привет", 0.02, message_id=24)
    now = int(time.time()) + 2 * DAY + 60
    await adapter.send_pilot_reports(now)
    report = told(env, OWNER)[-1]
    assert "Итоги 2 дней" in report or "Итоги" in report
    assert "Проверено комментариев: 8" in report
    assert "удалено: 4" in report and "ботов забанено: 3" in report
    assert "Вопросов покупателей найдено: 3" in report
    assert "Сколько стоит?" in report and "Как записаться?" in report and "Просто привет" not in report
    assert "отменили: 1" in report
    assert (await env.storage.get_chat(CHAT))["pilot_report"] == "d3"
    before = len(told(env, OWNER))
    await adapter.send_pilot_reports(now + 3600)
    assert len(told(env, OWNER)) == before, "a report is sent once"


async def test_the_last_day_report_has_the_payment_button_and_replaces_the_reminder(env):
    adapter, _ = adapter_of(env)
    await lead_log(env)
    await env.storage.db.execute("UPDATE chats SET trial_until = ?", (int(time.time()) + DAY // 2,))
    await env.storage.db.commit()
    await adapter.send_pilot_reports()
    call = sent_to(env.bot, OWNER)[-1]
    assert "Итоги пробного периода" in call.args[1] and "1 день" in call.args[1]
    assert "start=pay_" in call.kwargs["reply_markup"].inline_keyboard[0][0].url
    count = len(sent_to(env.bot, OWNER))
    await adapter.send_billing_notices()
    assert len(sent_to(env.bot, OWNER)) == count, "no second 'one day left' message"


async def test_a_trial_report_in_the_observe_mode_says_what_would_have_been_removed(env):
    adapter, _ = adapter_of(env)
    await env.storage.set_setting(CHAT, "observe", 1)
    await env.storage.add_log(chat_id=CHAT, message_id=1, user_id=7, user_name="Бот", text="спам", category="spam", confidence=0.99,
                              bot_probability=0.9, action="delete_and_ban", executed=0)
    await adapter.send_pilot_reports(int(time.time()) + 3 * DAY)
    report = told(env, OWNER)[-1]
    assert "удалил бы: 1" in report and "забанил бы: 1" in report and "/observe off" in report


async def test_an_empty_trial_tells_the_owner_to_check_the_rights(env):
    adapter, _ = adapter_of(env)
    await adapter.send_pilot_reports(int(time.time()) + 3 * DAY)
    assert "Комментариев пока не было" in told(env, OWNER)[-1]


async def test_paid_chats_get_no_trial_reports(env):
    adapter, _ = adapter_of(env)
    await env.storage.grant(CHAT, "start", 30)
    await adapter.send_pilot_reports(int(time.time()) + 3 * DAY)
    assert told(env, OWNER) == []


# ------------------------------------------------------------------- buyers' questions


LEAD = answer("useful", 0.9, 0.02, kind={"type": "choice", "choice": "question"}, lead={"type": "noul", "noul": 0.93}, needs_answer={"type": "noul", "noul": 0.8})
CHEAP_TALK = answer("normal", 0.9, 0.02, lead={"type": "noul", "noul": 0.03})


async def test_a_buyers_question_reaches_the_owner_at_once_by_default(env):
    adapter, _ = adapter_of(env, LEAD)
    m = msg("Сколько стоит доставка в Казань?")
    await adapter.handle_comment(m)
    note = told(env, OWNER)[-1]
    assert "Вопрос покупателя" in note and "Сколько стоит доставка" in note and "t.me/c/" in note
    m.delete.assert_not_awaited()


async def test_a_buyers_question_is_reported_also_in_the_trial_mode(env):
    await env.storage.set_setting(CHAT, "observe", 1)
    adapter, _ = adapter_of(env, LEAD)
    await adapter.handle_comment(msg("Как записаться?"))
    assert "Вопрос покупателя" in told(env, OWNER)[-1]


async def test_ordinary_talk_and_spam_are_not_buyers(env):
    adapter, _ = adapter_of(env, CHEAP_TALK)
    await adapter.handle_comment(msg("классный пост, спасибо большое"))
    assert not [t for t in told(env, OWNER) if "Вопрос покупателя" in t]
    spam_with_lead = answer("spam", 0.99, 0.9, "r0", lead={"type": "noul", "noul": 0.95})
    adapter, _ = adapter_of(env, spam_with_lead)
    await adapter.handle_comment(msg("Заработок без вложений пиши мне", user_id=6))
    assert not [t for t in told(env, OWNER) if "Вопрос покупателя" in t]


async def test_the_owner_can_turn_instant_buyer_alerts_off(env):
    await env.storage.set_setting(CHAT, "leads_instant", 0)
    adapter, _ = adapter_of(env, LEAD)
    await adapter.handle_comment(msg("Сколько стоит?"))
    assert not [t for t in told(env, OWNER) if "Вопрос покупателя" in t]


async def test_a_busy_chat_cannot_flood_the_owner_with_buyer_alerts(env, monkeypatch):
    monkeypatch.setattr("adapters.telegram.pipeline.LEAD_ALERTS_PER_DAY", 2)
    adapter, _ = adapter_of(env, LEAD)
    for i in range(5):
        await adapter.handle_comment(msg(f"Сколько стоит товар номер {i}?", user_id=20 + i, message_id=300 + i))
    assert len([t for t in told(env, OWNER) if "Вопрос покупателя" in t]) == 2


# ------------------------------------------------------------------- the signature


async def test_the_deletion_notice_in_the_group_is_signed(env):
    adapter, _ = adapter_of(env, SPAM)
    await adapter.handle_comment(msg("Реклама моего канала t.me/x"))
    text = sent_to(env.bot, CHAT)[-1].args[1]
    assert text.endswith('<a href="https://t.me/DefenceAiBot?start=sig">🛡 DefenceAi</a>')


async def test_a_silent_removal_says_nothing_so_there_is_no_signature(env):
    adapter, _ = adapter_of(env, SPAM_BOT)
    await adapter.handle_comment(msg("Заработок без вложений пиши мне"))
    assert sent_to(env.bot, CHAT) == []


async def test_the_captcha_is_signed(env):
    adapter, _ = adapter_of(env, answer())
    await env.storage.set_setting(CHAT, "captcha", 1)
    await adapter.handle_comment(msg("привет всем, как дела у вас", user_id=70))
    assert "DefenceAi" in sent_to(env.bot, CHAT)[-1].args[1] and "подтвердите" in sent_to(env.bot, CHAT)[-1].args[1]


async def test_the_greeting_in_a_new_chat_is_signed(env):
    adapter, _ = adapter_of(env)
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.ADMINISTRATOR, can_delete_messages=True, can_restrict_members=True))
    await adapter.on_bot_added(joined_event(-100999))
    assert "Готов к работе" in sent_to(env.bot, -100999)[-1].args[1] and "start=sig" in sent_to(env.bot, -100999)[-1].args[1]


async def test_only_paid_plans_can_switch_the_signature_off(env):
    adapter, _ = adapter_of(env, SPAM)
    admin = msg("/signature off", user_id=1)
    admin.reply = AsyncMock()
    await adapter.on_signature(admin, command("signature", "off"))
    assert "платном тарифе" in admin.reply.await_args.args[0]
    assert (await env.storage.get_chat(CHAT))["signature"] == 1
    await env.storage.grant(CHAT, "start", 30)
    adapter._usage.clear()
    await adapter.on_signature(admin, command("signature", "off"))
    assert (await env.storage.get_chat(CHAT))["signature"] == 0
    await adapter.handle_comment(msg("Реклама моего канала t.me/x"))
    assert "DefenceAi" not in sent_to(env.bot, CHAT)[-1].args[1]


async def test_a_trial_chat_keeps_the_signature_even_if_the_setting_says_off(env):
    adapter, _ = adapter_of(env, SPAM)
    await env.storage.set_setting(CHAT, "signature", 0)
    await adapter.handle_comment(msg("Реклама моего канала t.me/x"))
    assert "DefenceAi" in sent_to(env.bot, CHAT)[-1].args[1]


# ------------------------------------------------------------------- invitations


async def pay(adapter, charge, payer, chat=CHAT):
    return await adapter.complete_payment(charge, chat, payer, "start", 1, 399, "XTR")


async def invite(env, adapter, inviter=1001, invitee=1002):
    await adapter.on_start(private(user_id=inviter), command("start"))
    m = private(user_id=invitee)
    await adapter.on_start(m, command("start", f"ref_{inviter}"))
    return m


async def test_ref_gives_a_personal_link(env):
    adapter, _ = adapter_of(env)
    m = private("/ref", user_id=1001)
    await adapter.on_ref(m)
    text = m.answer.await_args.args[0]
    assert "https://t.me/DefenceAiBot?start=ref_1001" in text and "30 дней" in text and "Приглашено: 0" in text


async def test_an_invitation_is_recorded_once_and_self_invitation_is_ignored(env):
    adapter, _ = adapter_of(env)
    await invite(env, adapter)
    assert (await env.storage.referral_of(1002))["referrer_id"] == 1001
    assert (await env.storage.get_start(1002))["source"] == "ref"
    await adapter.on_start(private(user_id=1003), command("start", "ref_1003"))
    assert await env.storage.referral_of(1003) is None
    await adapter.on_start(private(user_id=1004), command("start", "ref_424242"))
    assert await env.storage.referral_of(1004) is None, "the inviter must be someone who knows the bot"
    await adapter.on_start(private(user_id=1001), command("start", "ref_1002"))
    assert await env.storage.referral_of(1001) is None, "an old user cannot be invited"


async def test_both_get_thirty_days_when_the_invited_one_pays_the_first_month(env):
    adapter, _ = adapter_of(env)
    await invite(env, adapter)
    await env.storage.set_added_by(CHAT, 1001)  # the inviter has a chat; the invitee pays for another one
    await env.storage.ensure_chat(-100500)
    before_inviter = (await env.storage.get_chat(CHAT))["trial_until"]
    thanks = await pay(adapter, "c1", 1002, chat=-100500)
    assert "30 дней в подарок" in thanks
    paid = (await env.storage.get_chat(-100500))["paid_until"]
    assert paid > time.time() + 59 * DAY, "30 days bought + 30 days given"
    assert (await env.storage.get_chat(CHAT))["trial_until"] == before_inviter + 30 * DAY
    assert any("Приглашённый вами" in t for t in told(env, 1001))
    stats = await env.storage.referral_stats(1001)
    assert stats == {"invited": 1, "paid": 1, "days": 30}


async def test_only_the_first_payment_is_rewarded(env):
    adapter, _ = adapter_of(env)
    await invite(env, adapter)
    await env.storage.set_added_by(CHAT, 1001)
    await env.storage.ensure_chat(-100500)
    await pay(adapter, "c1", 1002, chat=-100500)
    until = (await env.storage.get_chat(-100500))["paid_until"]
    second = await pay(adapter, "c2", 1002, chat=-100500)
    assert "подарок" not in second
    assert (await env.storage.get_chat(-100500))["paid_until"] == until + 30 * DAY


async def test_a_payment_of_someone_not_invited_gives_no_gift(env):
    adapter, _ = adapter_of(env)
    assert "подарок" not in await pay(adapter, "c1", 1002)


async def test_an_inviter_without_a_chat_gets_the_days_on_the_first_chat(env):
    adapter, _ = adapter_of(env)
    await invite(env, adapter, inviter=2001, invitee=2002)
    await env.storage.ensure_chat(-100500)
    await pay(adapter, "c1", 2002, chat=-100500)
    assert any("как только вы подключите" in t for t in told(env, 2001))
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.ADMINISTRATOR, can_delete_messages=True, can_restrict_members=True))
    await adapter.on_bot_added(joined_event(-100888, adder_id=2001))
    chat = await env.storage.get_chat(-100888)
    assert chat["trial_until"] > time.time() + 40 * DAY
    assert any("+30 дней" in t for t in told(env, 2001))


async def test_a_refund_takes_the_gift_back_from_both(env):
    adapter, _ = adapter_of(env)
    adapter.operators = {OPERATOR}
    env.bot.refund_star_payment = AsyncMock()
    await invite(env, adapter)
    await env.storage.set_added_by(CHAT, 1001)
    await env.storage.ensure_chat(-100500)
    inviter_trial = (await env.storage.get_chat(CHAT))["trial_until"]
    await pay(adapter, "c1", 1002, chat=-100500)
    boss = private("/refund", user_id=OPERATOR)
    await adapter.on_refund(boss, command("refund", "c1"))
    assert (await env.storage.get_chat(CHAT))["trial_until"] == inviter_trial
    assert (await env.storage.referral_stats(1001))["days"] == 0


async def test_sources_show_the_invitations_to_the_operator(env):
    adapter, _ = adapter_of(env)
    await invite(env, adapter)
    boss = private("/sources", user_id=OPERATOR)
    await adapter.on_sources(boss, command("sources"))
    assert "Рефералы: приглашено 1, оплатили 0" in boss.answer.await_args.args[0]


# ------------------------------------------------------------------- next to another bot


def admin_member(user_id, username=None, is_bot=False):
    return MagicMock(user=MagicMock(id=user_id, username=username, is_bot=is_bot))


def test_other_moderation_bots_are_found_among_admins():
    members = [admin_member(1), admin_member(2, "combot", True), admin_member(3, "MissRose_bot", True), admin_member(4, "shieldy_bot", True),
               admin_member(5, "ChatKeeperBot", True), admin_member(6, "combot_fan", False), admin_member(7, "pizzabot", True)]
    assert find_companions(members) == "ChatKeeper, Combot, Rose, Shieldy"
    assert find_companions([admin_member(1)]) == ""


async def bot_joins_next_to(env, adapter, chat_id=-100999, bot_name="combot"):
    env.bot.get_chat_administrators = AsyncMock(return_value=[admin_member(1), admin_member(99, bot_name, True)])
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.ADMINISTRATOR, can_delete_messages=True, can_restrict_members=True))
    await adapter.on_bot_added(joined_event(chat_id, adder_id=1001))


async def test_next_to_another_bot_the_greeting_is_skipped_and_the_adder_is_told(env):
    adapter = adapter_of(env)[0]
    adapter.is_admin = type(adapter).is_admin.__get__(adapter)  # the real one: it is where the admins are read
    await bot_joins_next_to(env, adapter)
    assert (await env.storage.get_chat(-100999))["companions"] == "Combot"
    assert sent_to(env.bot, -100999) == [], "no second hello in the group"
    assert any("Combot" in t and "не пишу своё приветствие" in t for t in told(env, 1001))


async def test_next_to_another_bot_the_captcha_is_not_repeated(env):
    adapter, _ = adapter_of(env, answer())
    await env.storage.set_setting(CHAT, "captcha", 1)
    await env.storage.set_companions(CHAT, "Combot")
    await adapter.handle_comment(msg("привет всем, как дела у вас", user_id=71))
    assert not [t for t in told(env, CHAT) if "подтвердите" in t]
    await env.storage.set_setting(CHAT, "coexist", 0)
    await adapter.handle_comment(msg("привет всем, как дела у вас", user_id=72))
    assert [t for t in told(env, CHAT) if "подтвердите" in t], "with /coexist off the bot works fully"


async def test_the_report_says_what_the_neighbour_missed(env):
    adapter, _ = adapter_of(env)
    await env.storage.set_companions(CHAT, "Combot")
    for i in range(4):
        await env.storage.add_log(chat_id=CHAT, message_id=i, user_id=7, user_name="Бот", text="спам", category="spam", confidence=0.99,
                                  bot_probability=0.9, action="delete_silent")
    await adapter.send_pilot_reports(int(time.time()) + 3 * DAY)
    assert "Рядом работает Combot. Спама, который он пропустил, а я поймал: 4" in told(env, OWNER)[-1]


async def test_companions_are_noted_when_admins_are_read_later(env):
    adapter = adapter_of(env)[0]
    adapter.is_admin = type(adapter).is_admin.__get__(adapter)
    env.bot.get_chat_administrators = AsyncMock(return_value=[admin_member(1), admin_member(99, "shieldy_bot", True)])
    await adapter.is_admin(CHAT, 1, fresh=True)
    assert (await env.storage.get_chat(CHAT))["companions"] == "Shieldy"
    env.bot.get_chat_administrators = AsyncMock(return_value=[admin_member(1)])
    await adapter.is_admin(CHAT, 1, fresh=True)
    assert (await env.storage.get_chat(CHAT))["companions"] == "", "the other bot left"


# ------------------------------------------------------------------- the panel


async def test_the_panel_has_the_new_switches_and_locks_the_signature_on_the_trial(client):  # noqa: F811
    client.adapter.access_for = AsyncMock(return_value=access({"plan": "", "paid_until": None, "trial_until": time.time() + DAY}, 0))
    data = await (await client.get(f"/api/chat/{WEB_CHAT}", headers=h())).json()
    assert {"signature", "leads_instant", "coexist"} <= data["settings"].keys() and data["settings"]["leads_instant"] is True
    assert (await client.put(f"/api/chat/{WEB_CHAT}/settings", json={"leads_instant": False, "coexist": False}, headers=h())).status == 200
    resp = await client.put(f"/api/chat/{WEB_CHAT}/settings", json={"signature": False}, headers=h())
    assert resp.status == 403 and "платном тарифе" in (await resp.json())["error"]
    client.adapter.access_for = AsyncMock(return_value=access({"plan": "start", "paid_until": time.time() + DAY, "trial_until": 0}, 0))
    assert (await client.put(f"/api/chat/{WEB_CHAT}/settings", json={"signature": False}, headers=h())).status == 200
    s = (await (await client.get(f"/api/chat/{WEB_CHAT}", headers=h())).json())["settings"]
    assert (s["signature"], s["leads_instant"], s["coexist"]) == (False, False, False)
