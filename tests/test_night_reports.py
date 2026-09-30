"""Night mode and complaints from members."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from aiogram.filters import CommandObject

from core.models import ActionType, Author, Comment
from core.moderation import ChatConfig, Moderator
from tests.test_handlers import ADMIN_ID, CHAT, OWNER, answer, env, msg, sent_to  # noqa: F401

BORDERLINE = answer("normal", 0.6, probs={"normal": 0.6, "spam": 0.05, "rule_violation": 0.3, "useful": 0.05})
NIGHT = datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)  # 03:30 in Moscow
DAY = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)  # 12:00 in Moscow


class Jev:
    async def ask(self, *args):
        return BORDERLINE


def comment(text="Ну ты и выдал, конечно", previous=10):
    return Comment(1, 1, text, Author(1, "Вася", previous_messages=previous))


async def check(now, text="Ну ты и выдал, конечно", previous=10, **config):
    return await Moderator(Jev()).check(comment(text, previous), ChatConfig(**config), now=now)


# ------------------------------------------------------------------ night mode


def test_night_hours_and_wrap_around():
    config = ChatConfig(night_mode=True, night_from=0, night_to=7)
    assert config.night_active(NIGHT) and not config.night_active(DAY)
    late = ChatConfig(night_mode=True, night_from=23, night_to=6)  # crosses midnight
    assert late.night_active(datetime(2026, 9, 29, 20, 30, tzinfo=timezone.utc))  # 23:30 MSK
    assert late.night_active(NIGHT) and not late.night_active(DAY)
    assert not ChatConfig(night_mode=False).night_active(NIGHT)


async def test_night_makes_the_check_strict():
    assert (await check(DAY, night_mode=True)).action == ActionType.NONE
    assert (await check(NIGHT, night_mode=True)).action == ActionType.SEND_TO_REVIEW
    assert (await check(NIGHT, night_mode=False)).action == ActionType.NONE


async def test_newcomers_cannot_post_links_at_night():
    text = "Смотрите разбор тут example.org/post"
    at_night = await check(NIGHT, text, previous=0, night_mode=True)
    assert at_night.action != ActionType.NONE and "ночью" in at_night.reason
    assert (await check(DAY, text, previous=0, night_mode=True)).action == ActionType.NONE
    assert "ночью" not in (await check(NIGHT, text, previous=10, night_mode=True)).reason


async def test_night_command(env):
    adapter, _ = env.build()
    m = msg("/night", user_id=ADMIN_ID)
    m.reply = AsyncMock()
    await adapter.on_night(m, CommandObject(prefix="/", command="night", args="on"))
    assert (await adapter.config(CHAT)).night_mode
    await adapter.on_night(m, CommandObject(prefix="/", command="night", args="23 6"))
    config = await adapter.config(CHAT)
    assert (config.night_from, config.night_to) == (23, 6)
    m.reply.reset_mock()
    await adapter.on_night(m, CommandObject(prefix="/", command="night", args="25 6"))
    assert (await adapter.config(CHAT)).night_from == 23  # invalid hours change nothing
    assert "Напишите" in m.reply.await_args.args[0]


# ------------------------------------------------------------------- complaints


def report(target, user_id, message_id=900):
    m = msg("/spam", user_id=user_id, message_id=message_id, reply_to=target)
    return m


def target(user_id=77, message_id=800):
    t = msg("Купи подписчиков дёшево", user_id=user_id, message_id=message_id)
    t.content_type = "text"
    return t


async def test_three_different_members_send_a_comment_to_the_owner(env):
    adapter, _ = env.build()
    t = target()
    for reporter in (11, 12):
        await adapter.on_spam_report(report(t, reporter))
    assert not sent_to(env.bot, OWNER)
    await adapter.on_spam_report(report(t, 13))
    calls = sent_to(env.bot, OWNER)
    assert len(calls) == 1 and "пожаловались 3" in calls[0].args[1]
    assert "Купи подписчиков" in calls[0].args[1]
    await adapter.on_spam_report(report(t, 14))
    assert len(sent_to(env.bot, OWNER)) == 1, "the owner is asked once"


async def test_repeated_complaints_from_one_person_count_once(env):
    adapter, _ = env.build()
    t = target()
    for _ in range(5):
        await adapter.on_spam_report(report(t, 11))
    assert not sent_to(env.bot, OWNER)


async def test_complaints_about_admins_bots_self_and_trusted_are_ignored(env):
    adapter, _ = env.build()
    await env.storage.set_trusted(CHAT, 78, True, "Друг")
    for t in (target(user_id=ADMIN_ID), target(user_id=78), target(user_id=11)):
        for reporter in (11, 12, 13):
            await adapter.on_spam_report(report(t, reporter))
    bot_target = target(user_id=79)
    bot_target.from_user.is_bot = True
    for reporter in (11, 12, 13):
        await adapter.on_spam_report(report(bot_target, reporter))
    assert not sent_to(env.bot, OWNER)


async def test_the_complaint_command_is_removed_and_the_owner_can_delete(env):
    adapter, _ = env.build()
    t = target()
    last = None
    for reporter in (11, 12, 13):
        last = report(t, reporter)
        await adapter.on_spam_report(last)
        last.delete.assert_awaited_once()
    button = sent_to(env.bot, OWNER)[0].kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    call = MagicMock(data=button, from_user=MagicMock(id=OWNER))
    call.message.html_text, call.message.edit_text, call.answer = "x", AsyncMock(), AsyncMock()
    await adapter.on_review_decision(call)
    env.bot.delete_message.assert_awaited_once_with(CHAT, 800)
