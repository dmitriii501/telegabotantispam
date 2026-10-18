"""A limiter that shares a fixed number of slots fairly between many users of them.

A plain semaphore serves requests in the order they come, so one chat in a raid (thousands of comments in a minute)
fills the line and every other chat waits behind it. Here the waiting requests of different chats take turns:
a quiet chat waits for at most one round, however long the line of the busy one is.
"""

import asyncio
import contextlib
from collections import deque
from collections.abc import Hashable


class QueueFull(Exception):
    """The line of this key is too long or the wait took too long: the caller should do without the shared resource."""


class FairLimiter:
    def __init__(self, slots: int, max_queue: int = 200, max_wait: float = 30.0):
        self.slots = slots
        self.max_queue = max_queue  # waiting requests one key may have
        self.max_wait = max_wait  # seconds a request may wait for its turn
        self.running = 0
        self._waiters: dict[Hashable, deque[asyncio.Future]] = {}
        self._turns: deque[Hashable] = deque()  # keys with someone waiting, in the order they are served

    @property
    def waiting(self) -> int:
        return sum(len(q) for q in self._waiters.values())

    async def acquire(self, key: Hashable) -> None:
        if self.running < self.slots and not self._turns:
            self.running += 1
            return
        queue = self._waiters.setdefault(key, deque())
        if len(queue) >= self.max_queue:
            raise QueueFull(f"{len(queue)} requests of {key!r} are already waiting")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        queue.append(future)
        if key not in self._turns:
            self._turns.append(key)
        try:
            await asyncio.wait_for(asyncio.shield(future), self.max_wait)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            if future.done() and not future.cancelled():
                self.release()  # the slot was handed over just as the wait ended: give it back
            else:
                future.cancel()
                self._forget(key, future)
            if isinstance(e, asyncio.TimeoutError):
                raise QueueFull(f"waited more than {self.max_wait:.0f}s for a slot") from None
            raise

    def release(self) -> None:
        self.running -= 1
        self._dispatch()

    def _forget(self, key: Hashable, future: asyncio.Future) -> None:
        queue = self._waiters.get(key)
        if queue is not None:
            with contextlib.suppress(ValueError):
                queue.remove(future)
            if not queue:
                del self._waiters[key]
                with contextlib.suppress(ValueError):
                    self._turns.remove(key)

    def _dispatch(self) -> None:
        while self.running < self.slots and self._turns:
            key = self._turns.popleft()
            queue = self._waiters.get(key)
            while queue:
                future = queue.popleft()
                if future.cancelled():
                    continue
                self.running += 1
                future.set_result(None)
                break
            if queue:
                self._turns.append(key)  # more of this key are waiting: the others go first
            else:
                self._waiters.pop(key, None)

    def slot(self, key: Hashable) -> "_Slot":
        """`async with limiter.slot(chat_id): ...`"""
        return _Slot(self, key)


class _Slot:
    def __init__(self, limiter: FairLimiter, key: Hashable):
        self._limiter = limiter
        self._key = key

    async def __aenter__(self) -> None:
        await self._limiter.acquire(self._key)

    async def __aexit__(self, *exc) -> None:
        self._limiter.release()
