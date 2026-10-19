"""The shared AI slots: chats take turns, a raid cannot starve the others, the line has an end."""

import asyncio
from unittest.mock import patch

import pytest

from core.fairness import FairLimiter, QueueFull
from core.jev_client import JevBusy, JevClient


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_slots_are_limited_and_given_back():
    limiter = FairLimiter(2)
    await limiter.acquire("a")
    await limiter.acquire("a")
    third = asyncio.create_task(limiter.acquire("a"))
    await settle()
    assert limiter.running == 2 and limiter.waiting == 1 and not third.done()
    limiter.release()
    await third
    assert limiter.running == 2 and limiter.waiting == 0
    limiter.release()
    limiter.release()
    assert limiter.running == 0


async def test_a_quiet_chat_does_not_wait_behind_a_raid():
    limiter = FairLimiter(1)
    served: list[str] = []
    await limiter.acquire("busy")  # the only slot is taken

    async def one(key):
        await limiter.acquire(key)
        served.append(key)
        limiter.release()

    raid = [asyncio.create_task(one("busy")) for _ in range(50)]
    await settle()
    quiet = asyncio.create_task(one("quiet"))
    await settle()
    limiter.release()  # the first request finishes
    await asyncio.gather(*raid, quiet)
    assert served.index("quiet") <= 1, "the quiet chat is served at the next turn, not after fifty requests"
    assert served.count("busy") == 50


async def test_keys_take_turns_in_rotation():
    limiter = FairLimiter(1)
    order: list[str] = []
    await limiter.acquire("x")

    async def one(key):
        await limiter.acquire(key)
        order.append(key)
        await asyncio.sleep(0)
        limiter.release()

    tasks = [asyncio.create_task(one(k)) for k in ("a", "a", "a", "b", "b", "c")]
    await settle()
    limiter.release()
    await asyncio.gather(*tasks)
    assert order[:3] == ["a", "b", "c"], order  # one of each before the second of any


async def test_a_line_that_is_too_long_is_refused_at_once():
    limiter = FairLimiter(1, max_queue=3)
    await limiter.acquire("raid")
    waiting = [asyncio.create_task(limiter.acquire("raid")) for _ in range(3)]
    await settle()
    with pytest.raises(QueueFull):
        await limiter.acquire("raid")
    other = asyncio.create_task(limiter.acquire("other"))  # another chat may still queue
    await settle()
    assert not other.done() and limiter.waiting == 4
    for task in (*waiting, other):
        task.cancel()
    await asyncio.gather(*waiting, other, return_exceptions=True)


async def test_waiting_too_long_gives_up_and_frees_the_place():
    limiter = FairLimiter(1, max_wait=0.05)
    await limiter.acquire("a")
    with pytest.raises(QueueFull):
        await limiter.acquire("b")
    assert limiter.waiting == 0
    limiter.release()
    await limiter.acquire("c")  # nothing is stuck behind the one that gave up
    assert limiter.running == 1


async def test_a_cancelled_request_leaves_no_trace():
    limiter = FairLimiter(1)
    await limiter.acquire("a")
    task = asyncio.create_task(limiter.acquire("b"))
    await settle()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert limiter.waiting == 0
    limiter.release()
    assert limiter.running == 0
    await limiter.acquire("c")


async def test_the_slot_context_gives_the_slot_back_on_errors():
    limiter = FairLimiter(1)
    with pytest.raises(RuntimeError):
        async with limiter.slot("a"):
            raise RuntimeError("boom")
    assert limiter.running == 0


# --------------------------------------------------------------- through the client


async def test_a_raid_in_one_chat_does_not_delay_the_others_at_the_client():
    client = JevClient("k", max_concurrent=2)
    done: list[int] = []
    gate = asyncio.Event()

    async def slow_ask(state, questions, images=None):
        await gate.wait()
        return {"answers": {}, "usage": {}}

    with patch.object(client, "_ask", slow_ask):
        raid = [asyncio.create_task(client.ask({}, {}, key=1)) for _ in range(30)]
        await settle()
        quiet = asyncio.create_task(client.ask({}, {}, key=2))
        await settle()
        assert client.queued == 29 or client.queued == 28
        gate.set()
        await quiet
        done.append(1)
        await asyncio.gather(*raid)
    assert done and client.limiter.running == 0


async def test_a_too_long_line_is_a_busy_signal_not_an_outage():
    client = JevClient("k", max_concurrent=1)
    client.limiter.max_queue = 2
    gate = asyncio.Event()

    async def slow_ask(state, questions, images=None):
        await gate.wait()
        return {"answers": {}, "usage": {}}

    with patch.object(client, "_ask", slow_ask):
        tasks = [asyncio.create_task(client.ask({}, {}, key=7)) for _ in range(3)]
        await settle()
        with pytest.raises(JevBusy):
            await client.ask({}, {}, key=7)
        assert client.healthy, "the breaker is not tripped by a busy line"
        gate.set()
        await asyncio.gather(*tasks)
