"""Shared HTTP client for upstream APIs, with retry and rate-limit handling.

Public APIs such as pypistats.org and GitHub rate limit by IP, so failed calls
are retried with jittered backoff and connections are pooled instead of opening
a fresh client per request.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

#: Status codes worth retrying. 429 is the application-wide rate limit that
#: pypistats.org returns once a caller exceeds its IP quota.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

MAX_ATTEMPTS = 3
BASE_DELAY = 0.5
#: Cap on any single backoff, including a server-provided ``Retry-After``, so a
#: client request never hangs for long.
MAX_DELAY = 5.0
MAX_CONCURRENCY = 4


class UpstreamError(RuntimeError):
    """Raised when an upstream API still fails after every retry."""


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into a capped number of seconds."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, MAX_DELAY))


def _backoff(attempt: int) -> float:
    """Full-jitter exponential backoff for ``attempt`` (0-based)."""
    return random.uniform(0, min(MAX_DELAY, BASE_DELAY * 2**attempt))


class UpstreamClient:
    """Async HTTP client that retries transient errors and rate limits."""

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
        """GET ``url``, retrying rate limits and transient failures.

        Retries are limited and jittered: hammering a rate-limited upstream only
        prolongs the ban. A non-retryable status (including 4xx such as 404) is
        returned as-is for the caller to forward.
        """
        last_error = UpstreamError(f"no attempt made for {url}")
        cause: BaseException | None = None
        client = self._ensure_client()
        for attempt in range(MAX_ATTEMPTS):
            retry_after: float | None = None
            try:
                async with self._semaphore:
                    response = await client.get(url, headers=headers)
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_error = UpstreamError(
                    f"upstream returned HTTP {response.status_code}"
                )
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            except httpx.TransportError as exc:
                last_error = UpstreamError(f"upstream request failed: {exc}")
                cause = exc

            if attempt + 1 == MAX_ATTEMPTS:
                break
            delay = retry_after if retry_after is not None else _backoff(attempt)
            logger.warning("%s; retrying %s in %.2fs", last_error, url, delay)
            await self._sleep(delay)

        if cause is not None:
            raise last_error from cause
        raise last_error
