import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.telegram import bot as botmod
from adapters.telegram.bot import TelegramAdapter
from core.moderation import ChatConfig
from core.storage import Storage


@pytest.fixture
async def adapter(tmp_path):
    storage = Storage(str(tmp_path / "c.db"))
    await storage.open()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    moderator = MagicMock()
    moderator.tension = AsyncMock(return_value=(2.0, 1.0))
    yield TelegramAdapter(bot, moderator, storage)
    await storage.close()


def msg(i, thread=10):
    m = MagicMock()
    m.chat.id = -1001
    m.chat.username = None
    m.message_id = i
    m.message_thread_id = thread
    m.reply_to_message = None
    m.from_user.full_name = f"Участник {i % 3}"
    return m


CHAT = {"owner_id": 42}


async def feed(adapter, n, config=None, chat=CHAT, thread=10):
    for i in range(n):
        await adapter.watch_thread(msg(i, thread), f"сообщение {i}", config or ChatConfig(), chat)


async def test_alert_sent_to_owner_when_busy_thread_is_heated(adapter):
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES)
    adapter.moderator.tension.assert_awaited_once()
    adapter.bot.send_message.assert_awaited_once()
    assert adapter.bot.send_message.await_args.args[0] == 42
    assert "Разгорается конфликт" in adapter.bot.send_message.await_args.args[1]


async def test_quiet_thread_is_not_scored(adapter):
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES - 1)
    adapter.moderator.tension.assert_not_awaited()


async def test_calm_thread_does_not_alert(adapter):
    adapter.moderator.tension.return_value = (0.2, 1.0)
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES)
    adapter.moderator.tension.assert_awaited_once()
    adapter.bot.send_message.assert_not_awaited()


async def test_low_confidence_does_not_alert(adapter):
    adapter.moderator.tension.return_value = (2.0, 0.2)
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES)
    adapter.bot.send_message.assert_not_awaited()


async def test_same_thread_alerts_once_per_cooldown(adapter):
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES + 10)
    adapter.bot.send_message.assert_awaited_once()
    # after the recheck delay the thread is scored again, but the cooldown suppresses a second alert
    adapter._checked.clear()
    await feed(adapter, 2)
    adapter.bot.send_message.assert_awaited_once()


async def test_other_thread_is_independent(adapter):
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES, thread=10)
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES, thread=11)
    assert adapter.bot.send_message.await_count == 2


async def test_disabled_or_no_owner_means_no_scoring(adapter):
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES, config=ChatConfig(conflicts=False))
    await feed(adapter, botmod.CONFLICT_MIN_MESSAGES, chat={"owner_id": None}, thread=11)
    adapter.moderator.tension.assert_not_awaited()


async def test_old_messages_do_not_count(adapter):
    for i in range(botmod.CONFLICT_MIN_MESSAGES):
        adapter._threads.setdefault((-1001, 10), __import__("collections").deque(maxlen=30)).append(
            (time.time() - botmod.CONFLICT_WINDOW - 60, "Старый", f"старое {i}")
        )
    await feed(adapter, 1)
    adapter.moderator.tension.assert_not_awaited()
