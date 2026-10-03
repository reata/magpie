"""Tests for the ClickPy query layer: name normalisation, shaping and the SQL.

The queries are asserted rather than executed -- the real instance needs credentials and a network -- because what
the dashboard depends on is the contract they encode.
"""

import asyncio
import datetime

import pytest

from magpie import services
from magpie.errors import RemoteError
from magpie.services import clickpy
from magpie.services.clickpy import (
    DEFAULT_WINDOW,
    RANK_SQL,
    RECENT_SQL,
    SERIES_SPECS,
    WINDOW_SPECS,
    SeriesDimension,
    SeriesInterval,
    SeriesWindow,
    normalize_project,
    shape_overall,
    shape_recent,
)

DAY = datetime.date(2026, 9, 11)
DAY_AFTER = datetime.date(2026, 9, 12)


# --------------------------------------------------------------------------- #
# name normalisation
# --------------------------------------------------------------------------- #


def test_normalize_project_is_pep503():
    assert normalize_project("SQLAlchemy") == "sqlalchemy"
    assert normalize_project("ruamel.yaml") == "ruamel-yaml"
    assert normalize_project("Django_REST.framework") == "django-rest-framework"


def test_normalization_keeps_distinct_projects_distinct():
    """``django-rest-framework`` and ``djangorestframework`` are both real, separate projects, so dashes must not be
    collapsed away.
    """
    assert normalize_project("Django-REST-Framework") == "django-rest-framework"
    assert normalize_project("djangorestframework") == "djangorestframework"


# --------------------------------------------------------------------------- #
# shaping
# --------------------------------------------------------------------------- #


def test_shape_recent():
    rows = [{"last_day": 1, "last_week": 2, "last_month": 3, "rows_seen": 30}]
    rank = [{"rank_month": 4, "total_packages": 8}]

    payload = shape_recent("sqllineage", rows, rank)

    assert payload is not None
    assert payload.model_dump(mode="json") == {
        "data": {
            "last_day": 1,
            "last_month": 3,
            "last_week": 2,
            "rank_month": 4,
            "rank_month_percentile": 50.0,
        },
        "package": "sqllineage",
        "type": "recent_downloads",
    }


def test_shape_recent_unknown_package():
    """The all-zero row is ambiguous on its own, hence ``rows_seen``."""
    all_zero = [{"last_day": 0, "last_week": 0, "last_month": 0, "rows_seen": 0}]

    assert shape_recent("nope", all_zero, []) is None
    assert shape_recent("nope", [], []) is None


@pytest.mark.parametrize(
    ("rank_month", "total_packages", "expected"),
    [
        (1, 10000, 0.01),
        (3953, 931904, 0.4242),
        (7, 8, 87.5),
        (5, 0, 0.0),
    ],
)
def test_rank_percentile_is_the_top_share(rank_month, total_packages, expected):
    """The percentile is ``rank / total``, so the top package is near 0 and the last is 100: the package sits in the top
    ``rank_month_percentile`` percent.
    """
    rows = [{"last_day": 1, "last_week": 2, "last_month": 3, "rows_seen": 30}]

    payload = shape_recent("pkg", rows, [{"rank_month": rank_month, "total_packages": total_packages}])

    assert payload is not None
    assert payload.data.rank_month_percentile == expected


@pytest.mark.parametrize("interval", list(SeriesInterval))
def test_shape_overall_emits_both_categories_oldest_first(interval):
    payload = shape_overall(
        "pkg",
        [
            {"bucket": DAY, "with_mirrors": 10, "without_mirrors": 8},
            {"bucket": DAY_AFTER, "with_mirrors": 20, "without_mirrors": 18},
        ],
        interval,
    )

    assert payload is not None
    assert payload.model_dump(mode="json") == {
        "data": [
            {"category": "with_mirrors", "date": "2026-09-11", "downloads": 10},
            {"category": "without_mirrors", "date": "2026-09-11", "downloads": 8},
            {"category": "with_mirrors", "date": "2026-09-12", "downloads": 20},
            {"category": "without_mirrors", "date": "2026-09-12", "downloads": 18},
        ],
        "interval": interval.value,
        "package": "pkg",
        "type": "overall_downloads",
    }


@pytest.mark.parametrize(
    ("dimension", "raw", "expected"),
    [
        (SeriesDimension.PYTHON_MINOR, "3.12", "3.12"),
        (SeriesDimension.PYTHON_MINOR, "", "null"),
        (SeriesDimension.SYSTEM, "Linux", "Linux"),
        (SeriesDimension.SYSTEM, "Darwin", "Darwin"),
        (SeriesDimension.SYSTEM, "Windows", "Windows"),
        (SeriesDimension.SYSTEM, "", "null"),
        (SeriesDimension.SYSTEM, "CYGWIN_NT-10.0-19042", "other"),
        (SeriesDimension.SYSTEM, "FreeBSD", "other"),
    ],
)
def test_category_mapping(dimension, raw, expected):
    payload = SERIES_SPECS[dimension].shape(
        "pkg", [{"bucket": DAY, "category": raw, "downloads": 7}], SeriesInterval.DAY
    )

    assert payload is not None
    assert payload.model_dump(mode="json")["data"] == [{"category": expected, "date": "2026-09-11", "downloads": 7}]
    assert payload.interval is SeriesInterval.DAY
    assert payload.type == (
        "python_minor_downloads" if dimension is SeriesDimension.PYTHON_MINOR else "system_downloads"
    )


@pytest.mark.parametrize("dimension", list(SeriesDimension))
def test_empty_series_has_no_payload(dimension):
    assert SERIES_SPECS[dimension].shape("nope", [], SeriesInterval.DAY) is None


# --------------------------------------------------------------------------- #
# the supported subset
# --------------------------------------------------------------------------- #


def test_only_the_series_the_dashboard_draws_are_supported():
    assert set(SERIES_SPECS) == set(SeriesDimension)


def test_recent_is_not_a_series_dimension():
    """``recent`` has its own entry point and response model, so it is not a series dimension."""
    assert "recent" not in {dimension.value for dimension in SeriesDimension}


def _all_sql():
    """Every query the service can run, across dimensions and windows."""
    return [RECENT_SQL, RANK_SQL, *(sql for spec in SERIES_SPECS.values() for sql in spec.sqls.values())]


def test_queries_use_aggregate_tables_only():
    for sql in _all_sql():
        assert "%(package)s" in sql
        # The raw ``pypi`` table is deliberately never queried: it is what would exhaust the public instance's read
        # quota.
        assert "FROM pypi.pypi\n" not in sql


def test_mirror_downloads_are_excluded_like_pypistats():
    for sql in [RECENT_SQL, *SERIES_SPECS[SeriesDimension.OVERALL].sqls.values()]:
        assert "lower(installer) NOT IN ('bandersnatch', 'z3c.pypimirror', 'artifactory', 'devpi')" in sql


@pytest.mark.parametrize(
    ("window", "days"),
    [(window, spec.days) for window, spec in WINDOW_SPECS.items() if spec.days is not None],
)
def test_each_window_reaches_back_its_offset(window, days):
    for dimension in SeriesDimension:
        assert f"a.d - {days}" in SERIES_SPECS[dimension].sqls[window]


def test_the_all_window_drops_the_date_filter():
    """``all`` has nothing to window against, so its query reads the package's rows with no anchor at all."""
    for dimension in SeriesDimension:
        sql = SERIES_SPECS[dimension].sqls[SeriesWindow.ALL]
        assert "date >=" not in sql
        assert "anchor" not in sql


@pytest.mark.parametrize(
    ("interval", "bucket"),
    [
        (SeriesInterval.DAY, "date AS bucket"),
        (SeriesInterval.WEEK, "toStartOfWeek(date) AS bucket"),
    ],
)
def test_each_interval_has_its_bucket(interval, bucket):
    """A point per day for 3 years is more rows than the public instance returns, and more than a chart can draw."""
    for window, spec in WINDOW_SPECS.items():
        if spec.interval is not interval:
            continue
        for dimension in SeriesDimension:
            assert bucket in SERIES_SPECS[dimension].sqls[window]


def test_the_long_windows_are_the_weekly_ones():
    assert {window for window, spec in WINDOW_SPECS.items() if spec.interval is SeriesInterval.WEEK} == {
        SeriesWindow.YEARS_3,
        SeriesWindow.ALL,
    }


@pytest.mark.parametrize("dimension", list(SeriesDimension))
def test_every_series_covers_every_window(dimension):
    assert set(SERIES_SPECS[dimension].sqls) == set(SeriesWindow)


def test_the_default_window_is_180_days():
    """The span the dashboard opened with, kept so a client that sends no window gets the same chart."""
    assert DEFAULT_WINDOW is SeriesWindow.DAYS_180
    assert WINDOW_SPECS[DEFAULT_WINDOW].days == 180
    assert WINDOW_SPECS[DEFAULT_WINDOW].interval is SeriesInterval.DAY


# --------------------------------------------------------------------------- #
# startup warm-up
# --------------------------------------------------------------------------- #


def test_prewarm_queries_recent_and_every_series(monkeypatch):
    calls = []

    # Keyword-only: the warm-up must key the caches exactly as the routes do.
    async def fake_recent(*, package):
        calls.append(("recent", package))

    async def fake_series(*, package, dimension, window):
        calls.append((dimension, window, package))

    monkeypatch.setattr(clickpy, "fetch_recent", fake_recent)
    monkeypatch.setattr(clickpy, "fetch_series", fake_series)

    asyncio.run(clickpy.prewarm())

    assert calls == [
        ("recent", clickpy.PREWARM_PACKAGE),
        *((dimension, clickpy.DEFAULT_WINDOW, clickpy.PREWARM_PACKAGE) for dimension in SERIES_SPECS),
    ]


def test_prewarm_survives_a_failing_series(monkeypatch):
    """A cold ClickHouse must not stop the app from starting, nor keep the remaining warm-ups from running."""
    calls = []

    async def fake_recent(*, package):
        calls.append("recent")

    async def flaky_series(*, package, dimension, window):
        calls.append(dimension)
        if dimension is SeriesDimension.OVERALL:
            raise RemoteError("clickhouse query failed: boom")

    monkeypatch.setattr(clickpy, "fetch_recent", fake_recent)
    monkeypatch.setattr(clickpy, "fetch_series", flaky_series)

    asyncio.run(clickpy.prewarm())

    assert calls == ["recent", *SERIES_SPECS]


def test_prewarm_is_disabled_for_tests():
    """The session TestClient runs the real lifespan; a live warm-up would race the per-test fakes."""
    assert services.PREWARM_ENABLED is False
