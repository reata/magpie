"""Tests for the ``/api/starhistory`` endpoint.

The real endpoint paginates GitHub's stargazers API and aggregates the results
with pandas. These tests stub ``httpx.AsyncClient.get`` so they run offline and
deterministically, verifying the endpoint's own logic (pagination, date-series
construction, and cumulative counts) rather than GitHub's API.
"""

import httpx


class _FakeResponse:
    """Minimal ``httpx.Response`` stand-in exposing only what the view reads."""

    headers: dict[str, str] = {}

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def _stub_github(monkeypatch, pages):
    """Make ``httpx.AsyncClient.get`` return ``pages`` in order, offline.

    Returns the list of URLs the endpoint requested, so tests can also assert
    the pagination parameters.
    """
    captured_urls = []
    remaining = iter(pages)

    async def fake_get(self, url, headers=None):
        captured_urls.append(url)
        try:
            payload = next(remaining)
        except StopIteration:
            raise AssertionError(f"unexpected extra request: {url}")
        return _FakeResponse(payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return captured_urls


def test_starhistory_aggregates_stars_by_day(client, monkeypatch):
    captured_urls = _stub_github(
        monkeypatch,
        [
            [
                {"starred_at": "2026-08-01T10:00:00Z"},
                {"starred_at": "2026-08-01T11:00:00Z"},
                {"starred_at": "2026-08-03T10:00:00Z"},
            ]
        ],
    )

    response = client.get("/api/starhistory/reata/sqllineage")

    assert response.status_code == 200
    assert captured_urls == [
        "https://api.github.com/repos/reata/sqllineage/stargazers?per_page=100&page=1"
    ]
    assert response.json() == [
        {"date": "2026-08-01", "star_cnt": 2, "star_cum_cnt": 2},
        {"date": "2026-08-02", "star_cnt": 0, "star_cum_cnt": 2},
        {"date": "2026-08-03", "star_cnt": 1, "star_cum_cnt": 3},
    ]


def test_starhistory_stops_pagination_on_short_page(client, monkeypatch):
    full_page = [{"starred_at": "2026-08-01T10:00:00Z"}] * 100
    captured_urls = _stub_github(monkeypatch, [full_page, []])

    response = client.get("/api/starhistory/reata/sqllineage")

    assert response.status_code == 200
    assert captured_urls == [
        "https://api.github.com/repos/reata/sqllineage/stargazers?per_page=100&page=1",
        "https://api.github.com/repos/reata/sqllineage/stargazers?per_page=100&page=2",
    ]
    assert response.json() == [
        {"date": "2026-08-01", "star_cnt": 100, "star_cum_cnt": 100},
    ]


def test_starhistory_reuses_cached_aggregation(client, monkeypatch):
    # One stubbed page only: a second upstream request would make _stub_github
    # raise, so passing proves the aggregation was served from cache.
    captured_urls = _stub_github(
        monkeypatch, [[{"starred_at": "2026-08-01T10:00:00Z"}]]
    )

    first = client.get("/api/starhistory/reata/sqllineage")
    second = client.get("/api/starhistory/reata/sqllineage")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(captured_urls) == 1


def test_starhistory_returns_empty_list_for_a_repo_without_stars(client, monkeypatch):
    _stub_github(monkeypatch, [[]])

    response = client.get("/api/starhistory/reata/brand-new")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == []


def test_starhistory_maps_upstream_errors_to_503_without_caching(client, monkeypatch):
    remaining = iter(
        [
            _FakeResponse({"message": "Not Found"}, status_code=404),
            _FakeResponse([{"starred_at": "2026-08-01T10:00:00Z"}]),
        ]
    )

    async def fake_get(self, url, headers=None):
        return next(remaining)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    first = client.get("/api/starhistory/reata/nope")
    second = client.get("/api/starhistory/reata/nope")

    assert first.status_code == 503
    assert "404" in first.json()["detail"]
    assert second.status_code == 200  # the error was never stored
    assert second.json() == [{"date": "2026-08-01", "star_cnt": 1, "star_cum_cnt": 1}]
