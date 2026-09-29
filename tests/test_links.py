from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from core.links import Link, extract_links, matches, normalize_entry, outside
from core.models import ActionType, Author, Comment
from core.moderation import ChatConfig, Moderator, build_state
from tests.test_handlers import CHAT, answer, env, msg  # noqa: F401
from tests.test_webapp import CHAT as WEB_CHAT, client, h  # noqa: F401

# ------------------------------------------------------------------ finding and matching


@pytest.mark.parametrize(
    "text, expected",
    [
        ("смотри https://www.Example.com/path?x=1, и t.me/mychan!", ["example.com/path", "t.me/mychan"]),
        ("заходи на casino-x.top/bonus или bit.ly/abc", ["casino-x.top/bonus", "bit.ly/abc"]),
        ("пишите a@b.com и в t.me/+AbCdEf", ["t.me/+AbCdEf"]),  # an e-mail address is not a link
        ("т.е. это про 3.5 млн и v1.2.3, файл file.zip", []),  # abbreviations and versions are not links
        ("Мой сайт: mysite.ru.", ["mysite.ru"]),
    ],
)
def test_extract_links(text, expected):
    assert [str(link) for link in extract_links(text)] == expected


def test_hidden_link_descriptions_are_read():
    assert [str(x) for x in extract_links("Смотри здесь", ["«здесь» → https://scam.example/x"])] == ["scam.example/x"]


@pytest.mark.parametrize(
    "value, entry",
    [("https://www.Example.com/", "example.com"), ("Example.COM", "example.com"), ("t.me/mychan/123", "t.me/mychan"),
     ("@mychan", "t.me/mychan"), ("not a domain", None), ("foo", None), ("http://", None), ("a/b/c.com", None)],
)
def test_normalize_entry(value, entry):
    assert normalize_entry(value) == entry


def test_matching_rules():
    assert matches(Link("sub.example.com", "/x"), "example.com")
    assert not matches(Link("notexample.com"), "example.com")
    assert matches(Link("t.me", "/mychan/5"), "t.me/mychan")
    assert not matches(Link("t.me", "/other"), "t.me/mychan")
    assert matches(Link("t.me", "/anything"), "t.me")
    assert outside([Link("t.me", "/mychan"), Link("evil.ru")], ["t.me/mychan"]) == [Link("evil.ru")]


# --------------------------------------------------------------------------- decisions


class NoJev:
    def __init__(self):
        self.calls = 0

    async def ask(self, *args):
        self.calls += 1
        return answer()


def comment(text, previous=10, deletions=0):
    return Comment(1, 1, text, Author(1, "Вася", previous_messages=previous, previous_deletions=deletions))


async def decide(config, text, **kwargs):
    jev = NoJev()
    return await Moderator(jev).check(comment(text, **kwargs), config), jev


async def test_blocked_domain_is_deleted_without_asking_jev():
    d, jev = await decide(ChatConfig(blocked_domains=["scam.ru"]), "заходи scam.ru/bonus")
    assert d.action == ActionType.DELETE_SILENT and "чёрного списка" in d.reason and jev.calls == 0


async def test_block_mode_allows_only_listed_links():
    cfg = ChatConfig(links_mode="block", allowed_domains=["t.me/mychan"])
    d, _ = await decide(cfg, "смотри t.me/other")
    assert d.action == ActionType.DELETE_AND_EXPLAIN and "только разрешённые" in d.reason
    d, jev = await decide(cfg, "смотри t.me/mychan")
    assert d.action == ActionType.NONE and jev.calls == 1, "an allowed link goes on to the normal AI check"


async def test_newcomers_mode_only_stops_newcomers():
    cfg = ChatConfig(links_mode="newcomers")
    d, _ = await decide(cfg, "смотри example.com", previous=1)
    assert d.action == ActionType.DELETE_AND_EXPLAIN and "новичкам" in d.reason
    d, jev = await decide(cfg, "смотри example.com", previous=5)
    assert jev.calls == 1


async def test_repeat_offenders_escalate_even_with_lists():
    d, _ = await decide(ChatConfig(links_mode="block"), "смотри example.com", deletions=2)
    assert d.action == ActionType.DELETE_AND_MUTE and d.reason.endswith("(повторные нарушения)")


async def test_default_mode_leaves_links_to_the_ai():
    d, jev = await decide(ChatConfig(), "смотри example.com")
    assert jev.calls == 1


async def test_hidden_links_are_checked_too():
    c = Comment(1, 1, "Смотри здесь", Author(1, "В", previous_messages=10), links=["«здесь» → https://scam.ru/x"])
    d = await Moderator(NoJev()).check(c, ChatConfig(blocked_domains=["scam.ru"]))
    assert d.action == ActionType.DELETE_SILENT


def test_allowed_domains_are_shown_to_jev_as_the_owner_own():
    state = build_state(comment("привет"), [], ["t.me/mychan"])
    assert state["links_this_chat_allows_and_owns"] == ["t.me/mychan"]
    assert "links_this_chat_allows_and_owns" not in build_state(comment("привет"), [])


# ------------------------------------------------------------------------------ commands


def command(name, args=None):
    return CommandObject(prefix="/", command=name, args=args)


async def run(env, name, args=None):
    adapter, _ = env.build()
    adapter.is_admin_message = AsyncMock(return_value=True)
    m = msg(f"/{name}")
    m.reply = AsyncMock()
    handler = adapter.on_links if name == "links" else adapter.on_link_lists
    await handler(m, command(name, args))
    return adapter, m.reply.await_args.args[0]


async def test_links_command_shows_and_sets_the_mode(env):
    adapter, text = await run(env, "links")
    assert "решает ИИ" in text and "/allowlink" in text
    _, text = await run(env, "links", "block")
    assert "разрешены только ссылки из списка" in text
    assert (await adapter.config(CHAT)).links_mode == "block"
    _, text = await run(env, "links", "nonsense")
    assert "Изменить" in text


async def test_allow_block_and_unlink_commands(env):
    adapter, _ = await run(env, "allowlink", "https://www.Example.com/")
    _, text = await run(env, "blocklink", "scam.ru")
    config = await adapter.config(CHAT)
    assert config.allowed_domains == ["example.com"] and config.blocked_domains == ["scam.ru"]
    assert "example.com" in text and "scam.ru" in text
    await run(env, "allowlink", "scam.ru")  # moves it from blocked to allowed
    config = await adapter.config(CHAT)
    assert "scam.ru" in config.allowed_domains and config.blocked_domains == []
    await run(env, "unlink", "example.com")
    assert "example.com" not in (await adapter.config(CHAT)).allowed_domains


async def test_link_commands_reject_nonsense_and_non_admins(env):
    adapter, text = await run(env, "allowlink", "не адрес")
    assert "Напишите адрес" in text
    assert (await adapter.config(CHAT)).allowed_domains == []
    adapter, _ = env.build()
    adapter.is_admin_message = AsyncMock(return_value=False)
    m = msg("/allowlink example.com")
    m.reply = AsyncMock()
    await adapter.on_link_lists(m, command("allowlink", "example.com"))
    m.reply.assert_not_awaited()


async def test_a_blocked_link_is_removed_in_a_real_message(env):
    await env.storage.set_links(CHAT, "ai", [], ["scam.ru"])
    adapter, jev = env.build(answer())
    m = msg("Заходи на scam.ru/bonus", user_id=6)
    await adapter.handle_comment(m)
    m.delete.assert_awaited_once()
    assert jev.calls == []


# ---------------------------------------------------------------------------------- panel


async def test_panel_saves_and_returns_link_settings(client):  # noqa: F811
    resp = await client.put(
        f"/api/chat/{WEB_CHAT}/links",
        json={"mode": "block", "allowed": ["https://Example.com/", "@mychan"], "blocked": ["scam.ru", "scam.ru"]},
        headers=h(),
    )
    assert resp.status == 200
    links = (await (await client.get(f"/api/chat/{WEB_CHAT}", headers=h())).json())["links"]
    assert links == {"mode": "block", "allowed": ["example.com", "t.me/mychan"], "blocked": ["scam.ru"]}


async def test_panel_partial_update_keeps_the_rest(client):  # noqa: F811
    await client.put(f"/api/chat/{WEB_CHAT}/links", json={"allowed": ["example.com"], "blocked": ["scam.ru"]}, headers=h())
    await client.put(f"/api/chat/{WEB_CHAT}/links", json={"mode": "newcomers"}, headers=h())
    links = (await (await client.get(f"/api/chat/{WEB_CHAT}", headers=h())).json())["links"]
    assert links == {"mode": "newcomers", "allowed": ["example.com"], "blocked": ["scam.ru"]}


@pytest.mark.parametrize("bad", [{"mode": "evil"}, {"allowed": "example.com"}, {"allowed": ["не адрес"]}, {"blocked": [1]},
                                 {"allowed": ["a%d.com" % i for i in range(51)]}])
async def test_panel_rejects_bad_link_settings(client, bad):  # noqa: F811
    assert (await client.put(f"/api/chat/{WEB_CHAT}/links", json=bad, headers=h())).status == 400


async def test_panel_link_errors_name_the_problem(client):  # noqa: F811
    resp = await client.put(f"/api/chat/{WEB_CHAT}/links", json={"allowed": ["не адрес"]}, headers=h())
    assert "Разрешённые" in (await resp.json())["error"]
