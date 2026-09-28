import sqlite3
import time

import pytest

from core.storage import Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "test.db"))
    await s.open()
    yield s
    await s.close()


async def test_pending_rules_become_active_only_after_confirmation(storage):
    await storage.set_pending_rules(1, "мат можно", "[]")
    assert (await storage.get_chat(1))["rules"] == ""
    assert await storage.confirm_rules(1, owner_id=42)
    chat = await storage.get_chat(1)
    assert chat["rules"] == "мат можно" and chat["rules_json"] == "[]" and chat["owner_id"] == 42
    assert chat["pending_rules"] is None
    assert not await storage.confirm_rules(1, owner_id=42)


async def test_settings_and_defaults(storage):
    await storage.ensure_chat(1)
    chat = await storage.get_chat(1)
    assert (chat["mode"], chat["lockdown"], chat["escalation"], chat["digest"], chat["useful_mode"]) == (
        "normal", 0, 1, 1, "digest",
    )
    await storage.set_setting(1, "mode", "strict")
    assert (await storage.get_chat(1))["mode"] == "strict"
    with pytest.raises(ValueError):
        await storage.set_setting(1, "owner_id", 5)


async def test_old_database_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, owner_id INTEGER, rules TEXT NOT NULL DEFAULT '', "
        "pending_rules TEXT, enabled INTEGER NOT NULL DEFAULT 1)"
    )
    con.execute("INSERT INTO chats (chat_id, rules) VALUES (7, 'без рекламы')")
    con.commit()
    con.close()
    s = Storage(path)
    await s.open()
    chat = await s.get_chat(7)
    assert chat["rules"] == "без рекламы" and chat["mode"] == "normal" and chat["escalation"] == 1
    await s.close()


async def test_message_counter_and_trust(storage):
    assert await storage.message_count(1, 5) == 0
    await storage.count_message(1, 5)
    await storage.count_message(1, 5)
    assert await storage.message_count(1, 5) == 2
    assert not await storage.is_trusted(1, 5)
    await storage.set_trusted(1, 5, True, "Вася")
    assert await storage.is_trusted(1, 5)
    await storage.set_trusted(1, 5, False)
    assert not await storage.is_trusted(1, 5)


async def test_posts(storage):
    await storage.add_post(1, 10, "Пост про биткоин")
    assert await storage.get_post(1, 10) == "Пост про биткоин"
    assert await storage.get_post(1, 11) is None


async def add(storage, action, reason="", text="t", confidence=0.9):
    await storage.add_log(
        chat_id=1, message_id=1, user_id=1, user_name="u", text=text,
        category="x", confidence=confidence, bot_probability=0.1, action=action, reason=reason, tokens=100,
    )


async def test_digest_queries(storage):
    for _ in range(3):
        await add(storage, "delete_silent", "спам или реклама")
    await add(storage, "delete_and_ban", "нарушено правило «Политика»")
    await add(storage, "forward_useful", "полезный комментарий", text="вопрос про налог", confidence=0.95)
    await add(storage, "none")
    since = int(time.time()) - 60
    assert (await storage.top_reasons(1, since))[0] == ("спам или реклама", 3)
    useful = await storage.useful_comments(1, since)
    assert [row["text"] for row in useful] == ["вопрос про налог"]
    stats = await storage.stats(1, since)
    assert stats["delete_silent"] == 3 and stats["tokens"] == 600
    assert await storage.top_reasons(1, int(time.time()) + 60) == []
