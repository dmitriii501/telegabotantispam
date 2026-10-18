"""Metrics: the text format, the /metrics page and the numbers the bot really produces."""

import pytest

from core.metrics import COMMENT_SECONDS, COMMENTS, JEV_REQUESTS, METRICS, PAYMENTS, Counter, Gauge, Histogram, Registry
from tests.test_handlers import CHAT, SPAM, answer, msg


def test_counter_gauge_and_labels_in_the_text_format():
    r = Registry()
    c = r.counter("things_total", "Things", ("kind",))
    c.inc(kind="a")
    c.inc(2, kind="a")
    c.inc(kind='we"ird\nname')
    g = r.gauge("level", "Level")
    g.set(7.5)
    text = r.render()
    assert '# TYPE things_total counter' in text and 'things_total{kind="a"} 3' in text
    assert 'kind="we\\"ird\\nname"' in text
    assert "level 7.5" in text and "defenceai_uptime_seconds" in text
    assert text.endswith("\n")


def test_histogram_buckets_are_cumulative():
    h = Histogram("t_seconds", "T", buckets=(0.1, 1))
    for v in (0.05, 0.5, 5):
        h.observe(v)
    lines = h.lines()
    assert 't_seconds_bucket{le="0.1"} 1' in lines
    assert 't_seconds_bucket{le="1"} 2' in lines
    assert 't_seconds_bucket{le="+Inf"} 3' in lines
    assert "t_seconds_count 3" in lines and any(line.startswith("t_seconds_sum 5.55") for line in lines)


def test_wrong_labels_are_a_programming_error():
    c = Counter("c_total", "C", ("a",))
    with pytest.raises(ValueError):
        c.inc(b="x")
    with pytest.raises(ValueError):
        Gauge("g", "G", ("a",)).set(1)


def test_scrape_hooks_refresh_gauges():
    r = Registry()
    g = r.gauge("queue", "Queue")
    r.on_scrape(lambda: g.set(42))
    assert "queue 42" in r.render()


def test_snapshot_gives_plain_totals():
    r = Registry()
    r.counter("defenceai_x_total", "X", ("k",)).inc(3, k="a")
    assert r.snapshot() == {"x_total{a}": 3}


def total(counter, **labels):
    return counter.values.get(tuple(str(labels[n]) for n in counter.label_names), 0)


async def test_handled_comments_are_counted_by_action(env):
    adapter, _ = env.build(SPAM, answer())
    before_ban = total(COMMENTS, action="delete_and_explain") + total(COMMENTS, action="delete_and_ban")
    before_none = total(COMMENTS, action="none")
    before_time = sum(row[-1] for row in COMMENT_SECONDS.series.values())
    await adapter.handle_comment(msg("Реклама моего канала t.me/x", message_id=1))
    await adapter.handle_comment(msg("Расскажите про курс подробнее", message_id=2, user_id=6))
    assert total(COMMENTS, action="delete_and_explain") + total(COMMENTS, action="delete_and_ban") == before_ban + 1
    assert total(COMMENTS, action="none") == before_none + 1
    assert sum(row[-1] for row in COMMENT_SECONDS.series.values()) == before_time + 2


async def test_payments_are_counted_by_currency(env):
    adapter, _ = env.build()
    before = total(PAYMENTS, currency="RUB")
    await adapter.complete_payment("m-1", CHAT, 1, "start", 1, 790, "RUB")
    await adapter.complete_payment("m-1", CHAT, 1, "start", 1, 790, "RUB")  # the same notice twice
    assert total(PAYMENTS, currency="RUB") == before + 1


async def test_the_metrics_page_is_closed_without_the_token(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert (await client.get("/metrics")).status == 404
    monkeypatch.setenv("METRICS_TOKEN", "s3cret")
    assert (await client.get("/metrics")).status == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status == 401
    ok = await client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status == 200 and "text/plain" in ok.headers["Content-Type"]
    text = await ok.text()
    assert 'defenceai_chats{state="trial"} 1' in text and "defenceai_pending_reviews 0" in text
    assert "defenceai_db_bytes" in text and "defenceai_uptime_seconds" in text


async def test_the_ai_client_reports_outcomes_and_queue():
    from unittest.mock import patch

    from core.jev_client import JevClient

    client = JevClient("k")

    async def fine(state, questions):
        return {"answers": {}, "usage": {}}

    before = total(JEV_REQUESTS, outcome="ok")
    with patch.object(client, "_ask", fine):
        await client.ask({}, {}, key=1)
    assert total(JEV_REQUESTS, outcome="ok") == before + 1
    assert "defenceai_jev_queue 0" in METRICS.render()
