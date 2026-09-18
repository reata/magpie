"""Unit tests for the shared HTTP client's retry behaviour."""

import asyncio

import httpx
import pytest

from magpie.clients.http import MAX_ATTEMPTS, HttpClient
from magpie.errors import RemoteError


def _run(monkeypatch, responses):
    """Drive ``HttpClient.get`` with stubbed responses.

    Returns ``(response_or_error, retry_delays, requested_urls)``.
    """
    remaining = iter(responses)
    delays: list[float] = []
    urls: list[str] = []

    async def fake_get(self, url, headers=None):
        urls.append(url)
        try:
            item = next(remaining)
        except StopIteration:
            raise AssertionError(f"unexpected extra request: {url}") from None
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async def main():
        client = HttpClient(sleep=fake_sleep)
        try:
            return await client.get("https://api.github.com/x")
        finally:
            await client.aclose()

    try:
        result = asyncio.run(main())
    except RemoteError as exc:
        result = exc
    return result, delays, urls


def test_returns_non_retryable_response_immediately(monkeypatch):
    response, delays, urls = _run(monkeypatch, [httpx.Response(404)])

    assert response.status_code == 404
    assert delays == []
    assert len(urls) == 1


def test_retries_server_error_then_succeeds(monkeypatch):
    response, delays, urls = _run(
        monkeypatch,
        [
            httpx.Response(503),
            httpx.Response(200, content=b'{"ok": true}'),
        ],
    )

    assert response.status_code == 200
    assert len(urls) == 2
    assert len(delays) == 1
    assert 0 <= delays[0] <= 0.5  # full-jitter backoff for attempt 0


def test_429_is_not_retried(monkeypatch):
    """A rate limit counts every attempt against it, so retrying a 429 cannot
    succeed -- it only spends more of the quota."""
    response, delays, urls = _run(monkeypatch, [httpx.Response(429)])

    assert response.status_code == 429
    assert len(urls) == 1
    assert delays == []


def test_gives_up_after_max_attempts(monkeypatch):
    response, delays, urls = _run(monkeypatch, [httpx.Response(500)] * MAX_ATTEMPTS)

    assert isinstance(response, RemoteError)
    assert "HTTP 500" in str(response)
    assert len(urls) == MAX_ATTEMPTS
    assert len(delays) == MAX_ATTEMPTS - 1
    assert delays[0] <= 0.5
    assert delays[1] <= 1.0


def test_retries_transport_errors(monkeypatch):
    response, _, urls = _run(
        monkeypatch,
        [
            httpx.ConnectError("connection refused"),
            httpx.Response(200),
        ],
    )

    assert response.status_code == 200
    assert len(urls) == 2


def test_transport_error_after_max_attempts_raises(monkeypatch):
    response, _, _ = _run(monkeypatch, [httpx.ConnectError("boom")] * MAX_ATTEMPTS)

    assert isinstance(response, RemoteError)


def test_cause_reflects_only_the_final_attempt(monkeypatch):
    """A stale transport error must not be chained onto a later HTTP failure."""
    response, _, _ = _run(
        monkeypatch,
        [
            httpx.ConnectError("connection refused"),
            httpx.Response(500),
            httpx.Response(500),
        ],
    )

    assert isinstance(response, RemoteError)
    assert "HTTP 500" in str(response)
    assert response.__cause__ is None


def test_unknown_type_error_is_not_swallowed(monkeypatch):
    async def fake_get(self, url, headers=None):
        raise ValueError("bug in client")

    async def fake_sleep(seconds: float) -> None:
        raise AssertionError("should not sleep")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async def main():
        client = HttpClient(sleep=fake_sleep)
        try:
            return await client.get("https://api.github.com/x")
        finally:
            await client.aclose()

    with pytest.raises(ValueError, match="bug in client"):
        asyncio.run(main())
