"""Tests for the ``/api/starhistory`` endpoint.

The real endpoint paginates GitHub's stargazers API and aggregates the results by
day. These tests stub ``httpx.AsyncClient.get`` so they run offline and
deterministically, verifying the endpoint's own logic (pagination, date-series
construction, and cumulative counts) rather than GitHub's API.

The series is built through "today", so every test that asserts a payload pins
that edge: otherwise the expectations would drift with the calendar.
"""

from datetime import date

import httpx

from magpie.services import starhistory


class _FakeResponse:
    """Minimal ``httpx.Response`` stand-in exposing what the code under test reads."""

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


def _pin_today(monkeypatch, day: str):
    """Freeze the series' right edge."""
    monkeypatch.setattr(starhistory, "_today", lambda: date.fromisoformat(day))


def test_starhistory_aggregates_stars_by_day(client, monkeypatch):
    _pin_today(monkeypatch, "2026-08-03")
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
    _pin_today(monkeypatch, "2026-08-01")
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
    _pin_today(monkeypatch, "2026-08-01")
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


def test_starhistory_keeps_days_without_stars(client, monkeypatch):
    """Days nobody starred are emitted with zero stars and the cumulative count
    carried forward, so the chart's x axis stays continuous. The timestamps are
    deliberately out of order: the range comes from min/max, not from the order
    GitHub returned the pages in."""
    _pin_today(monkeypatch, "2026-08-05")
    _stub_github(
        monkeypatch,
        [
            [
                {"starred_at": "2026-08-03T10:00:00Z"},
                {"starred_at": "2026-08-01T10:00:00Z"},
                {"starred_at": "2026-08-05T10:00:00Z"},
            ]
        ],
    )

    response = client.get("/api/starhistory/reata/sqllineage")

    assert response.json() == [
        {"date": "2026-08-01", "star_cnt": 1, "star_cum_cnt": 1},
        {"date": "2026-08-02", "star_cnt": 0, "star_cum_cnt": 1},
        {"date": "2026-08-03", "star_cnt": 1, "star_cum_cnt": 2},
        {"date": "2026-08-04", "star_cnt": 0, "star_cum_cnt": 2},
        {"date": "2026-08-05", "star_cnt": 1, "star_cum_cnt": 3},
    ]


def test_starhistory_extends_a_flat_tail_through_today(client, monkeypatch):
    """A repository that went quiet still shows a flat line up to today rather
    than a series that stops at its last star."""
    _pin_today(monkeypatch, "2026-08-04")
    _stub_github(monkeypatch, [[{"starred_at": "2026-08-01T10:00:00Z"}]])

    assert client.get("/api/starhistory/reata/sqllineage").json() == [
        {"date": "2026-08-01", "star_cnt": 1, "star_cum_cnt": 1},
        {"date": "2026-08-02", "star_cnt": 0, "star_cum_cnt": 1},
        {"date": "2026-08-03", "star_cnt": 0, "star_cum_cnt": 1},
        {"date": "2026-08-04", "star_cnt": 0, "star_cum_cnt": 1},
    ]


def test_starhistory_survives_a_star_ahead_of_our_clock(client, monkeypatch):
    """A star timestamped just after midnight UTC can be "tomorrow" for a clock
    a few seconds behind; the range must not collapse to nothing."""
    _pin_today(monkeypatch, "2026-08-04")
    _stub_github(monkeypatch, [[{"starred_at": "2026-08-05T00:00:01Z"}]])

    assert client.get("/api/starhistory/reata/sqllineage").json() == [
        {"date": "2026-08-05", "star_cnt": 1, "star_cum_cnt": 1}
    ]


def test_starhistory_maps_upstream_errors_to_503_without_caching(client, monkeypatch):
    _pin_today(monkeypatch, "2026-08-01")
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
