import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.telegram.bot import TelegramAdapter, describe_rules
from core.rules import EVERYTHING, PERMISSION, PROHIBITION, Rule
from core.storage import Storage


@pytest.fixture
async def adapter(tmp_path):
    storage = Storage(str(tmp_path / "t.db"))
    await storage.open()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Мой канал"))
    yield TelegramAdapter(bot, MagicMock(), storage)
    await storage.close()


async def log(storage, action, reason="", text="t"):
    await storage.add_log(
        chat_id=-1001, message_id=5, user_id=1, user_name="Вася", text=text,
        category="x", confidence=0.9, bot_probability=0.1, action=action, reason=reason, tokens=1,
    )


async def test_digest_is_none_without_activity(adapter):
    assert await adapter.build_digest(-1001, int(time.time()) - 60) is None


async def test_digest_text(adapter):
    await log(adapter.storage, "delete_silent", "спам или реклама")
    await log(adapter.storage, "forward_useful", text="Как считать налог?")
    text = await adapter.build_digest(-1001, int(time.time()) - 60)
    assert "Мой канал" in text and "Проверено комментариев: 2" in text
    assert "спам или реклама — 1" in text and "Как считать налог?" in text


async def test_digest_sent_once_per_day_to_owner(adapter):
    await adapter.storage.set_pending_rules(-1001, "x", "[]")
    await adapter.storage.confirm_rules(-1001, owner_id=42)
    await log(adapter.storage, "delete_silent", "спам или реклама")
    now = datetime.now(timezone.utc)
    await adapter.send_digests(now)
    await adapter.send_digests(now)
    adapter.bot.send_message.assert_awaited_once()
    assert adapter.bot.send_message.await_args.args[0] == 42


async def test_digest_skipped_when_disabled(adapter):
    await adapter.storage.set_pending_rules(-1001, "x", "[]")
    await adapter.storage.confirm_rules(-1001, owner_id=42)
    await adapter.storage.set_setting(-1001, "digest", 0)
    await log(adapter.storage, "delete_silent", "спам")
    await adapter.send_digests()
    adapter.bot.send_message.assert_not_awaited()


def test_describe_rules():
    text = describe_rules([
        Rule("Мат можно", PERMISSION),
        Rule("Рекламу нельзя — бан", PROHIBITION, "ban"),
        Rule("Политику нельзя", PROHIBITION, None),
        Rule("Удаляй всё", EVERYTHING, None),
    ])
    assert "✅ Мат можно" in text
    assert "❌ Рекламу нельзя — бан — удалять и банить" in text
    assert "автоматически" in text and "спрошу вас" in text
    assert "все комментарии" in text
