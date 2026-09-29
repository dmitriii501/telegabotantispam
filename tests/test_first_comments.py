"""Newcomers who comment within the first minutes under a post are judged strictly."""

from datetime import datetime, timedelta, timezone

from core.models import ActionType, Author, Comment
from core.moderation import FIRST_COMMENT_WINDOW, ChatConfig, Moderator
from tests.test_handlers import CHAT, OWNER, answer, env, msg, sent_to  # noqa: F401

# violation 35%: "normal" mode ignores it, "strict" asks the admin
BORDERLINE = answer("normal", 0.6, probs={"normal": 0.6, "spam": 0.05, "rule_violation": 0.3, "useful": 0.05})
PUBLISHED = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


class Jev:
    async def ask(self, *args):
        return BORDERLINE


def comment(after, previous=0):
    return Comment(1, 1, "Ну ты и выдал, конечно", Author(1, "Вася", previous_messages=previous), seconds_after_post=after)


async def action(after, previous=0, **config):
    return (await Moderator(Jev()).check(comment(after, previous), ChatConfig(**config))).action


async def test_a_newcomer_in_the_first_minutes_is_judged_strictly():
    assert await action(30) == ActionType.SEND_TO_REVIEW


async def test_later_comments_and_established_members_are_judged_normally():
    assert await action(FIRST_COMMENT_WINDOW + 1) == ActionType.NONE
    assert await action(None) == ActionType.NONE
    assert await action(30, previous=10) == ActionType.NONE


async def test_the_window_boundary_and_the_switch():
    assert await action(FIRST_COMMENT_WINDOW) == ActionType.SEND_TO_REVIEW
    assert await action(30, first_strict=False) == ActionType.NONE


# ---------------------------------------------------------------------- in the bot


async def publish(adapter, message_id=500, text="Пост про биткоин", date=PUBLISHED):
    post = msg(text, message_id=message_id)
    post.is_automatic_forward = True
    post.date = date
    await adapter.handle_comment(post)
    return post


def under(post, seconds, text="Ну ты и выдал, конечно", user_id=5, message_id=501):
    m = msg(text, user_id=user_id, message_id=message_id, thread=post.message_id)
    m.date = post.date + timedelta(seconds=seconds)
    return m


async def test_a_quick_newcomer_comment_goes_to_the_admin(env):
    adapter, _ = env.build(BORDERLINE)
    post = await publish(adapter)
    m = under(post, 30)
    await adapter.handle_comment(m)
    assert (await env.storage.get_post_ts(CHAT, 500)) == int(PUBLISHED.timestamp())
    assert any("возможное нарушение" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_a_late_comment_from_a_newcomer_is_left_alone(env):
    adapter, _ = env.build(BORDERLINE)
    post = await publish(adapter)
    await adapter.handle_comment(under(post, 600))
    assert not any("возможное нарушение" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_a_photo_post_without_caption_still_has_a_time(env):
    adapter, _ = env.build(BORDERLINE)
    post = msg("", message_id=510)
    post.text = None
    post.is_automatic_forward = True
    post.date = PUBLISHED
    await adapter.handle_comment(post)
    assert await env.storage.get_post_ts(CHAT, 510) == int(PUBLISHED.timestamp())
    await adapter.handle_comment(under(post, 20, message_id=511))
    assert any("возможное нарушение" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_an_unknown_post_means_no_strictness(env):
    adapter, _ = env.build(BORDERLINE)
    m = msg("Ну ты и выдал, конечно", thread=9999)
    await adapter.handle_comment(m)
    assert not any("возможное нарушение" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_a_reply_to_the_forwarded_post_counts_as_well(env):
    adapter, _ = env.build(BORDERLINE)
    post = await publish(adapter, message_id=520)
    m = msg("Ну ты и выдал, конечно", message_id=521)
    m.reply_to_message = post
    m.date = PUBLISHED + timedelta(seconds=15)
    await adapter.handle_comment(m)
    assert any("возможное нарушение" in c.args[1] for c in sent_to(env.bot, OWNER))
