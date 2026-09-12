"""Shared HTTP client for upstream APIs.

One pooled ``httpx.AsyncClient`` is reused across requests, and transient server
errors -- 5xx and transport failures -- are retried with jittered backoff. Rate
limits are deliberately not retried; see ``RETRYABLE_STATUS``.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

#: Status codes worth retrying. 429 is deliberately absent: pypistats.org
#: allows "30 per minute" and counts every attempt against that quota, so a
#: sub-second retry cannot clear the window -- it only spends more of it.
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

MAX_ATTEMPTS = 3
BASE_DELAY = 0.5
MAX_CONCURRENCY = 4


class UpstreamError(RuntimeError):
    """Raised when an upstream call cannot be served to the client.

    Either the request kept failing after every retry, or the response carried a
    status the caller refuses to forward.
    """


def _backoff(attempt: int) -> float:
    """Full-jitter exponential backoff for ``attempt`` (0-based).

    With ``MAX_ATTEMPTS = 3`` the only sleeps are 0-0.5s and 0-1s.
    """
    return random.uniform(0, BASE_DELAY * 2**attempt)


class UpstreamClient:
    """Async HTTP client that pools connections and retries transient errors."""

    def __init__(
        self,
        *,
        concurrency: int = MAX_CONCURRENCY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        # Built on first use so importing the app never depends on the ambient
        # proxy environment (httpx parses ``NO_PROXY`` when a client is created).
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=True,
                headers={"Accept": "application/json"},
                limits=httpx.Limits(
                    max_connections=self._concurrency * 2,
                    max_keepalive_connections=self._concurrency,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET ``url``, retrying transient server errors.

        Retries are limited and jittered so a struggling upstream is not hammered
        further. Every other status -- 4xx and 429 included -- is returned as-is
        for the caller to decide what to forward.
        """
        last_error = UpstreamError(f"no attempt made for {url}")
        cause: BaseException | None = None
        client = self._ensure_client()
        for attempt in range(MAX_ATTEMPTS):
            cause = None  # only the final attempt's cause should be chained
            try:
                async with self._semaphore:
                    response = await client.get(url, headers=headers)
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_error = UpstreamError(
                    f"upstream returned HTTP {response.status_code}"
                )
            except httpx.TransportError as exc:
                last_error = UpstreamError(f"upstream request failed: {exc}")
                cause = exc

            if attempt + 1 == MAX_ATTEMPTS:
                break
            delay = _backoff(attempt)
            logger.warning("%s; retrying %s in %.2fs", last_error, url, delay)
            await self._sleep(delay)

        if cause is not None:
            raise last_error from cause
        raise last_error


#: The one client every outbound HTTP call shares: one connection pool for the
#: whole process, closed by the application lifespan.
upstream = UpstreamClient()
