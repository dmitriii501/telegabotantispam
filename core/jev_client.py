"""Thin async client for TypeSafe's System One API (model Jev).

Docs: https://docs.typesafe.ai/api
"""

import asyncio
import logging
import time

import aiohttp

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

log = logging.getLogger(__name__)


class JevError(Exception):
    pass


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
        self._semaphore = asyncio.Semaphore(max_concurrent)
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

    async def ask(self, state, questions: dict) -> dict:
        """Send one request; return the raw response ({"answers": ..., "usage": ...})."""
        if not self.healthy:
            raise JevError("Jev temporarily disabled after repeated failures")
        async with self._semaphore:
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
