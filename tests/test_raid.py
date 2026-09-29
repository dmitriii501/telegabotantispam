"""Service messages, raid alerts and the timed freeze."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject

from adapters.telegram import bot as botmod
from core.moderation import ChatConfig
from tests.test_handlers import ADMIN_ID, CHAT, OWNER, answer, env, msg, sent_to  # noqa: F401
from tests.test_webapp import CHAT as WEB_CHAT, client, h  # noqa: F401


def service(kind, joined=1, user_id=5, i=0):
    m = msg("", user_id=user_id, message_id=7000 + i)
    m.text = None
    m.content_type = kind
    m.new_chat_members = [MagicMock() for _ in range(joined)] if kind == "new_chat_members" else None
    return m


# --------------------------------------------------------------------- service messages


@pytest.mark.parametrize("kind", ["new_chat_members", "left_chat_member", "pinned_message", "new_chat_title",
                                  "video_chat_started", "message_auto_delete_timer_changed"])
async def test_service_messages_are_removed_when_switched_on(env, kind):
    await env.storage.set_setting(CHAT, "clean_service", 1)
    await env.storage.set_setting(CHAT, "antiraid", 0)
    adapter, _ = env.build(answer())
    m = service(kind)
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()


@pytest.mark.parametrize("kind", ["migrate_to_chat_id", "forum_topic_created"])
async def test_structural_service_messages_are_kept(env, kind):
    await env.storage.set_setting(CHAT, "clean_service", 1)
    adapter, _ = env.build(answer())
    m = service(kind)
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()


async def test_service_messages_stay_by_default_and_in_trial_mode(env):
    adapter, _ = env.build(answer())
    m = service("left_chat_member")
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    await env.storage.set_setting(CHAT, "clean_service", 1)
    await env.storage.set_setting(CHAT, "observe", 1)
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()


async def test_a_refused_deletion_of_a_service_message_is_harmless(env):
    await env.storage.set_setting(CHAT, "clean_service", 1)
    adapter, _ = env.build(answer())
    m = service("left_chat_member")
    m.delete.side_effect = TelegramBadRequest(method=MagicMock(), message="not enough rights")
    await adapter.handle_comment(m)


# --------------------------------------------------------------------------------- raid


async def test_a_wave_of_joins_alerts_the_owner_once(env):
    adapter, _ = env.build(answer())
    for i in range(botmod.RAID_JOINS - 1):
        await adapter.handle_comment(service("new_chat_members", i=i))
    assert sent_to(env.bot, OWNER) == []
    await adapter.handle_comment(service("new_chat_members", i=99))
    alert = sent_to(env.bot, OWNER)[-1]
    assert "Возможный рейд" in alert.args[1] and f"{botmod.RAID_JOINS} новых участников" in alert.args[1]
    assert [b.callback_data for row in alert.kwargs["reply_markup"].inline_keyboard for b in row] == [f"raid:on:{CHAT}", f"raid:no:{CHAT}"]
    for i in range(10):
        await adapter.handle_comment(service("new_chat_members", i=200 + i))
    assert len([c for c in sent_to(env.bot, OWNER) if "рейд" in c.args[1]]) == 1, "one alert per cooldown"


async def test_several_people_in_one_message_count_each(env):
    adapter, _ = env.build(answer())
    await adapter.handle_comment(service("new_chat_members", joined=botmod.RAID_JOINS))
    assert any("рейд" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_slow_joins_are_not_a_raid(env):
    adapter, _ = env.build(answer())
    from collections import deque

    adapter._joins[CHAT] = deque([time.time() - botmod.RAID_WINDOW - 5] * (botmod.RAID_JOINS + 3))
    await adapter.handle_comment(service("new_chat_members"))
    assert sent_to(env.bot, OWNER) == []


async def test_raid_alerts_can_be_switched_off(env):
    await env.storage.set_setting(CHAT, "antiraid", 0)
    adapter, _ = env.build(answer())
    for i in range(botmod.RAID_JOINS + 2):
        await adapter.handle_comment(service("new_chat_members", i=i))
    assert sent_to(env.bot, OWNER) == []


def press(data, user_id=ADMIN_ID):
    c = MagicMock()
    c.data, c.from_user.id = data, user_id
    c.message.html_text, c.message.edit_text, c.answer = "🚨 рейд", AsyncMock(), AsyncMock()
    return c


async def test_freeze_button_deletes_everything_from_members_for_a_while(env):
    adapter, _ = env.build(answer())
    c = press(f"raid:on:{CHAT}")
    await adapter.on_raid_button(c)
    chat = await env.storage.get_chat(CHAT)
    assert chat["lockdown"] == 1 and abs(chat["lockdown_until"] - (time.time() + botmod.RAID_FREEZE_MINUTES * 60)) < 5
    assert "Заморозил" in c.message.edit_text.await_args.args[0]
    m = msg("Обычное сообщение участника", user_id=90)
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()


async def test_the_freeze_ends_by_itself(env):
    adapter, _ = env.build(answer())
    past = int(time.time()) - 5
    await env.storage.set_lockdown(CHAT, True, past)
    assert not (await adapter.config(CHAT)).lockdown, "an expired freeze no longer applies"
    m = msg("Обычное сообщение", user_id=91)
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    await adapter._end_freeze(CHAT, past)
    assert (await env.storage.get_chat(CHAT))["lockdown"] == 0
    assert "Заморозка" in sent_to(env.bot, OWNER)[-1].args[1] and "снята" in sent_to(env.bot, OWNER)[-1].args[1]


async def test_an_old_timer_does_not_end_a_newer_freeze(env):
    adapter, _ = env.build(answer())
    now = int(time.time())
    await env.storage.set_lockdown(CHAT, True, now + 1000)
    await adapter._end_freeze(CHAT, now - 10)
    assert (await env.storage.get_chat(CHAT))["lockdown"] == 1


async def test_only_admins_can_freeze_and_declining_changes_nothing(env):
    adapter, _ = env.build(answer())
    stranger = press(f"raid:on:{CHAT}", user_id=999)
    await adapter.on_raid_button(stranger)
    assert (await env.storage.get_chat(CHAT))["lockdown"] == 0
    assert "только админы" in stranger.answer.await_args.args[0]
    await adapter.on_raid_button(press(f"raid:no:{CHAT}"))
    assert (await env.storage.get_chat(CHAT))["lockdown"] == 0


async def test_manual_lockdown_has_no_end_time(env):
    adapter, _ = env.build(answer())
    await env.storage.set_lockdown(CHAT, True, int(time.time()) + 100)
    adapter.is_admin_message = AsyncMock(return_value=True)
    m = msg("/lockdown on")
    m.reply = AsyncMock()
    await adapter.on_lockdown(m, CommandObject(prefix="/", command="lockdown", args="on"))
    chat = await env.storage.get_chat(CHAT)
    assert chat["lockdown"] == 1 and chat["lockdown_until"] is None
    await adapter.on_lockdown(m, CommandObject(prefix="/", command="lockdown", args="off"))
    assert (await env.storage.get_chat(CHAT))["lockdown"] == 0


def test_config_lockdown_rules():
    def row(lock, until):
        return {"rules_json": None, "rules": "", "mode": "normal", "lockdown": lock, "escalation": 1, "lockdown_until": until}

    now = time.time()
    assert [ChatConfig.from_row(row(*a)).lockdown for a in [(0, None), (1, None), (1, now + 100), (1, now - 100), (0, now + 100)]] == [
        False, True, True, False, False]


# ------------------------------------------------------------------------------ panel


async def test_panel_switches_the_new_settings_and_lockdown_has_no_timer(client):  # noqa: F811
    await client.storage.set_lockdown(WEB_CHAT, True, int(time.time()) + 100)
    resp = await client.put(
        f"/api/chat/{WEB_CHAT}/settings",
        json={"clean_service": True, "antiraid": False, "lockdown": True, "first_strict": False, "image_ocr": False},
        headers=h(),
    )
    assert resp.status == 200
    s = (await (await client.get(f"/api/chat/{WEB_CHAT}", headers=h())).json())["settings"]
    assert (s["clean_service"], s["antiraid"], s["lockdown"], s["first_strict"], s["image_ocr"]) == (True, False, True, False, False)
    assert (await client.storage.get_chat(WEB_CHAT))["lockdown_until"] is None
