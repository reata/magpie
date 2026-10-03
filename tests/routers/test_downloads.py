"""Tests for the ``/api/clickpy`` endpoints.

Every test drives the real HTTP API with a fake ClickHouse client, so what they assert is what a client gets: the
response envelopes, which package names resolve, what a missing package means, the cache, and the documented OpenAPI
contract.
"""

import datetime

import pytest

from magpie.clients import clickhouse
from magpie.errors import RemoteError

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


def _category_row(**overrides):
    row = {"bucket": DAY, "category": "3.12", "downloads": 5}
    row.update(overrides)
    return row


def _overall_row(**overrides):
    row = {"bucket": DAY, "with_mirrors": 5, "without_mirrors": 4}
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


@pytest.mark.parametrize(
    ("rank_month", "total_packages", "percentile"),
    [
        (1, 10000, 0.01),
        (3953, 931904, 0.4242),
        (7, 8, 87.5),
        (5, 0, 0.0),
    ],
)
def test_rank_is_reported_as_a_percentile(client, monkeypatch, rank_month, total_packages, percentile):
    """``rank / total`` as a percentage, so the top package is near 0 and the last is 100."""
    fake = FakeExecute([_recent()], [_rank(rank_month=rank_month, total_packages=total_packages)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    data = client.get("/api/clickpy/sqllineage/recent").json()["data"]

    assert data["rank_month"] == rank_month
    assert data["rank_month_percentile"] == percentile


@pytest.mark.parametrize(
    ("requested", "queried"),
    [
        ("SQLAlchemy", "sqlalchemy"),
        ("ruamel.yaml", "ruamel-yaml"),
        ("Django_REST.framework", "django-rest-framework"),
        ("Django-REST-Framework", "django-rest-framework"),
        ("djangorestframework", "djangorestframework"),
    ],
)
def test_package_names_are_pep503_normalized(client, monkeypatch, requested, queried):
    """The name a client sends is looked up as the project ClickPy stores.

    The last two rows are separate projects, so dashes are normalised, not collapsed away.
    """
    fake = FakeExecute([_recent()], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get(f"/api/clickpy/{requested}/recent").status_code == 200
    assert [parameters["package"] for _, parameters in fake.calls] == [queried, queried]


def test_unknown_package_is_404_and_is_not_ranked(client, monkeypatch):
    """A package ClickPy never saw has nothing to rank, so the second query is skipped rather than run for a name that
    does not exist.
    """
    fake = FakeExecute([_recent(last_day=0, last_week=0, last_month=0, rows_seen=0)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/nope/recent").status_code == 404
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("dimension", "type_"),
    [
        ("overall", "overall_downloads"),
        ("python_minor", "python_minor_downloads"),
        ("system", "system_downloads"),
    ],
)
def test_serves_each_series_dimension(client, monkeypatch, dimension, type_):
    rows = [_overall_row()] if dimension == "overall" else [_category_row()]
    fake = FakeExecute(rows)
    monkeypatch.setattr(clickhouse, "execute", fake)

    payload = client.get(f"/api/clickpy/pkg/{dimension}").json()

    assert payload["package"] == "pkg"
    assert payload["type"] == type_


@pytest.mark.parametrize(
    ("dimension", "raw", "category"),
    [
        ("python_minor", "3.12", "3.12"),
        ("python_minor", "", "null"),
        ("system", "Linux", "Linux"),
        ("system", "Darwin", "Darwin"),
        ("system", "Windows", "Windows"),
        ("system", "", "null"),
        ("system", "CYGWIN_NT-10.0-19042", "other"),
        ("system", "FreeBSD", "other"),
    ],
)
def test_category_names_match_pypistats(client, monkeypatch, dimension, raw, category):
    """pypistats reports an unknown value as ``null`` and folds every system it does not track into ``other``."""
    fake = FakeExecute([_category_row(category=raw)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    data = client.get(f"/api/clickpy/pkg/{dimension}").json()["data"]

    assert [point["category"] for point in data] == [category]


@pytest.mark.parametrize("dimension", ["overall", "python_minor", "system"])
def test_series_without_rows_is_404(client, monkeypatch, dimension):
    """A package ClickPy has no rows for has no chart to draw."""
    fake = FakeExecute([])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get(f"/api/clickpy/pkg/{dimension}").status_code == 404


def test_series_is_served_in_pypistats_shape(client, monkeypatch):
    """The date is the model's ``datetime.date``, so the wire format depends on its JSON encoding staying the ISO string
    pypistats uses.
    """
    fake = FakeExecute([_category_row()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/pkg/python_minor").json() == {
        "data": [{"category": "3.12", "date": "2026-09-11", "downloads": 5}],
        "interval": "day",
        "package": "pkg",
        "type": "python_minor_downloads",
    }


def test_overall_series_lists_both_mirror_categories(client, monkeypatch):
    """pypistats draws the two categories as separate lines, each point dated the same way."""
    fake = FakeExecute([_overall_row(with_mirrors=10, without_mirrors=8)])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get("/api/clickpy/pkg/overall").json()["data"] == [
        {"category": "with_mirrors", "date": "2026-09-11", "downloads": 10},
        {"category": "without_mirrors", "date": "2026-09-11", "downloads": 8},
    ]


@pytest.mark.parametrize(
    ("window", "interval"),
    [
        ("180d", "day"),
        ("1y", "day"),
        ("3y", "week"),
        ("all", "week"),
    ],
)
def test_window_says_how_finely_the_series_is_bucketed(client, monkeypatch, window, interval):
    """A long window's ``date`` is a week start, so the response reports the granularity it used."""
    fake = FakeExecute([_category_row()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    assert client.get(f"/api/clickpy/pkg/python_minor?window={window}").json()["interval"] == interval


def test_omitting_the_window_is_the_180_day_window(client, monkeypatch):
    """A client that sends no ``window`` gets the chart it always got, out of the same cache entry."""
    fake = FakeExecute([_category_row()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    default = client.get("/api/clickpy/pkg/python_minor")
    explicit = client.get("/api/clickpy/pkg/python_minor?window=180d")

    assert default.json() == explicit.json()
    assert len(fake.calls) == 1


def test_unknown_window_is_rejected_without_querying(client, monkeypatch):
    """``window`` is typed as the enum, so FastAPI rejects a window the dashboard does not offer."""
    fake = FakeExecute()
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/pkg/overall?window=10y")

    assert response.status_code == 422
    assert fake.calls == []


def test_unknown_dimension_is_rejected_without_querying(client, monkeypatch):
    """The path parameter is typed as the enum, so FastAPI rejects a dimension the dashboard does not draw before the
    route body runs.
    """
    fake = FakeExecute()
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/python_major")

    assert response.status_code == 422
    assert fake.calls == []


def test_response_is_cached_per_series(client, monkeypatch):
    """One upstream query per package, dimension and window: a chart the dashboard already drew costs nothing again."""
    fake = FakeExecute([_category_row()], [_category_row()], [_overall_row()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    python_minor = client.get("/api/clickpy/pkg/python_minor")
    year = client.get("/api/clickpy/pkg/python_minor?window=1y")
    overall = client.get("/api/clickpy/pkg/overall")
    client.get("/api/clickpy/pkg/python_minor")
    client.get("/api/clickpy/pkg/python_minor?window=1y")
    client.get("/api/clickpy/pkg/overall")

    assert python_minor.json()["type"] == "python_minor_downloads"
    assert year.json()["interval"] == "day"
    assert overall.json()["type"] == "overall_downloads"
    assert len(fake.calls) == 3


def test_recent_queries_are_cached(client, monkeypatch):
    """ClickPy is refreshed once a day, so one pair of queries per path per TTL is plenty."""
    fake = FakeExecute([_recent()], [_rank()], [_recent(last_day=99)], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    first = client.get("/api/clickpy/sqllineage/recent")
    second = client.get("/api/clickpy/sqllineage/recent")

    assert len(fake.calls) == 2
    assert first.json() == second.json() == _recent_payload()


def test_recent_is_not_a_series_dimension(client, monkeypatch):
    """``recent`` is served by its own handler, not matched by ``{dimension}`` and rejected as an unknown series."""
    fake = FakeExecute([_recent()], [_rank()])
    monkeypatch.setattr(clickhouse, "execute", fake)

    response = client.get("/api/clickpy/sqllineage/recent")

    assert response.status_code == 200
    assert response.json() == _recent_payload()


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
