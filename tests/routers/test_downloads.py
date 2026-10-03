"""Tests for the ``/api/clickpy`` endpoints.

The queries run against a fake ClickHouse client: what these assert is the HTTP side -- the response envelopes, what
a missing package means, the cache, and the startup warm-up.
"""

import datetime

from magpie.clients import clickhouse
from magpie.errors import RemoteError
from magpie.services.clickpy import SPECS, Dimension

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
    assert sql is SPECS[Dimension.RECENT].sql
    assert parameters == {"package": "sqlalchemy"}


def test_serves_each_dimension(client, monkeypatch):
    rows = [{"date": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows, rows, [{"date": DAY, "with_mirrors": 5, "without_mirrors": 4}])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/pkg/python_minor").json()["type"] == ("python_minor_downloads")
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


def test_unknown_dimension_is_rejected_without_querying(client, monkeypatch):
    """The path parameter is typed as the enum, so FastAPI rejects a dimension the dashboard does not draw before the
    route body runs.
    """
    fake = FakeExecute()
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/python_major")

    assert response.status_code == 422
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
