"""Tests for the ``/api/pypistats`` proxy endpoint.

Responses are memoized per path (PyPI Stats updates once a day), so repeated
browser requests do not reach the rate-limited upstream.
"""

import httpx
import pytest

from magpie.clients.http import HttpClient
from magpie.errors import RemoteError

URL = "https://pypistats.org/api/packages/sqllineage/recent"


def _ok(payload: bytes = b'{"package": "sqllineage"}'):
    return httpx.Response(200, content=payload)


def test_caches_upstream_response(client, monkeypatch):
    calls = []

    async def fake_get(self, url, headers=None):
        calls.append(url)
        return _ok()

    monkeypatch.setattr(HttpClient, "get", fake_get)

    first = client.get("/api/pypistats/api/packages/sqllineage/recent")
    second = client.get("/api/pypistats/api/packages/sqllineage/recent")

    assert calls == [URL]  # the second request was served from cache
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"package": "sqllineage"}


def test_cache_key_is_the_path(client, monkeypatch):
    calls = []

    async def fake_get(self, url, headers=None):
        calls.append(url)
        return _ok()

    monkeypatch.setattr(HttpClient, "get", fake_get)

    # Query strings are neither part of the key nor forwarded: the client never
    # sends them, so the path alone identifies the response.
    client.get("/api/pypistats/api/packages/sqllineage/recent")
    client.get("/api/pypistats/api/packages/sqllineage/recent?period=month")
    client.get("/api/pypistats/api/packages/sqllineage/overall")

    assert calls == [
        "https://pypistats.org/api/packages/sqllineage/recent",
        "https://pypistats.org/api/packages/sqllineage/overall",
    ]


def test_returns_503_when_upstream_fails(client, monkeypatch):
    async def rate_limited(self, url, headers=None):
        raise RemoteError("upstream returned HTTP 429")

    monkeypatch.setattr(HttpClient, "get", rate_limited)

    response = client.get("/api/pypistats/api/packages/sqllineage/recent")

    assert response.status_code == 503
    assert "429" in response.json()["detail"]


def test_failure_is_not_cached(client, monkeypatch):
    """alru_cache only skips caching on exceptions, so the 503 must be raised."""
    fail_once = {"recent": True}

    async def flaky(self, url, headers=None):
        if fail_once.pop("recent", False):
            raise RemoteError("upstream returned HTTP 429")
        return _ok(b'{"data": {"last_day": 42}}')

    monkeypatch.setattr(HttpClient, "get", flaky)

    first = client.get("/api/pypistats/api/packages/sqllineage/recent")
    second = client.get("/api/pypistats/api/packages/sqllineage/recent")

    assert first.status_code == 503
    assert second.status_code == 200  # the failure was never stored
    assert second.json() == {"data": {"last_day": 42}}


@pytest.mark.parametrize(
    ("status", "body"),
    [(404, b"404"), (429, b'<a href="/api/#etiquette">429 RATE LIMIT EXCEEDED</a>')],
)
def test_error_response_is_neither_forwarded_nor_cached(
    client, monkeypatch, status, body
):
    """pypistats answers errors with plain text; forwarding one would cache it."""
    responses = [
        httpx.Response(status, content=body),
        _ok(b'{"data": {"last_day": 42}}'),
    ]

    async def fake_get(self, url, headers=None):
        return responses.pop(0)

    monkeypatch.setattr(HttpClient, "get", fake_get)

    first = client.get("/api/pypistats/api/packages/sqllineage/recent")
    second = client.get("/api/pypistats/api/packages/sqllineage/recent")

    assert first.status_code == 503
    assert str(status) in first.json()["detail"]
    assert second.status_code == 200  # the error was never stored
    assert second.json() == {"data": {"last_day": 42}}
