"""Tests for the ``/api/clickpy`` endpoints.

The queries run against a fake ClickHouse client: what these assert is the HTTP
side -- the response envelopes, what a missing package means, the cache, and the
startup warm-up.
"""

import asyncio
import datetime

from magpie.clients import clickhouse
from magpie.errors import RemoteError
from magpie.routers import downloads as downloads_router
from magpie.services import clickpy
from magpie.services.clickpy import SPECS

DAY = datetime.date(2026, 9, 11)


class FakeExecute:
    """Stand-in for ``clickhouse.execute`` that records its calls."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, query, parameters=None):
        self.calls.append((query, parameters))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _recent(**overrides):
    row = {"last_day": 1, "last_week": 2, "last_month": 3, "rows_seen": 30}
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# endpoint
# --------------------------------------------------------------------------- #


def test_serves_recent_in_pypistats_shape(client, monkeypatch):
    fake = FakeExecute([_recent()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/recent")

    assert response.status_code == 200
    assert response.json() == {
        "data": {"last_day": 1, "last_month": 3, "last_week": 2},
        "package": "sqllineage",
        "type": "recent_downloads",
    }


def test_normalizes_the_package_before_querying(client, monkeypatch):
    fake = FakeExecute([_recent()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    client.get("/api/clickpy/SQLAlchemy/recent")

    sql, parameters = fake.calls[0]
    assert sql is SPECS["recent"].sql
    assert parameters == {"package": "sqlalchemy"}


def test_serves_each_dimension(client, monkeypatch):
    rows = [{"date": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(
        rows, rows, [{"date": DAY, "with_mirrors": 5, "without_mirrors": 4}]
    )
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/pkg/python_minor").json()["type"] == (
        "python_minor_downloads"
    )
    assert client.get("/api/clickpy/pkg/system").json()["type"] == "system_downloads"
    assert client.get("/api/clickpy/pkg/overall").json()["type"] == "overall_downloads"


def test_response_is_cached_per_path(client, monkeypatch):
    """ClickPy is refreshed once a day, so one query per path per TTL is plenty."""
    fake = FakeExecute([_recent()], [_recent(last_day=99)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    first = client.get("/api/clickpy/sqllineage/recent")
    second = client.get("/api/clickpy/sqllineage/recent")

    assert len(fake.calls) == 1
    assert (
        first.json()
        == second.json()
        == {
            "data": {"last_day": 1, "last_month": 3, "last_week": 2},
            "package": "sqllineage",
            "type": "recent_downloads",
        }
    )


def test_unsupported_dimension_is_404_without_querying(client, monkeypatch):
    fake = FakeExecute()
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/python_major")

    assert response.status_code == 404
    assert fake.calls == []


def test_unknown_package_is_404(client, monkeypatch):
    fake = FakeExecute([_recent(last_day=0, last_week=0, last_month=0, rows_seen=0)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/nope/recent").status_code == 404


def test_failure_is_503_and_not_cached(client, monkeypatch):
    fake = FakeExecute(RemoteError("clickhouse query failed: boom"), [_recent()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    first = client.get("/api/clickpy/sqllineage/recent")
    second = client.get("/api/clickpy/sqllineage/recent")

    assert first.status_code == 503
    assert "boom" in first.json()["detail"]
    assert second.status_code == 200


# --------------------------------------------------------------------------- #
# startup warm-up
# --------------------------------------------------------------------------- #


def test_prewarm_queries_every_dimension(monkeypatch):
    calls = []

    # Keyword-only: the warm-up must key the cache exactly as the route does.
    async def fake_fetch(*, package, dimension):
        calls.append((package, dimension))

    monkeypatch.setattr(clickpy, "fetch", fake_fetch)

    asyncio.run(downloads_router.prewarm())

    assert calls == [
        (downloads_router.PREWARM_PACKAGE, dimension) for dimension in SPECS
    ]


def test_prewarm_survives_a_failing_dimension(monkeypatch):
    """A cold ClickHouse must not stop the app from starting, nor keep the
    remaining dimensions from being warmed."""
    calls = []

    async def flaky_fetch(*, package, dimension):
        calls.append(dimension)
        if dimension == "overall":
            raise RemoteError("clickhouse query failed: boom")

    monkeypatch.setattr(clickpy, "fetch", flaky_fetch)

    asyncio.run(downloads_router.prewarm())

    assert calls == list(SPECS)


def test_prewarm_is_disabled_for_tests(client):
    """The session TestClient runs the real lifespan; a live warm-up would race
    the per-test fakes."""
    assert downloads_router.PREWARM_ENABLED is False
