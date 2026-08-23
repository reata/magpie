"""Tests for the ``/api/starhistory`` endpoint.

The real endpoint paginates GitHub's stargazers API and aggregates the results
with pandas. These tests stub ``httpx.AsyncClient.get`` so they run offline and
deterministically, verifying the endpoint's own logic (pagination, date-series
construction, and cumulative counts) rather than GitHub's API.
"""

import httpx
from fastapi.testclient import TestClient

from magpie.main import app

client = TestClient(app)


class _FakeResponse:
    """Minimal ``httpx.Response`` stand-in exposing only ``json()``."""

    def __init__(self, payload):
        self._payload = payload

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


def test_starhistory_aggregates_stars_by_day(monkeypatch):
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


def test_starhistory_stops_pagination_on_short_page(monkeypatch):
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
