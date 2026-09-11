"""Tests for the ``/api/pypistats`` proxy endpoint.

Responses are memoized per path (PyPI Stats updates once a day), so repeated
browser requests do not reach the rate-limited upstream.
"""

import httpx

import magpie.main as main
from magpie.upstream import UpstreamError

URL = "https://pypistats.org/api/packages/sqllineage/recent"


def _ok(payload: bytes = b'{"package": "sqllineage"}'):
    return httpx.Response(200, content=payload)


def test_caches_upstream_response(client, monkeypatch):
    calls = []

    async def fake_get(self, url, headers=None):
        calls.append(url)
        return _ok()

    monkeypatch.setattr(main.UpstreamClient, "get", fake_get)

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

    monkeypatch.setattr(main.UpstreamClient, "get", fake_get)

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
        raise UpstreamError("upstream returned HTTP 429")

    monkeypatch.setattr(main.UpstreamClient, "get", rate_limited)

    response = client.get("/api/pypistats/api/packages/sqllineage/recent")

    assert response.status_code == 503
    assert "429" in response.json()["detail"]


def test_failure_is_not_cached(client, monkeypatch):
    """alru_cache only skips caching on exceptions, so the 503 must be raised."""
    fail_once = {"recent": True}

    async def flaky(self, url, headers=None):
        if fail_once.pop("recent", False):
            raise UpstreamError("upstream returned HTTP 429")
        return _ok(b'{"data": {"last_day": 42}}')

    monkeypatch.setattr(main.UpstreamClient, "get", flaky)

    first = client.get("/api/pypistats/api/packages/sqllineage/recent")
    second = client.get("/api/pypistats/api/packages/sqllineage/recent")

    assert first.status_code == 503
    assert second.status_code == 200  # the failure was never stored
    assert second.json() == {"data": {"last_day": 42}}
