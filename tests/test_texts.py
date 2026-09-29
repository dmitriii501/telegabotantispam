"""What people read: no English or internal words leak into messages, owners always see which chat."""

import re
from unittest.mock import AsyncMock, MagicMock

from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandObject

from adapters.telegram import bot as botmod
from tests.test_handlers import CHAT, OWNER, SPAM, answer, env, msg, sent_to  # noqa: F401
from tests.test_webapp import CHAT as WEB_CHAT, client, h  # noqa: F401

INTERNAL = re.compile(r"rule_violation|delete_|send_to_review|forward_useful|_silent|none\b|Jev|jev|токен", re.I)


async def command_reply(env, name, args=None, **build):
    adapter, _ = env.build(*build.get("responses", ()))
    adapter.is_admin_message = AsyncMock(return_value=True)
    m = msg(f"/{name}")
    m.reply = AsyncMock()
    handler = getattr(adapter, f"on_{name}")
    if name in ("stats", "settings", "report"):
        await handler(m)
    else:
        await handler(m, CommandObject(prefix="/", command=name, args=args))
    return m.reply.await_args.args[0]


async def test_check_speaks_russian_and_shows_analytics(env):
    analytics = {
        "kind": {"type": "choice", "choice": "question", "confidence": 0.9},
        "lead": {"type": "noul", "noul": 0.9},
        "needs_answer": {"type": "noul", "noul": 0.9},
        "sentiment": {"type": "score", "score": 1.0, "confidence": 0.9},
    }
    text = await command_reply(env, "check", "Сколько стоит курс?", responses=[answer("useful", 0.9, **analytics)])
    assert "Оценка: полезный. Бот уверен на 90%" in text
    assert "Тип комментария: вопрос" in text and "хочет купить" in text and "ждёт ответа" in text
    assert not INTERNAL.search(text), text


async def test_check_of_a_violation_uses_russian_category(env):
    text = await command_reply(env, "check", "Реклама t.me/x", responses=[SPAM])
    assert "Оценка: спам" in text and "rule_violation" not in text


async def test_stats_shows_cost_not_tokens(env):
    adapter, _ = env.build(answer())
    await adapter.handle_comment(msg())
    adapter.is_admin_message = AsyncMock(return_value=True)
    m = msg("/stats")
    m.reply = AsyncMock()
    await adapter.on_stats(m)
    text = m.reply.await_args.args[0]
    assert "Расход ИИ: около $" in text and "токен" not in text.lower()


async def test_settings_has_no_internal_words(env):
    text = await command_reply(env, "settings")
    assert "Пробный режим" in text and "Удалять всё от участников" in text
    assert not INTERNAL.search(text), text


async def test_help_explains_modes_and_points_to_the_panel():
    for word in ("/panel", "пробном режиме", "/observe off", "soft чаще спрашивает вас", "/report"):
        assert word in botmod.HELP_TEXT, word


async def test_help_command_replies(env):
    adapter, _ = env.build()
    m = msg("/help")
    m.chat.type = "supergroup"
    m.answer = AsyncMock()
    await adapter.on_help(m)
    assert m.answer.await_args.args[0] == botmod.HELP_TEXT


async def test_command_menu_is_registered_for_private_chats_and_admins(env):
    adapter, _ = env.build()
    env.bot.set_my_commands = AsyncMock()
    await adapter.set_commands()
    assert env.bot.set_my_commands.await_count == 2
    scopes = {type(c.kwargs["scope"]).__name__: [x.command for x in c.args[0]] for c in env.bot.set_my_commands.await_args_list}
    assert scopes["BotCommandScopeAllPrivateChats"] == ["start", "panel"]
    assert "rules" in scopes["BotCommandScopeAllChatAdministrators"] and "help" in scopes["BotCommandScopeAllChatAdministrators"]


async def test_owner_messages_name_the_chat(env):
    adapter, _ = env.build(answer("normal", 0.55, probs={"normal": 0.55, "spam": 0.1, "rule_violation": 0.35, "useful": 0}))
    await adapter.handle_comment(msg("Ну ты и выдал, конечно"))
    review = [c.args[1] for c in sent_to(env.bot, OWNER)][-1]
    assert "в чате «Чат»" in review and "Бот уверен на 55%" in review

    chat = await env.storage.get_chat(CHAT)
    await adapter.notify_owner(chat, "test", "⚠️ Что-то случилось", 0)
    assert sent_to(env.bot, OWNER)[-1].args[1].endswith("Чат: «Чат»")


async def test_not_admin_bot_is_told_exactly_what_to_enable(env):
    adapter, _ = env.build()
    env.bot.id = 1
    env.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=ChatMemberStatus.MEMBER))
    event = MagicMock()
    event.chat.id, event.chat.type = -100999, "supergroup"
    event.new_chat_member.status = ChatMemberStatus.MEMBER
    await adapter.on_bot_added(event)
    text = env.bot.send_message.await_args.args[1]
    assert "сделайте меня админом" in text and "«Удалять сообщения»" in text and "я не админ" not in text


async def test_panel_range_error_uses_a_human_name(client):  # noqa: F811
    resp = await client.put(f"/api/chat/{WEB_CHAT}/settings", json={"flood_messages": 99}, headers=h())
    assert resp.status == 400
    assert "Сообщений подряд" in (await resp.json())["error"]
