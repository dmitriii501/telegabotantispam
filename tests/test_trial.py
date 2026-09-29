"""Trial mode must be visible: the owner sees examples, and an admin can test the bot by hand."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramForbiddenError

from adapters.telegram import bot as botmod
from core.normalizer import example_key
from tests.test_handlers import ADMIN_ID, CHAT, OWNER, SPAM_BOT, answer, env, msg, sent_to  # noqa: F401


async def trial_env(env, *responses):
    await env.storage.set_setting(CHAT, "observe", 1)
    return env.build(*responses)


def spam(i=0, user_id=5):
    return msg(f"Заработок без вложений пиши в лс номер {i}", user_id=user_id, message_id=1000 + i)


def buttons(call):
    markup = call.kwargs.get("reply_markup")
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


# ---------------------------------------------------------------- what the owner sees


async def test_owner_gets_a_live_example_with_verdict_buttons(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    m = spam()
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()  # trial mode: nothing is removed
    notice = sent_to(env.bot, OWNER)[-1]
    text = notice.args[1]
    assert "Пробный режим" in text and "удалил бы и забанил" in text and "Причина" in text and "«Чат»" in text
    assert [c.split(":")[0] for c in buttons(notice)] == ["obs", "obs"]


async def test_owner_notices_are_capped_per_day(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    for i in range(botmod.TRIAL_NOTICES_PER_DAY + 3):
        await adapter.handle_comment(spam(i, user_id=100 + i))  # different authors: no antiflood
    notices = sent_to(env.bot, OWNER)
    assert len(notices) == botmod.TRIAL_NOTICES_PER_DAY
    assert "последняя подсказка" in notices[-1].args[1] and "последняя подсказка" not in notices[0].args[1]
    # everything is still recorded for /report
    assert (await env.storage.stats(CHAT, 0))["delete_and_ban"] == botmod.TRIAL_NOTICES_PER_DAY + 3


async def test_no_owner_means_no_notice_and_no_crash(env):
    await env.storage.db.execute("UPDATE chats SET owner_id = NULL WHERE chat_id = ?", (CHAT,))
    await env.storage.db.commit()
    adapter, _ = await trial_env(env, SPAM_BOT)
    await adapter.handle_comment(spam())
    assert sent_to(env.bot, OWNER) == []


async def test_no_notice_when_observe_is_off(env):
    adapter, _ = env.build(SPAM_BOT)
    await adapter.handle_comment(spam())
    assert not any("Пробный режим" in c.args[1] for c in sent_to(env.bot, OWNER))


# ------------------------------------------------------------------ verdict buttons


def call(data, user_id=OWNER):
    c = MagicMock()
    c.data, c.from_user.id = data, user_id
    c.message.html_text = "🔎 Пробный режим…"
    c.message.edit_text, c.answer = AsyncMock(), AsyncMock()
    return c


async def first_log_id(env):
    async with env.storage.db.execute("SELECT id FROM log ORDER BY id LIMIT 1") as cur:
        return (await cur.fetchone())["id"]


async def test_owner_confirms_and_the_text_is_remembered_as_spam(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    m = spam()
    await adapter.handle_comment(m)
    log_id = await first_log_id(env)
    c = call(f"obs:ok:{log_id}")
    await adapter.on_trial_verdict(c)
    assert (await env.storage.get_log(log_id))["feedback"] == "confirmed"
    assert await env.storage.example_kind(CHAT, example_key(m.text)) == "remove"
    assert "бот прав" in c.message.edit_text.await_args.args[0]


async def test_owner_marks_a_mistake_and_the_text_is_remembered_as_fine(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    m = spam()
    await adapter.handle_comment(m)
    log_id = await first_log_id(env)
    await adapter.on_trial_verdict(call(f"obs:no:{log_id}"))
    assert (await env.storage.get_log(log_id))["feedback"] == "not_spam"
    assert await env.storage.example_kind(CHAT, example_key(m.text)) == "allow"


async def test_only_the_owner_can_press_the_buttons(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    await adapter.handle_comment(spam())
    log_id = await first_log_id(env)
    stranger = call(f"obs:ok:{log_id}", user_id=999)
    await adapter.on_trial_verdict(stranger)
    assert (await env.storage.get_log(log_id))["feedback"] is None
    assert stranger.answer.await_args.args[0] == "Нет доступа."


# ------------------------------------------------------------ an admin tests the bot


async def test_admin_can_test_the_bot_with_his_own_spam(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    m = spam(user_id=ADMIN_ID)
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    reply = sent_to(env.bot, ADMIN_ID)[-1].args[1]
    assert "если бы это написал участник" in reply and "удалил бы и забанил" in reply
    assert await env.storage.stats(CHAT, 0) == {"tokens": 0}, "an admin's test is not recorded"


async def test_admin_test_stays_silent_for_a_normal_message(env):
    adapter, _ = await trial_env(env, answer())
    await adapter.handle_comment(msg("Всем привет, отличный пост", user_id=ADMIN_ID))
    assert sent_to(env.bot, ADMIN_ID) == []


async def test_admin_test_is_off_outside_trial_mode_for_commands_and_edits(env):
    adapter, jev = env.build(SPAM_BOT)
    await adapter.handle_comment(spam(user_id=ADMIN_ID))
    assert jev.calls == [] and sent_to(env.bot, ADMIN_ID) == []
    await env.storage.set_setting(CHAT, "observe", 1)
    adapter, jev = env.build(SPAM_BOT)
    await adapter.handle_comment(msg("/rules мат можно, рекламу нельзя", user_id=ADMIN_ID))
    await adapter.handle_comment(spam(user_id=ADMIN_ID), edited=True)
    assert jev.calls == []


async def test_admin_test_falls_back_to_a_short_lived_chat_reply(env):
    async def send(chat_id, *args, **kwargs):
        if chat_id == ADMIN_ID:
            raise TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot can't initiate conversation with a user")
        return MagicMock(message_id=1)

    adapter, _ = await trial_env(env, SPAM_BOT)
    env.bot.send_message.side_effect = send
    adapter.delete_later = AsyncMock()
    m = spam(user_id=ADMIN_ID)
    m.reply = AsyncMock(return_value=MagicMock(message_id=77))
    await adapter.handle_comment(m)
    await asyncio.sleep(0)
    m.reply.assert_awaited_once()
    assert "если бы это написал участник" in m.reply.await_args.args[0]
    adapter.delete_later.assert_awaited_once_with(CHAT, 77, botmod.EXPLANATION_TTL)


async def test_anonymous_admin_test_goes_to_the_owner(env):
    adapter, _ = await trial_env(env, SPAM_BOT)
    m = msg("Заработок без вложений пиши в лс", sender_chat=MagicMock(id=CHAT, title="Чат"))
    await adapter.handle_comment(m)
    assert "если бы это написал участник" in sent_to(env.bot, OWNER)[-1].args[1]


async def test_test_messages_are_capped(env):
    adapter, jev = await trial_env(env, SPAM_BOT)
    for i in range(botmod.TRIAL_ADMIN_TESTS_PER_DAY + 5):
        await adapter.handle_comment(spam(i, user_id=ADMIN_ID))
    assert len(jev.calls) == botmod.TRIAL_ADMIN_TESTS_PER_DAY


# ------------------------------------------------------------------------- ownership


async def test_first_admin_to_use_a_command_becomes_the_owner(env):
    await env.storage.db.execute("UPDATE chats SET owner_id = NULL WHERE chat_id = ?", (CHAT,))
    await env.storage.db.commit()
    adapter, _ = env.build()
    await adapter.is_admin_message(msg("/settings", user_id=ADMIN_ID))
    assert (await env.storage.get_chat(CHAT))["owner_id"] == ADMIN_ID
    adapter.is_admin = AsyncMock(return_value=True)
    await adapter.is_admin_message(msg("/settings", user_id=2))
    assert (await env.storage.get_chat(CHAT))["owner_id"] == ADMIN_ID, "a second admin does not take over"
