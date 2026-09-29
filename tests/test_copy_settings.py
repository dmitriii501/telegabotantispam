import json
from unittest.mock import AsyncMock

import pytest

from core.rules import Rule, rules_to_json
from tests.test_webapp import CHAT as DEST, client, h  # noqa: F401

SOURCE = -2002


async def prepare_source(client):
    storage = client.storage
    await storage.ensure_chat(SOURCE)
    await storage.save_rules(SOURCE, "мат можно. рекламу нельзя — бан", rules_to_json([Rule("Рекламу нельзя — бан", "prohibition", "ban")]), 77)
    await storage.set_setting(SOURCE, "mode", "strict")
    await storage.set_setting(SOURCE, "flood_messages", 9)
    await storage.set_setting(SOURCE, "captcha", 1)
    await storage.set_setting(SOURCE, "observe", 1)
    await storage.set_setting(SOURCE, "enabled", 0)
    await storage.set_links(SOURCE, "block", ["example.com"], ["scam.ru"])
    await storage.set_trusted(SOURCE, 5, True, "Вася")


async def test_rules_and_settings_are_copied_but_the_chat_keeps_its_identity(client):  # noqa: F811
    await prepare_source(client)
    await client.storage.save_rules(DEST, "старое", "[]", 42)
    resp = await client.post(f"/api/chat/{DEST}/copy", json={"from": SOURCE}, headers=h())
    assert resp.status == 200
    dest = await client.storage.get_chat(DEST)
    assert json.loads(dest["rules_json"])[0]["text"] == "Рекламу нельзя — бан"
    assert (dest["mode"], dest["flood_messages"], dest["captcha"], dest["links_mode"]) == ("strict", 9, 1, "block")
    assert json.loads(dest["allowed_domains"]) == ["example.com"] and json.loads(dest["blocked_domains"]) == ["scam.ru"]
    # what belongs to the destination chat stays
    assert dest["owner_id"] == 42 and dest["enabled"] == 1 and dest["observe"] == 0
    assert await client.storage.list_trusted(DEST) == [], "trusted people are not copied"
    # and the source is untouched
    assert (await client.storage.get_chat(SOURCE))["owner_id"] == 77


async def test_the_page_shows_the_copied_rules(client):  # noqa: F811
    await prepare_source(client)
    await client.post(f"/api/chat/{DEST}/copy", json={"from": SOURCE}, headers=h())
    data = await (await client.get(f"/api/chat/{DEST}", headers=h())).json()
    assert [r["text"] for r in data["rules"]] == ["Рекламу нельзя — бан"] and data["settings"]["mode"] == "strict"


async def test_one_must_administer_both_chats(client):  # noqa: F811
    await prepare_source(client)
    client.adapter.is_admin = AsyncMock(side_effect=lambda chat_id, user_id, fresh=False: user_id == 42 and chat_id != SOURCE)
    resp = await client.post(f"/api/chat/{DEST}/copy", json={"from": SOURCE}, headers=h())
    assert resp.status == 403
    assert (await client.storage.get_chat(DEST))["mode"] == "normal"


@pytest.mark.parametrize("body, status", [({"from": DEST}, 400), ({"from": "x"}, 400), ({"from": True}, 400), ({}, 400), ({"from": -9999}, 404)])
async def test_bad_requests(client, body, status):  # noqa: F811
    assert (await client.post(f"/api/chat/{DEST}/copy", json=body, headers=h())).status == status
