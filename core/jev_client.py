"""Thin async client for TypeSafe's System One API (model Jev).

Docs: https://docs.typesafe.ai/api
"""

import asyncio
import logging
import time

import aiohttp

from core.fairness import FairLimiter, QueueFull
from core.metrics import JEV_QUEUE, JEV_REQUESTS, JEV_RUNNING, JEV_SECONDS, JEV_WAIT_SECONDS, METRICS

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

log = logging.getLogger(__name__)


class JevError(Exception):
    pass


class JevBusy(JevError):
    """Too many requests are waiting for the AI at the moment (a raid in a chat): not an outage."""


class JevClient:
    """Async client with a concurrency limit and a circuit breaker.

    After `breaker_threshold` failed requests in a row the client stops calling
    the API for `breaker_pause` seconds and fails at once, so a Jev outage does
    not pile up hundreds of slow, doomed requests.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 20.0,
        retries: int = 3,
        api_url: str = API_URL,
        max_concurrent: int = 20,
        breaker_threshold: int = 5,
        breaker_pause: float = 30.0,
    ):
        self._api_url = api_url
        self.limiter = FairLimiter(max_concurrent)
        METRICS.on_scrape(self._refresh_gauges)
        self._breaker_threshold = breaker_threshold
        self._breaker_pause = breaker_pause
        self._failures = 0
        self._open_until = 0.0
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = retries
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self._open_until

    def _refresh_gauges(self) -> None:
        JEV_QUEUE.set(self.limiter.waiting)
        JEV_RUNNING.set(self.limiter.running)

    @property
    def queued(self) -> int:
        return self.limiter.waiting

    async def ask(self, state, questions: dict, key=None) -> dict:
        """Send one request; return the raw response ({"answers": ..., "usage": ...}).
        `key` (the chat) is what the waiting line is shared by: a busy chat does not push the others back.
        JevBusy when the line is too long to wait in: the caller falls back to the local filter."""
        if not self.healthy:
            JEV_REQUESTS.inc(outcome="breaker_open")
            raise JevError("Jev temporarily disabled after repeated failures")
        queued_at = time.perf_counter()
        try:
            async with self.limiter.slot(key):
                started = time.perf_counter()
                JEV_WAIT_SECONDS.observe(started - queued_at)
                try:
                    result = await self._counted(state, questions)
                except JevError:
                    JEV_REQUESTS.inc(outcome="error")
                    raise
                JEV_SECONDS.observe(time.perf_counter() - started)
                JEV_REQUESTS.inc(outcome="ok")
                return result
        except QueueFull as e:
            JEV_REQUESTS.inc(outcome="busy")
            raise JevBusy(str(e)) from None

    async def _counted(self, state, questions: dict) -> dict:
        """One request, with the circuit breaker's count of failures in a row."""
        try:
            result = await self._ask(state, questions)
        except JevError:
            self._failures += 1
            if self._failures >= self._breaker_threshold:
                self._open_until = time.monotonic() + self._breaker_pause
                self._failures = 0
                log.error("Jev failed %d times in a row, pausing for %.0fs", self._breaker_threshold, self._breaker_pause)
            raise
        self._failures = 0
        return result

    async def _ask(self, state, questions: dict) -> dict:
        body = {"model": self._model, "state": state, "questions": questions}
        session = await self._get_session()
        delay = 1.0
        for attempt in range(1, self._retries + 1):
            try:
                async with session.post(self._api_url, json=body) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = float(resp.headers.get("retry-after", delay))
                        raise _Retryable(f"HTTP {resp.status}", retry_after)
                    if resp.status != 200:
                        raise JevError(f"HTTP {resp.status}: {await resp.text()}")
                    return await resp.json()
            except (_Retryable, aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                if attempt == self._retries:
                    raise JevError(f"Jev unavailable after {attempt} attempts: {e}") from e
                wait = e.retry_after if isinstance(e, _Retryable) else delay
                log.warning("Jev request failed (%s), retrying in %.1fs", e, wait)
                await asyncio.sleep(wait)
                delay *= 2
        raise JevError("unreachable")


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float):
        super().__init__(msg)
        self.retry_after = retry_after
