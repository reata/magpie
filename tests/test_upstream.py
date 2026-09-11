"""Unit tests for the shared upstream HTTP client's retry behaviour."""

import asyncio

import httpx
import pytest

from magpie.upstream import MAX_ATTEMPTS, UpstreamClient, UpstreamError


def _run(monkeypatch, responses, *, headers=None):
    """Drive ``UpstreamClient.get`` with stubbed responses.

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
        client = UpstreamClient(sleep=fake_sleep)
        try:
            return await client.get("https://pypistats.org/api/x", headers=headers)
        finally:
            await client.aclose()

    try:
        result = asyncio.run(main())
    except UpstreamError as exc:
        result = exc
    return result, delays, urls


def test_returns_non_retryable_response_immediately(monkeypatch):
    response, delays, urls = _run(monkeypatch, [httpx.Response(404)])

    assert response.status_code == 404
    assert delays == []
    assert len(urls) == 1


def test_retries_rate_limit_then_succeeds(monkeypatch):
    response, delays, urls = _run(
        monkeypatch,
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, content=b'{"ok": true}'),
        ],
    )

    assert response.status_code == 200
    assert delays == [2.0]  # Retry-After is honoured over the jittered backoff
    assert len(urls) == 2


def test_retry_after_is_capped(monkeypatch):
    _, delays, _ = _run(
        monkeypatch,
        [
            httpx.Response(429, headers={"Retry-After": "600"}),
            httpx.Response(200),
        ],
    )

    assert len(delays) == 1
    assert delays[0] <= 5.0


def test_retry_after_accepts_http_date(monkeypatch):
    _, delays, _ = _run(
        monkeypatch,
        [
            httpx.Response(
                429,
                headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
            ),
            httpx.Response(200),
        ],
    )

    assert len(delays) == 1
    assert delays[0] <= 5.0


def test_gives_up_after_max_attempts(monkeypatch):
    response, delays, urls = _run(monkeypatch, [httpx.Response(429)] * MAX_ATTEMPTS)

    assert isinstance(response, UpstreamError)
    assert "HTTP 429" in str(response)
    assert len(urls) == MAX_ATTEMPTS
    assert len(delays) == MAX_ATTEMPTS - 1
    assert all(delay <= 5.0 for delay in delays)


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

    assert isinstance(response, UpstreamError)


def test_retry_after_with_invalid_value_falls_back_to_jitter(monkeypatch):
    _, delays, _ = _run(
        monkeypatch,
        [
            httpx.Response(429, headers={"Retry-After": "not-a-date"}),
            httpx.Response(200),
        ],
    )

    assert len(delays) == 1
    assert 0 <= delays[0] <= 5.0


def test_unknown_type_error_is_not_swallowed(monkeypatch):
    async def fake_get(self, url, headers=None):
        raise ValueError("bug in client")

    async def fake_sleep(seconds: float) -> None:
        raise AssertionError("should not sleep")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    async def main():
        client = UpstreamClient(sleep=fake_sleep)
        try:
            return await client.get("https://pypistats.org/api/x")
        finally:
            await client.aclose()

    with pytest.raises(ValueError, match="bug in client"):
        asyncio.run(main())
