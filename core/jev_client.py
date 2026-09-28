"""Thin async client for TypeSafe's System One API (model Jev).

Docs: https://docs.typesafe.ai/api
"""

import asyncio
import logging

import aiohttp

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

log = logging.getLogger(__name__)


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 20.0, retries: int = 3):
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

    async def ask(self, state, questions: dict) -> dict:
        """Send one request; return the raw response ({"answers": ..., "usage": ...})."""
        body = {"model": self._model, "state": state, "questions": questions}
        session = await self._get_session()
        delay = 1.0
        for attempt in range(1, self._retries + 1):
            try:
                async with session.post(API_URL, json=body) as resp:
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
