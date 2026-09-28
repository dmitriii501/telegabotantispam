import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.fallback import looks_like_spam
from core.jev_client import JevClient, JevError
from core.models import ActionType, Author, Category, Comment, Verdict
from core.moderation import (
    ANALYTICS_QUESTIONS,
    MODES,
    ChatConfig,
    Moderator,
    build_questions,
    build_state,
    decide,
    needs_check,
    parse_verdict,
)
from core.rules import BASE_RULE, Rule
from core.storage import Storage

# ------------------------------------------------------------------- local fallback filter


@pytest.mark.parametrize(
    "text, links, expected",
    [
        ("Казино с бонусом, пиши в лс", None, True),
        ("зaрaбoтoк без вложений t.me/x", None, True),  # look-alike letters are restored
        ("Слоты и ставки, смотри профиль", None, True),
        ("Хороший пост про казино в Монако", None, False),  # a spam word alone is not enough
        ("Пиши в лс, обсудим пост", None, False),  # a lead-away alone is not enough
        ("Казино", ["«тут» → https://x.example"], True),  # a hidden link counts
        ("Согласен, спасибо за разбор", None, False),
    ],
)
def test_fallback_filter(text, links, expected):
    assert looks_like_spam(text, links) is expected


def test_moderator_fallback_respects_lockdown_and_deletes_only_known_spam():
    m = Moderator(None)
    author = Author(1, "Вася")
    spam = Comment(1, 1, "Казино пиши в лс", author)
    fine = Comment(1, 2, "Согласен с автором", author)
    assert m.fallback(spam, ChatConfig()).action == ActionType.DELETE_SILENT
    assert m.fallback(fine, ChatConfig()).action == ActionType.NONE
    assert m.fallback(fine, ChatConfig(lockdown=True)).action == ActionType.DELETE_SILENT


# --------------------------------------------------------------------------- Jev client


async def serve(handler):
    app = web.Application()
    app.router.add_post("/", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_circuit_breaker_stops_calling_a_failing_api():
    hits = 0

    async def broken(request):
        nonlocal hits
        hits += 1
        return web.Response(status=500)

    server = await serve(broken)
    client = JevClient("k", api_url=str(server.make_url("/")), retries=1, breaker_threshold=2, breaker_pause=60)
    try:
        for _ in range(2):
            with pytest.raises(JevError):
                await client.ask("s", {})
        assert hits == 2 and not client.healthy
        with pytest.raises(JevError, match="temporarily disabled"):
            await client.ask("s", {})
        assert hits == 2, "no request is sent while the breaker is open"
    finally:
        await client.close()
        await server.close()


async def test_breaker_recovers_and_success_resets_failures():
    ok = False

    async def sometimes(request):
        return web.json_response({"answers": {}}) if ok else web.Response(status=500)

    server = await serve(sometimes)
    client = JevClient("k", api_url=str(server.make_url("/")), retries=1, breaker_threshold=3, breaker_pause=0.05)
    try:
        for _ in range(3):
            with pytest.raises(JevError):
                await client.ask("s", {})
        assert not client.healthy
        await asyncio.sleep(0.08)
        ok = True
        assert await client.ask("s", {}) == {"answers": {}}
        assert client.healthy
    finally:
        await client.close()
        await server.close()


async def test_concurrency_is_limited():
    running = peak = 0

    async def slow(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.03)
        running -= 1
        return web.json_response({"answers": {}})

    server = await serve(slow)
    client = JevClient("k", api_url=str(server.make_url("/")), max_concurrent=3)
    try:
        await asyncio.gather(*(client.ask("s", {}) for _ in range(12)))
        assert peak <= 3
    finally:
        await client.close()
        await server.close()


# ------------------------------------------------------------- probabilities and analytics


def response(choice="normal", conf=0.6, probs=None, **extra):
    a = {
        "category": {"type": "choice", "choice": choice, "confidence": conf, **({"probabilities": probs} if probs else {})},
        "rule": {"type": "choice", "choice": "none", "confidence": 1.0},
        "is_bot": {"type": "noul", "noul": 0.1},
    }
    a.update(extra)
    return {"answers": a, "usage": {"input_tokens": 900}}


def test_violation_probability_sums_spam_and_rule_violation():
    v = parse_verdict(response(probs={"normal": 0.5, "useful": 0.05, "spam": 0.15, "rule_violation": 0.3}))
    assert v.violation_probability == pytest.approx(0.45)


def test_violation_probability_without_probabilities():
    assert parse_verdict(response("spam", 0.9)).violation_probability == pytest.approx(0.9)
    assert parse_verdict(response("normal", 0.9)).violation_probability == 0.0


def test_parse_verdict_reads_analytics():
    v = parse_verdict(
        response(
            kind={"type": "choice", "choice": "complaint", "confidence": 0.9},
            lead={"type": "noul", "noul": 0.8},
            needs_answer={"type": "noul", "noul": 0.7},
            sentiment={"type": "score", "score": 0.2, "confidence": 0.9},
        )
    )
    assert (v.kind, v.lead, v.needs_answer, v.sentiment) == ("complaint", 0.8, 0.7, 0.2)


def test_borderline_normal_comment_is_reviewed_and_thresholds_follow_the_mode():
    borderline = Verdict(Category.NORMAL, 0.6, 0.1, None, violation_probability=0.35)
    rules = [BASE_RULE]
    assert decide(borderline, rules, MODES["normal"]).action == ActionType.NONE
    assert decide(borderline, rules, MODES["soft"]).action == ActionType.NONE
    strict = decide(borderline, rules, MODES["strict"])
    assert strict.action == ActionType.SEND_TO_REVIEW and strict.reason == "возможное нарушение"


def test_analytics_questions_are_optional():
    base = build_questions([BASE_RULE])
    full = build_questions([BASE_RULE], analytics=True)
    assert set(full) - set(base) == set(ANALYTICS_QUESTIONS)


def test_links_reach_the_state_and_count_for_needs_check():
    c = Comment(1, 1, "🔥", Author(1, "В"), links=["«тут» → https://x.example"])
    assert build_state(c, []) ["links_in_the_message"] == ["«тут» → https://x.example"]
    assert needs_check("🔥", c.links) and not needs_check("🔥")


# -------------------------------------------------------------------------------- storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "s.db"))
    await s.open()
    yield s
    await s.close()


async def log(storage, action="delete_silent", user_id=5, ts=None, executed=1, **extra):
    fields = dict(
        chat_id=1, message_id=1, user_id=user_id, user_name="Вася", text="текст комментария номер один",
        category="normal", confidence=0.9, bot_probability=0.1, action=action, reason="", tokens=1,
        executed=executed, **extra,
    )
    if ts is not None:
        fields["ts"] = ts
    return await storage.add_log(**fields)


async def test_recent_deletions_count_only_real_recent_uncontested_ones(storage):
    await log(storage)  # counts
    await log(storage, executed=0)  # Telegram refused: does not count
    await log(storage, ts=int(time.time()) - 40 * 24 * 3600)  # outside the 30-day window
    contested = await log(storage)
    await storage.set_feedback(contested, "not_spam")  # the admin restored it
    reviewed = await log(storage, action="send_to_review")
    await storage.set_feedback(reviewed, "confirmed")  # the admin confirmed the deletion
    await log(storage, action="send_to_review")  # still waiting: does not count
    await log(storage, user_id=6)  # another author
    assert await storage.recent_deletions(1, 5) == 2


async def test_unanswered_lists_open_questions_first_leads(storage):
    async def add(message_id, lead, needs, category="normal"):
        await storage.add_log(
            chat_id=1, message_id=message_id, user_id=5, user_name="Вася", text=f"текст {message_id}",
            category=category, confidence=0.9, bot_probability=0.1, action="none", reason="", tokens=1,
            kind="question", lead=lead, needs_answer=needs,
        )

    await add(10, 0.1, 0.9)
    await add(11, 0.95, 0.5)
    await add(12, 0.1, 0.1)  # neither: not listed
    await add(13, 0.9, 0.9, category="spam")  # spam is never a lead
    await add(14, 0.1, 0.9)
    await storage.mark_answered(1, 14)
    assert [r["message_id"] for r in await storage.unanswered(1, 0)] == [11, 10]
    counts = await storage.kind_counts(1, 0)
    assert counts == {"question": 4}, "spam rows are not counted as audience questions"


async def test_sentiment_summary(storage):
    for score in (0.0, 0.2, 1.0, 2.0, 1.9):
        await log(storage, action="none", sentiment=score)
    s = await storage.sentiment_summary(1, 0)
    assert (s["n"], s["negative"], s["positive"]) == (5, 2, 2)


async def test_purge_old_removes_only_expired_rows(storage):
    await log(storage, ts=int(time.time()) - 40 * 24 * 3600)
    fresh = await log(storage)
    assert await storage.purge_old(30) == 1
    assert (await storage.get_log(fresh)) is not None


async def test_wal_mode_and_migration_of_log_columns(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, chat_id INTEGER NOT NULL, "
                "message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, user_name TEXT, text TEXT NOT NULL, category TEXT, "
                "confidence REAL, bot_probability REAL, action TEXT NOT NULL, reason TEXT, tokens INTEGER NOT NULL DEFAULT 0, feedback TEXT)")
    con.execute("INSERT INTO log (ts, chat_id, message_id, user_id, text, action) VALUES (1, 1, 1, 1, 'старая запись', 'none')")
    con.commit()
    con.close()
    s = Storage(path)
    await s.open()
    row = await s.get_log(1)
    assert row["text"] == "старая запись" and row["executed"] == 1 and row["answered"] == 0
    async with s.db.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0] == "wal"
    await s.close()
