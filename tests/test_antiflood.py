import time
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from adapters.telegram import bot as botmod
from core.moderation import ChatConfig
from tests.test_handlers import CHAT, answer, env, msg  # noqa: F401  (env is a fixture)
from tests.test_webapp import CHAT as WEB_CHAT, client, h  # noqa: F401  (client is a fixture)


def command(args=None):
    return CommandObject(prefix="/", command="antiflood", args=args)


def command_message():
    m = msg("/antiflood")
    m.reply = AsyncMock()
    return m


async def run_command(env, args, admin=True):
    adapter, _ = env.build()
    adapter.is_admin_message = AsyncMock(return_value=admin)
    m = command_message()
    await adapter.on_antiflood(m, command(args))
    return adapter, m


def reply_text(m):
    return m.reply.await_args.args[0] if m.reply.await_args else ""


# ------------------------------------------------------------------------------ settings


async def test_defaults(env):
    config = await env.build()[0].config(CHAT)
    assert (config.antiflood, config.flood_messages, config.flood_window, config.flood_mute_minutes) == (True, 6, 20, 30)


async def test_command_without_arguments_shows_the_current_setting(env):
    _, m = await run_command(env, None)
    text = reply_text(m)
    assert "6 сообщений за 20 с" in text and "мут на 30 мин" in text and "/antiflood 5 10 60" in text


async def test_command_sets_all_three_values_and_enables(env):
    await env.storage.set_setting(CHAT, "antiflood", 0)
    adapter, m = await run_command(env, "4 10 90")
    config = await adapter.config(CHAT)
    assert (config.antiflood, config.flood_messages, config.flood_window, config.flood_mute_minutes) == (True, 4, 10, 90)
    assert "4 сообщений за 10 с" in reply_text(m) and "мут на 1 ч 30 мин" in reply_text(m)


async def test_command_off_and_on_keep_the_thresholds(env):
    adapter, _ = await run_command(env, "4 10 90")
    await adapter.on_antiflood(command_message(), command("off"))
    config = await adapter.config(CHAT)
    assert not config.antiflood and config.flood_messages == 4
    await adapter.on_antiflood(command_message(), command("on"))
    assert (await adapter.config(CHAT)).antiflood


@pytest.mark.parametrize("args", ["2 10 30", "31 10 30", "5 4 30", "5 121 30", "5 10 0", "5 10 10081", "abc", "5 10", "5 10 30 1"])
async def test_command_rejects_bad_values_and_changes_nothing(env, args):
    adapter, m = await run_command(env, args)
    assert "Изменить" in reply_text(m)
    config = await adapter.config(CHAT)
    assert (config.flood_messages, config.flood_window, config.flood_mute_minutes) == (6, 20, 30)


async def test_command_is_for_admins_only(env):
    adapter, m = await run_command(env, "4 10 90", admin=False)
    m.reply.assert_not_awaited()
    assert (await adapter.config(CHAT)).flood_messages == 6


# ------------------------------------------------------------------------- in operation


async def test_custom_thresholds_are_used(env):
    await env.storage.set_setting(CHAT, "flood_messages", 3)
    await env.storage.set_setting(CHAT, "flood_window", 10)
    await env.storage.set_setting(CHAT, "flood_mute", 90)
    adapter, jev = env.build(answer())
    last = None
    for i in range(3):
        last = msg(f"сообщение номер {i}", message_id=800 + i)
        await adapter.handle_comment(last)
    assert len(jev.calls) == 2, "the third message trips the limit before Jev is asked"
    last.delete.assert_awaited_once()
    until = env.bot.restrict_chat_member.await_args.kwargs["until_date"]
    assert until.total_seconds() == 90 * 60


async def test_a_slower_pace_is_not_flood(env):
    adapter, _ = env.build(answer())
    now = time.time()
    adapter._flood[(CHAT, 5)] = __import__("collections").deque([now - 100, now - 90, now - 80, now - 70, now - 60], maxlen=30)
    assert adapter.flood_hit(CHAT, 5, limit=6, window=20) is False
    assert adapter.flood_hit(CHAT, 5, limit=2, window=120) is True


async def test_one_author_does_not_trip_another(env):
    adapter, _ = env.build(answer())
    for _ in range(5):
        adapter.flood_hit(CHAT, 5, limit=6, window=20)
    assert adapter.flood_hit(CHAT, 6, limit=6, window=20) is False
    assert adapter.flood_hit(CHAT, 5, limit=6, window=20) is True


# ------------------------------------------------------------------------------- panel


async def test_panel_saves_and_returns_flood_settings(client):  # noqa: F811
    resp = await client.put(
        f"/api/chat/{WEB_CHAT}/settings", json={"flood_messages": 8, "flood_window": 30, "flood_mute": 120}, headers=h()
    )
    assert resp.status == 200
    s = (await (await client.get(f"/api/chat/{WEB_CHAT}", headers=h())).json())["settings"]
    assert (s["flood_messages"], s["flood_window"], s["flood_mute"]) == (8, 30, 120)


@pytest.mark.parametrize("bad", [{"flood_messages": 2}, {"flood_messages": 31}, {"flood_window": 4}, {"flood_mute": 0},
                                 {"flood_mute": 10081}, {"flood_messages": True}, {"flood_messages": "5"}, {"flood_window": 5.5}])
async def test_panel_rejects_bad_flood_values(client, bad):  # noqa: F811
    assert (await client.put(f"/api/chat/{WEB_CHAT}/settings", json=bad, headers=h())).status == 400


def test_config_from_row_reads_flood_settings():
    row = {"rules_json": None, "rules": "", "mode": "normal", "lockdown": 0, "escalation": 1,
           "flood_messages": 4, "flood_window": 12, "flood_mute": 45}
    config = ChatConfig.from_row(row)
    assert (config.flood_messages, config.flood_window, config.flood_mute_minutes) == (4, 12, 45)
