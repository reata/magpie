"""Tests for the ``/api/clickpy`` endpoints.

The queries run against a fake ClickHouse client: what these assert is the HTTP side -- the response envelopes, what
a missing package means, the cache, and the startup warm-up.
"""

import datetime

from magpie.clients import clickhouse
from magpie.errors import RemoteError
from magpie.services.clickpy import (
    DEFAULT_WINDOW,
    RANK_SQL,
    RECENT_SQL,
    SERIES_SPECS,
    SeriesDimension,
    SeriesWindow,
)

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


def _rank(**overrides):
    row = {"rank_month": 4, "total_packages": 8}
    row.update(overrides)
    return row


def _recent_payload(**data_overrides):
    data = {
        "last_day": 1,
        "last_month": 3,
        "last_week": 2,
        "rank_month": 4,
        "rank_month_percentile": 50.0,
    }
    data.update(data_overrides)
    return {"data": data, "package": "sqllineage", "type": "recent_downloads"}


def _resolve(schema, node):
    """Follow a component ``$ref`` so a test asserts the shape, not its name."""
    if "$ref" in node:
        name = node["$ref"].rpartition("/")[2]
        return schema["components"]["schemas"][name]
    return node


def _response_schema(schema, operation):
    return _resolve(
        schema,
        operation["responses"]["200"]["content"]["application/json"]["schema"],
    )


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


def test_serves_recent_in_pypistats_shape(client, monkeypatch):
    fake = FakeExecute([_recent()], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/recent")

    assert response.status_code == 200
    assert response.json() == _recent_payload()


def test_recent_ranks_the_package_after_its_totals(client, monkeypatch):
    """The rank is a second query over the per-month table, keyed the same way."""
    fake = FakeExecute([_recent()], [_rank(rank_month=3953, total_packages=931904)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    payload = client.get("/api/clickpy/sqllineage/recent").json()

    assert payload["data"]["rank_month"] == 3953
    assert payload["data"]["rank_month_percentile"] == 0.4242


def test_normalizes_the_package_before_querying(client, monkeypatch):
    fake = FakeExecute([_recent()], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    client.get("/api/clickpy/SQLAlchemy/recent")

    assert fake.calls == [
        (RECENT_SQL, {"package": "sqlalchemy"}),
        (RANK_SQL, {"package": "sqlalchemy"}),
    ]


def test_unknown_package_is_not_ranked(client, monkeypatch):
    """A package ClickPy never saw has nothing to rank, so the second query is skipped rather than run for a name that
    does not exist.
    """
    fake = FakeExecute([_recent(last_day=0, last_week=0, last_month=0, rows_seen=0)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/nope/recent").status_code == 404
    assert [sql for sql, _ in fake.calls] == [RECENT_SQL]


def test_serves_each_series_dimension(client, monkeypatch):
    rows = [{"bucket": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows, rows, [{"bucket": DAY, "with_mirrors": 5, "without_mirrors": 4}])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/pkg/python_minor").json()["type"] == ("python_minor_downloads")
    assert client.get("/api/clickpy/pkg/system").json()["type"] == "system_downloads"
    assert client.get("/api/clickpy/pkg/overall").json()["type"] == "overall_downloads"


def test_series_is_served_in_pypistats_shape(client, monkeypatch):
    """The date is the model's ``datetime.date``, so the wire format depends on its JSON encoding staying the ISO string
    pypistats uses.
    """
    rows = [{"bucket": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows)
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/pkg/python_minor").json() == {
        "data": [{"category": "3.12", "date": "2026-09-11", "downloads": 5}],
        "interval": "day",
        "package": "pkg",
        "type": "python_minor_downloads",
    }


def test_long_window_reports_the_weekly_interval(client, monkeypatch):
    """A long window's ``date`` is a week start, so the response says which interval it used."""
    rows = [{"bucket": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows)
    monkeypatch.setattr(clickhouse, "execute", fake)

    payload = client.get("/api/clickpy/pkg/python_minor?window=all").json()

    assert payload["interval"] == "week"
    assert fake.calls[0][0] == SERIES_SPECS[SeriesDimension.PYTHON_MINOR].sqls[SeriesWindow.ALL]


def test_each_series_path_runs_its_own_query(client, monkeypatch):
    rows = [{"bucket": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows, rows, [{"bucket": DAY, "with_mirrors": 5, "without_mirrors": 4}])
    monkeypatch.setattr(clickhouse, "execute", fake)

    client.get("/api/clickpy/pkg/python_minor")
    client.get("/api/clickpy/pkg/system")
    client.get("/api/clickpy/pkg/overall")

    assert [sql for sql, _ in fake.calls] == [
        SERIES_SPECS[SeriesDimension.PYTHON_MINOR].sqls[DEFAULT_WINDOW],
        SERIES_SPECS[SeriesDimension.SYSTEM].sqls[DEFAULT_WINDOW],
        SERIES_SPECS[SeriesDimension.OVERALL].sqls[DEFAULT_WINDOW],
    ]


def test_window_query_param_selects_the_query(client, monkeypatch):
    """``window`` reaches the service, which then runs that window's SQL rather than the default one."""
    rows = [{"bucket": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows)
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/pkg/python_minor?window=1y")

    assert response.status_code == 200
    assert [sql for sql, _ in fake.calls] == [SERIES_SPECS[SeriesDimension.PYTHON_MINOR].sqls[SeriesWindow.YEAR_1]]


def test_unknown_window_is_rejected_without_querying(client, monkeypatch):
    """``window`` is typed as the enum, so FastAPI rejects a window the dashboard does not offer."""
    fake = FakeExecute()
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/pkg/overall?window=10y")

    assert response.status_code == 422
    assert fake.calls == []


def test_response_is_cached_per_window(client, monkeypatch):
    """Each window is its own cache entry, so switching back and forth only queries once each."""
    rows = [{"bucket": DAY, "category": "3.12", "downloads": 5}]
    fake = FakeExecute(rows, rows)
    monkeypatch.setattr(clickhouse, "execute", fake)

    client.get("/api/clickpy/pkg/python_minor")
    client.get("/api/clickpy/pkg/python_minor?window=1y")
    client.get("/api/clickpy/pkg/python_minor")
    client.get("/api/clickpy/pkg/python_minor?window=1y")

    assert len(fake.calls) == 2


def test_response_is_cached_per_path(client, monkeypatch):
    """ClickPy is refreshed once a day, so one pair of queries per path per TTL is plenty."""
    fake = FakeExecute([_recent()], [_rank()], [_recent(last_day=99)], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    first = client.get("/api/clickpy/sqllineage/recent")
    second = client.get("/api/clickpy/sqllineage/recent")

    assert len(fake.calls) == 2
    assert first.json() == second.json() == _recent_payload()


def test_unknown_dimension_is_rejected_without_querying(client, monkeypatch):
    """The path parameter is typed as the enum, so FastAPI rejects a dimension the dashboard does not draw before the
    route body runs.
    """
    fake = FakeExecute()
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/python_major")

    assert response.status_code == 422
    assert fake.calls == []


def test_recent_is_not_a_series_dimension(client, monkeypatch):
    """``recent`` is a separate route: it must be served by its own handler, not matched by the ``{dimension}`` route
    and rejected as an unknown series.
    """
    fake = FakeExecute([_recent()], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/recent")

    assert response.status_code == 200
    assert fake.calls[0][0] is RECENT_SQL


def test_failure_is_503_and_not_cached(client, monkeypatch):
    fake = FakeExecute(RemoteError("clickhouse query failed: boom"), [_recent()], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    first = client.get("/api/clickpy/sqllineage/recent")
    second = client.get("/api/clickpy/sqllineage/recent")

    assert first.status_code == 503
    assert "boom" in first.json()["detail"]
    assert second.status_code == 200


# --------------------------------------------------------------------------- #
# the documented contract
# --------------------------------------------------------------------------- #


def test_openapi_gives_each_operation_its_own_response_type(client):
    """The reason for the split: each operation documents the exact JSON it returns instead of one shape whose ``data``
    depends on ``dimension``.
    """
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    recent = _response_schema(schema, paths["/api/clickpy/{package}/recent"]["get"])
    series = _response_schema(schema, paths["/api/clickpy/{package}/{dimension}"]["get"])

    assert recent["properties"]["type"]["const"] == "recent_downloads"
    assert series["properties"]["type"]["enum"] == [
        "overall_downloads",
        "python_minor_downloads",
        "system_downloads",
    ]
    assert recent["properties"]["data"] != series["properties"]["data"]


def test_openapi_recent_documents_the_rank_fields(client):
    schema = client.get("/openapi.json").json()
    recent = _response_schema(schema, schema["paths"]["/api/clickpy/{package}/recent"]["get"])

    data = _resolve(schema, recent["properties"]["data"])

    assert set(data["properties"]) == {
        "last_day",
        "last_month",
        "last_week",
        "rank_month",
        "rank_month_percentile",
    }
    assert data["properties"]["rank_month"]["type"] == "integer"
    assert data["properties"]["rank_month_percentile"]["type"] == "number"


def test_openapi_series_dimension_lists_only_series(client):
    schema = client.get("/openapi.json").json()
    series = schema["paths"]["/api/clickpy/{package}/{dimension}"]["get"]

    dimension = next(parameter for parameter in series["parameters"] if parameter["name"] == "dimension")

    assert _resolve(schema, dimension["schema"])["enum"] == [
        "overall",
        "python_minor",
        "system",
    ]


def test_openapi_series_documents_the_windows(client):
    schema = client.get("/openapi.json").json()
    series = schema["paths"]["/api/clickpy/{package}/{dimension}"]["get"]

    window = next(parameter for parameter in series["parameters"] if parameter["name"] == "window")

    assert _resolve(schema, window["schema"])["enum"] == ["180d", "1y", "3y", "all"]
    assert window["required"] is False


def test_openapi_series_documents_the_interval(client):
    """A client cannot infer day-vs-week granularity from the request alone once windows change, so it is documented."""
    schema = client.get("/openapi.json").json()
    series = _response_schema(schema, schema["paths"]["/api/clickpy/{package}/{dimension}"]["get"])

    interval = _resolve(schema, series["properties"]["interval"])

    assert interval["enum"] == ["day", "week"]
