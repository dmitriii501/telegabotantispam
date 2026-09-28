"""End-to-end tests of the Telegram adapter with a fake Bot and a scripted Jev."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from adapters.telegram import bot as botmod
from adapters.telegram.bot import TelegramAdapter
from core.jev_client import JevError
from core.moderation import Moderator
from core.storage import Storage

CHAT = -100123
OWNER = 42
ADMIN_ID = 1
LINKED = -100777


def answer(category="normal", conf=0.95, bot=0.05, rule="none", probs=None, **extra):
    a = {
        "category": {"type": "choice", "choice": category, "confidence": conf},
        "rule": {"type": "choice", "choice": rule, "confidence": 1.0},
        "is_bot": {"type": "noul", "noul": bot},
    }
    if probs:
        a["category"]["probabilities"] = probs
    a.update(extra)
    return {"answers": a, "usage": {"input_tokens": 1000}}


SPAM = answer("spam", 0.99, 0.05, "r0")
SPAM_BOT = answer("spam", 0.99, 0.9, "r0")


class ScriptedJev:
    """Returns queued responses (or raises queued exceptions); repeats the last one."""

    def __init__(self, *responses):
        self.responses = list(responses) or [answer()]
        self.calls = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def msg(text="привет всем, как дела", user_id=5, message_id=100, sender_chat=None, entities=None, reply_to=None, thread=None):
    m = MagicMock()
    m.text, m.caption, m.entities, m.caption_entities = text, None, entities or [], None
    m.is_automatic_forward = False
    m.chat.id, m.chat.username = CHAT, None
    m.message_id, m.message_thread_id, m.reply_to_message = message_id, thread, reply_to
    m.sender_chat = sender_chat
    m.from_user = MagicMock(id=user_id, full_name="Вася Иванов", first_name="Вася", username="vasya", is_premium=False, is_bot=False)
    m.delete = AsyncMock()
    return m


@pytest.fixture
async def env(tmp_path):
    storage = Storage(str(tmp_path / "h.db"))
    await storage.open()
    await storage.ensure_chat(CHAT)
    await storage.save_rules(CHAT, "", "[]", OWNER)
    bot = MagicMock()
    for name in ("send_message", "ban_chat_member", "ban_chat_sender_chat", "restrict_chat_member", "delete_message", "unban_chat_member"):
        setattr(bot, name, AsyncMock())
    bot.send_message.return_value = MagicMock(message_id=555)
    bot.get_chat = AsyncMock(return_value=MagicMock(linked_chat_id=LINKED, title="Чат"))

    def build(*responses):
        jev = ScriptedJev(*responses)
        adapter = TelegramAdapter(bot, Moderator(jev), storage)
        adapter.is_admin = AsyncMock(side_effect=lambda chat_id, user_id, fresh=False: user_id == ADMIN_ID)
        return adapter, jev

    e = MagicMock()
    e.storage, e.bot, e.build = storage, bot, build
    yield e
    await storage.close()


async def last_log(storage):
    async with storage.db.execute("SELECT * FROM log ORDER BY id DESC LIMIT 1") as cur:
        return await cur.fetchone()


def sent_to(bot, chat_id):
    return [c for c in bot.send_message.await_args_list if c.args and c.args[0] == chat_id]


# ------------------------------------------------------------------ decisions carried out


async def test_spam_from_a_person_is_deleted_with_an_explanation(env):
    adapter, _ = env.build(SPAM)
    m = msg("Реклама моего канала t.me/x")
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()
    assert sent_to(env.bot, CHAT), "the deletion notice is posted in the chat"
    assert (await last_log(env.storage))["executed"] == 1


async def test_spam_bot_is_deleted_and_banned_and_copies_skip_jev(env):
    adapter, jev = env.build(SPAM_BOT)
    await adapter.handle_comment(msg("Заработок без вложений пиши мне", message_id=100))
    env.bot.ban_chat_member.assert_awaited_once_with(CHAT, 5)
    second = msg("заработок  без вложений — пиши мне!", user_id=6, message_id=101)
    await adapter.handle_comment(second)
    assert len(jev.calls) == 1, "the identical text is remembered and never sent to Jev again"
    second.delete.assert_awaited_once()


async def test_normal_comment_is_left_alone(env):
    adapter, _ = env.build(answer())
    m = msg()
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    assert (await last_log(env.storage))["action"] == "none"


# ------------------------------------------------------------------------- bypasses closed


async def test_edited_message_is_checked_again(env):
    adapter, jev = env.build(answer(), SPAM)
    original = msg("Всем привет, отличный пост", message_id=200)
    await adapter.handle_comment(original)
    original.delete.assert_not_awaited()
    edited = msg("Заработок без вложений, пиши в лс", message_id=200)
    await adapter.handle_comment(edited, edited=True)
    assert len(jev.calls) == 2
    edited.delete.assert_awaited_once()
    assert await env.storage.message_count(CHAT, 5) == 1, "an edit is not a new message"


async def test_comment_from_a_stranger_channel_is_moderated_and_the_channel_banned(env):
    adapter, _ = env.build(SPAM)
    channel = MagicMock(id=-100555, title="Спам-канал", username="spamch")
    m = msg("Подписывайтесь на наш канал", sender_chat=channel)
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()
    env.bot.ban_chat_sender_chat.assert_awaited_once_with(CHAT, -100555)
    env.bot.ban_chat_member.assert_not_awaited()


async def test_anonymous_admin_and_own_channel_are_never_moderated(env):
    adapter, jev = env.build(SPAM)
    await adapter.handle_comment(msg(sender_chat=MagicMock(id=CHAT, title="Чат")))
    await adapter.handle_comment(msg(sender_chat=MagicMock(id=LINKED, title="Наш канал")))
    assert jev.calls == []


async def test_hidden_link_is_passed_to_jev(env):
    adapter, jev = env.build(answer())
    hidden = MagicMock(type="text_link", url="https://scam.example/x", extract_from=lambda t: "здесь")
    await adapter.handle_comment(msg("Смотри здесь", entities=[hidden]))
    state, _ = jev.calls[0]
    assert state["links_in_the_message"] == ["«здесь» → https://scam.example/x"]


# ----------------------------------------------------------------------------- reliability


async def test_jev_outage_falls_back_to_local_filter_and_warns_the_owner_once(env):
    adapter, _ = env.build(JevError("down"))
    spam = msg("Казино с бонусом, пиши в лс", message_id=1)
    await adapter.handle_comment(spam)
    spam.delete.assert_awaited_once()
    fine = msg("Согласен с автором", message_id=2)
    await adapter.handle_comment(fine)
    fine.delete.assert_not_awaited()
    warnings = sent_to(env.bot, OWNER)
    assert len(warnings) == 1 and "недоступен" in warnings[0].args[1]


async def test_flood_is_muted_without_asking_jev(env):
    adapter, jev = env.build(answer())
    last = None
    for i in range(botmod.FLOOD_MESSAGES):
        last = msg(f"сообщение номер {i}", message_id=300 + i)
        await adapter.handle_comment(last)
    assert len(jev.calls) == botmod.FLOOD_MESSAGES - 1, "the flooding message itself is not analysed"
    last.delete.assert_awaited_once()
    env.bot.restrict_chat_member.assert_awaited_once()
    assert env.bot.restrict_chat_member.await_args.kwargs["until_date"].total_seconds() == 30 * 60


async def test_antiflood_can_be_switched_off(env):
    await env.storage.set_setting(CHAT, "antiflood", 0)
    adapter, jev = env.build(answer())
    for i in range(botmod.FLOOD_MESSAGES + 2):
        await adapter.handle_comment(msg(f"сообщение номер {i}", message_id=400 + i))
    env.bot.restrict_chat_member.assert_not_awaited()


async def test_failed_deletion_is_not_counted_and_the_owner_is_told(env):
    adapter, _ = env.build(SPAM)
    m = msg("Реклама t.me/x")
    m.delete.side_effect = TelegramBadRequest(method=MagicMock(), message="Bad Request: not enough rights to delete a message")
    await adapter.handle_comment(m)
    assert (await last_log(env.storage))["executed"] == 0
    assert await env.storage.recent_deletions(CHAT, 5) == 0
    assert any("прав" in c.args[1] for c in sent_to(env.bot, OWNER))


# ------------------------------------------------------------------------ observe mode


async def test_observe_mode_records_but_changes_nothing(env):
    await env.storage.set_setting(CHAT, "observe", 1)
    adapter, jev = env.build(SPAM_BOT)
    m = msg("Заработок без вложений пиши в лс")
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    env.bot.ban_chat_member.assert_not_awaited()
    entry = await last_log(env.storage)
    assert entry["action"] == "delete_and_ban" and entry["executed"] == 0
    # nothing is remembered as removed either, so the real run later judges it afresh
    assert await env.storage.example_kind(CHAT, "x") is None
    text = await adapter.build_digest(CHAT, 0, observe=True, period="за 7 дней")
    assert "режима наблюдения" in text and "Удалил бы: 1" in text


async def test_new_chats_start_in_observe_mode_existing_ones_do_not(env):
    adapter, _ = env.build()
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.ADMINISTRATOR, can_delete_messages=True, can_restrict_members=True))
    env.bot.id = 1
    def joined(chat_id):
        e = MagicMock()
        e.chat.id, e.chat.type = chat_id, "supergroup"
        e.new_chat_member.status = ChatMemberStatus.MEMBER
        return e
    await adapter.on_bot_added(joined(-100999))
    assert (await env.storage.get_chat(-100999))["observe"] == 1
    await adapter.on_bot_added(joined(CHAT))
    assert (await env.storage.get_chat(CHAT))["observe"] == 0


# ------------------------------------------------------------------- logic of decisions


async def test_borderline_normal_comment_goes_to_the_admin(env):
    probs = {"normal": 0.55, "useful": 0.0, "spam": 0.1, "rule_violation": 0.35}
    adapter, _ = env.build(answer("normal", 0.55, probs=probs))
    m = msg("Ну ты и выдал, конечно")
    await adapter.handle_comment(m)
    m.delete.assert_not_awaited()
    assert (await last_log(env.storage))["action"] == "send_to_review"
    assert any("возможное нарушение" in c.args[1] for c in sent_to(env.bot, OWNER))


async def test_repeat_offender_is_muted_only_for_real_recent_deletions(env):
    adapter, _ = env.build(SPAM)
    for i in range(2):
        await adapter.handle_comment(msg(f"Реклама номер {i} t.me/x", message_id=500 + i))
    third = msg("Реклама номер 3 t.me/x", message_id=503)
    await adapter.handle_comment(third)
    env.bot.restrict_chat_member.assert_awaited_once()  # the 3rd removed comment escalates to a mute


# --------------------------------------------------------------------------- analytics


async def test_analytics_are_stored_and_admin_reply_marks_the_comment_answered(env):
    analytics = {
        "kind": {"type": "choice", "choice": "question", "confidence": 0.9},
        "lead": {"type": "noul", "noul": 0.9},
        "needs_answer": {"type": "noul", "noul": 0.95},
        "sentiment": {"type": "score", "score": 1.0, "confidence": 0.9},
    }
    adapter, jev = env.build(answer("useful", 0.9, **analytics))
    await adapter.handle_comment(msg("Сколько стоит курс? Как записаться?", message_id=700))
    assert "kind" in jev.calls[0][1] and "lead" in jev.calls[0][1]
    entry = await last_log(env.storage)
    assert entry["kind"] == "question" and entry["lead"] == pytest.approx(0.9)
    waiting = await env.storage.unanswered(CHAT, 0)
    assert [r["message_id"] for r in waiting] == [700]

    reply = msg("Сейчас отвечу", user_id=ADMIN_ID, message_id=701, reply_to=MagicMock(message_id=700))
    await adapter.handle_comment(reply)
    assert await env.storage.unanswered(CHAT, 0) == []


async def test_analytics_can_be_switched_off(env):
    await env.storage.set_setting(CHAT, "analytics", 0)
    adapter, jev = env.build(answer())
    await adapter.handle_comment(msg())
    assert "kind" not in jev.calls[0][1]


async def test_trusted_authors_are_not_checked(env):
    await env.storage.set_trusted(CHAT, 5, True, "Вася")
    adapter, jev = env.build(SPAM)
    m = msg()
    await adapter.handle_comment(m)
    assert jev.calls == [] and m.delete.await_count == 0
