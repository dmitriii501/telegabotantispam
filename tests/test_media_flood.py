"""Stickers, photos and other messages without text count for the antiflood; service messages do not."""

from unittest.mock import MagicMock

from adapters.telegram import bot as botmod
from tests.test_handlers import CHAT, OWNER, answer, env, msg, sent_to  # noqa: F401


def media(kind="sticker", i=0, user_id=5):
    m = msg("", user_id=user_id, message_id=2000 + i)
    m.text = None
    m.content_type = kind
    return m


async def feed(adapter, n, kind="sticker", user_id=5):
    last = None
    for i in range(n):
        last = media(kind, i, user_id)
        await adapter.handle_comment(last)
    return last


async def test_sticker_flood_is_muted_and_never_sent_to_jev(env):
    adapter, jev = env.build(answer())
    last = await feed(adapter, botmod.FLOOD_MESSAGES)
    last.delete.assert_awaited_once()
    env.bot.restrict_chat_member.assert_awaited_once()
    assert jev.calls == []
    async with env.storage.db.execute("SELECT text, action, reason FROM log") as cur:
        row = await cur.fetchone()
    assert row["text"] == "[стикер]" and row["action"] == "delete_and_mute" and "флуд" in row["reason"]


async def test_a_few_stickers_are_fine(env):
    adapter, jev = env.build(answer())
    last = await feed(adapter, botmod.FLOOD_MESSAGES - 1)
    last.delete.assert_not_awaited()
    env.bot.restrict_chat_member.assert_not_awaited()
    assert await env.storage.message_count(CHAT, 5) == botmod.FLOOD_MESSAGES - 1


async def test_mixed_media_and_text_count_together(env):
    adapter, _ = env.build(answer())
    for i in range(3):
        await adapter.handle_comment(media("photo", i))
    for i in range(2):
        await adapter.handle_comment(msg(f"обычное сообщение {i}", message_id=3000 + i))
    last = media("voice", 9)
    await adapter.handle_comment(last)
    last.delete.assert_awaited_once()


async def test_service_messages_are_ignored(env):
    adapter, jev = env.build(answer())
    last = await feed(adapter, botmod.FLOOD_MESSAGES + 4, kind="new_chat_members")
    last.delete.assert_not_awaited()
    assert await env.storage.message_count(CHAT, 5) == 0


async def test_edits_of_media_are_ignored(env):
    adapter, jev = env.build(answer())
    m = media()
    await adapter.handle_comment(m, edited=True)
    assert jev.calls == [] and await env.storage.message_count(CHAT, 5) == 0


async def test_antiflood_off_means_media_is_not_limited(env):
    await env.storage.set_setting(CHAT, "antiflood", 0)
    adapter, _ = env.build(answer())
    last = await feed(adapter, botmod.FLOOD_MESSAGES + 3)
    last.delete.assert_not_awaited()


async def test_lockdown_removes_stickers_too(env):
    await env.storage.set_setting(CHAT, "lockdown", 1)
    adapter, _ = env.build(answer())
    m = media()
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()


async def test_trial_notice_for_a_media_flood_does_not_crash(env):
    await env.storage.set_setting(CHAT, "observe", 1)
    adapter, _ = env.build(answer())
    last = await feed(adapter, botmod.FLOOD_MESSAGES)
    last.delete.assert_not_awaited()
    notice = sent_to(env.bot, OWNER)[-1].args[1]
    assert "Пробный режим" in notice and "[сообщение без текста]" in notice
