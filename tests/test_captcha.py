"""The "I am human" check for newcomers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramBadRequest

from adapters.telegram import bot as botmod
from tests.test_handlers import CHAT, SPAM_BOT, answer, env, msg, sent_to  # noqa: F401

USEFUL = answer("useful", 0.95)


async def captcha_env(env, *responses, **settings):
    await env.storage.set_setting(CHAT, "captcha", 1)
    for key, value in settings.items():
        await env.storage.set_setting(CHAT, key, value)
    return env.build(*responses)


def challenge(env):
    """(text, {picture index: callback data}) of the last challenge posted in the chat."""
    call = sent_to(env.bot, CHAT)[-1]
    buttons = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
    return call.args[1], {int(data.split(":")[-1]): data for data in buttons}


def right_answer(text):
    return next(i for i, (_, word) in enumerate(botmod.CAPTCHA_PICTURES) if f"нажмите на {word}." in text)


def press(data, user_id):
    c = MagicMock()
    c.data, c.from_user.id = data, user_id
    c.answer, c.message.delete = AsyncMock(), AsyncMock()
    return c


# --------------------------------------------------------------------------- asking


async def test_a_newcomer_whose_first_message_passed_gets_a_challenge(env):
    adapter, _ = await captcha_env(env, answer())
    m = msg("Всем привет, отличный пост", user_id=60)
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()  # the message stays until the check fails
    text, options = challenge(env)
    assert "подтвердите, что вы человек" in text and len(options) == 4
    assert any(f"нажмите на {word}." in text for _, word in botmod.CAPTCHA_PICTURES)
    assert (CHAT, 60) in adapter._captcha


async def test_the_correct_button_is_among_the_four(env):
    adapter, _ = await captcha_env(env, answer())
    await adapter.handle_comment(msg("Всем привет", user_id=61))
    text, options = challenge(env)
    assert right_answer(text) in options


async def test_no_challenge_for_spam_established_trusted_verified_or_twice(env):
    adapter, _ = await captcha_env(env, SPAM_BOT)
    await adapter.handle_comment(msg("Заработок без вложений пиши в лс", user_id=62))
    assert (CHAT, 62) not in adapter._captcha, "spam is simply removed"

    adapter, _ = env.build(answer())
    await env.storage.count_message(CHAT, 63)
    await adapter.handle_comment(msg("Я тут давно", user_id=63, message_id=5))
    assert (CHAT, 63) not in adapter._captcha, "someone who already wrote here"

    await env.storage.set_trusted(CHAT, 64, True, "Вася")
    await adapter.handle_comment(msg("Привет", user_id=64, message_id=6))
    await env.storage.set_verified(CHAT, 65)
    await adapter.handle_comment(msg("Привет", user_id=65, message_id=7))
    assert not any(key[1] in (64, 65) for key in adapter._captcha)

    await adapter.handle_comment(msg("Привет", user_id=66, message_id=8))
    n = len(sent_to(env.bot, CHAT))
    await adapter.handle_comment(msg("Ещё одно сообщение", user_id=66, message_id=9))
    assert len(sent_to(env.bot, CHAT)) == n, "one open check per person"


async def test_no_challenge_when_the_feature_is_off_or_in_trial_mode_or_for_edits(env):
    adapter, _ = env.build(answer())
    await adapter.handle_comment(msg("Привет", user_id=67))
    assert (CHAT, 67) not in adapter._captcha  # captcha is off by default

    adapter, _ = await captcha_env(env, answer(), observe=1)
    await adapter.handle_comment(msg("Привет", user_id=68, message_id=2))
    assert (CHAT, 68) not in adapter._captcha

    await env.storage.set_setting(CHAT, "observe", 0)
    await adapter.handle_comment(msg("Привет", user_id=69, message_id=3), edited=True)
    assert (CHAT, 69) not in adapter._captcha


async def test_useful_first_comments_are_checked_but_those_awaiting_the_admin_are_not(env):
    adapter, _ = await captcha_env(env, USEFUL)
    await adapter.handle_comment(msg("Как настроить кошелёк?", user_id=70))
    assert (CHAT, 70) in adapter._captcha

    borderline = answer("normal", 0.55, probs={"normal": 0.55, "spam": 0.1, "rule_violation": 0.35, "useful": 0})
    adapter, _ = env.build(borderline)
    await adapter.handle_comment(msg("Ну ты и выдал", user_id=71, message_id=2))
    assert (CHAT, 71) not in adapter._captcha


async def test_a_first_sticker_is_checked_even_though_jev_was_not_asked(env):
    adapter, jev = await captcha_env(env, answer())
    m = msg("", user_id=72)
    m.text, m.content_type = None, "sticker"
    await adapter.handle_comment(m)
    assert jev.calls == [] and (CHAT, 72) in adapter._captcha


async def test_a_refused_challenge_does_not_break_anything(env):
    adapter, _ = await captcha_env(env, answer())
    env.bot.send_message.side_effect = TelegramBadRequest(method=MagicMock(), message="not enough rights")
    await adapter.handle_comment(msg("Привет", user_id=73))
    assert (CHAT, 73) not in adapter._captcha


# -------------------------------------------------------------------------- answering


async def test_the_right_button_passes_and_is_remembered(env):
    adapter, _ = await captcha_env(env, answer())
    await adapter.handle_comment(msg("Всем привет", user_id=80))
    text, options = challenge(env)
    c = press(options[right_answer(text)], 80)
    await adapter.on_captcha_button(c)
    assert await env.storage.is_verified(CHAT, 80) and (CHAT, 80) not in adapter._captcha
    c.message.delete.assert_awaited_once()
    assert "проверка пройдена" in c.answer.await_args.args[0]
    env.bot.restrict_chat_member.assert_not_awaited()
    n = len(sent_to(env.bot, CHAT))
    adapter, _ = env.build(answer())
    await adapter.handle_comment(msg("И ещё раз", user_id=80, message_id=9))
    assert len(sent_to(env.bot, CHAT)) == n, "a verified person is never asked again"


async def test_a_wrong_button_removes_the_message_and_mutes(env):
    adapter, _ = await captcha_env(env, answer())
    await adapter.handle_comment(msg("Всем привет", user_id=81, message_id=42))
    text, options = challenge(env)
    wrong = next(data for i, data in options.items() if i != right_answer(text))
    c = press(wrong, 81)
    await adapter.on_captcha_button(c)
    assert c.answer.await_args.args[0] == "Неверно."
    deleted = {call.args[1] for call in env.bot.delete_message.await_args_list}
    assert 42 in deleted and 555 in deleted, "the first message and the challenge itself"
    assert env.bot.restrict_chat_member.await_args.kwargs["until_date"].total_seconds() == 24 * 3600
    async with env.storage.db.execute("SELECT * FROM log ORDER BY id DESC LIMIT 1") as cur:
        row = await cur.fetchone()
    assert row["action"] == "delete_and_mute" and "я человек" in row["reason"] and row["category"] == "captcha"
    assert not await env.storage.is_verified(CHAT, 81)


async def test_only_the_addressed_person_can_press(env):
    adapter, _ = await captcha_env(env, answer())
    await adapter.handle_comment(msg("Всем привет", user_id=82))
    text, options = challenge(env)
    c = press(options[right_answer(text)], 999)
    await adapter.on_captcha_button(c)
    assert c.answer.await_args.args[0] == "Эта кнопка не для вас."
    assert (CHAT, 82) in adapter._captcha and not await env.storage.is_verified(CHAT, 82)


async def test_no_answer_in_time_removes_the_message_and_mutes(env, monkeypatch):
    monkeypatch.setattr(botmod, "CAPTCHA_TIMEOUT", 0.05)
    adapter, _ = await captcha_env(env, answer())
    await adapter.handle_comment(msg("Всем привет", user_id=83, message_id=43))
    await asyncio.sleep(0.3)
    assert (CHAT, 83) not in adapter._captcha
    assert 43 in {call.args[1] for call in env.bot.delete_message.await_args_list}
    env.bot.restrict_chat_member.assert_awaited_once()


async def test_an_answer_in_time_cancels_the_timeout_effect(env, monkeypatch):
    monkeypatch.setattr(botmod, "CAPTCHA_TIMEOUT", 0.15)
    adapter, _ = await captcha_env(env, answer())
    await adapter.handle_comment(msg("Всем привет", user_id=84))
    text, options = challenge(env)
    await adapter.on_captcha_button(press(options[right_answer(text)], 84))
    await asyncio.sleep(0.4)
    env.bot.restrict_chat_member.assert_not_awaited()


async def test_a_stale_button_after_a_restart_is_harmless(env):
    adapter, _ = await captcha_env(env, answer())
    c = press(f"cap:{CHAT}:85:2", 85)
    await adapter.on_captcha_button(c)
    assert "устарела" in c.answer.await_args.args[0]
    c.message.delete.assert_awaited_once()
    env.bot.restrict_chat_member.assert_not_awaited()


async def test_the_command_switches_the_check(env):
    from aiogram.filters import CommandObject

    adapter, _ = env.build()
    adapter.is_admin_message = AsyncMock(return_value=True)
    m = msg("/captcha on")
    m.reply = AsyncMock()
    await adapter.on_captcha_command(m, CommandObject(prefix="/", command="captcha", args="on"))
    assert (await adapter.config(CHAT)).captcha
    assert "включено" in m.reply.await_args.args[0]
