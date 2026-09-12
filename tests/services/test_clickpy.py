"""Tests for the ClickPy query layer: name normalisation, shaping and the SQL.

The queries are asserted rather than executed -- the real instance needs
credentials and a network -- because what the dashboard depends on is the
contract they encode.
"""

import datetime

import pytest

from magpie.services.clickpy import (
    SPECS,
    WINDOW_DAYS,
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
    """``django-rest-framework`` and ``djangorestframework`` are both real,
    separate projects, so dashes must not be collapsed away."""
    assert normalize_project("Django-REST-Framework") == "django-rest-framework"
    assert normalize_project("djangorestframework") == "djangorestframework"


# --------------------------------------------------------------------------- #
# shaping
# --------------------------------------------------------------------------- #


def test_shape_recent():
    rows = [{"last_day": 1, "last_week": 2, "last_month": 3, "rows_seen": 30}]

    assert shape_recent("sqllineage", rows) == {
        "data": {"last_day": 1, "last_month": 3, "last_week": 2},
        "package": "sqllineage",
        "type": "recent_downloads",
    }


def test_shape_recent_unknown_package():
    """The all-zero row is ambiguous on its own, hence ``rows_seen``."""
    all_zero = [{"last_day": 0, "last_week": 0, "last_month": 0, "rows_seen": 0}]

    assert shape_recent("nope", all_zero) is None
    assert shape_recent("nope", []) is None


def test_shape_overall_emits_both_categories_oldest_first():
    payload = shape_overall(
        "pkg",
        [
            {"date": DAY, "with_mirrors": 10, "without_mirrors": 8},
            {"date": DAY_AFTER, "with_mirrors": 20, "without_mirrors": 18},
        ],
    )

    assert payload == {
        "data": [
            {"category": "with_mirrors", "date": "2026-09-11", "downloads": 10},
            {"category": "without_mirrors", "date": "2026-09-11", "downloads": 8},
            {"category": "with_mirrors", "date": "2026-09-12", "downloads": 20},
            {"category": "without_mirrors", "date": "2026-09-12", "downloads": 18},
        ],
        "package": "pkg",
        "type": "overall_downloads",
    }


@pytest.mark.parametrize(
    ("dimension", "raw", "expected"),
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
def test_category_mapping(dimension, raw, expected):
    payload = SPECS[dimension].shape(
        "pkg", [{"date": DAY, "category": raw, "downloads": 7}]
    )

    assert payload is not None
    assert payload["data"] == [
        {"category": expected, "date": "2026-09-11", "downloads": 7}
    ]
    assert payload["type"] == (
        "python_minor_downloads" if dimension == "python_minor" else "system_downloads"
    )


@pytest.mark.parametrize("dimension", ["overall", "python_minor", "system"])
def test_empty_series_has_no_payload(dimension):
    assert SPECS[dimension].shape("nope", []) is None


# --------------------------------------------------------------------------- #
# the supported subset
# --------------------------------------------------------------------------- #


def test_only_the_dimensions_the_dashboard_draws_are_supported():
    assert set(SPECS) == {"recent", "overall", "python_minor", "system"}


def test_queries_use_aggregate_tables_only():
    for spec in SPECS.values():
        assert "%(package)s" in spec.sql
        # The 2.2 trillion row detail table is deliberately never queried: it is
        # what would exhaust the public instance's read quota.
        assert "FROM pypi.pypi\n" not in spec.sql
        assert "pypi.pypi_downloads" in spec.sql


@pytest.mark.parametrize("dimension", ["recent", "overall"])
def test_mirror_downloads_are_excluded_like_pypistats(dimension):
    assert (
        "lower(installer) NOT IN ('bandersnatch', 'z3c.pypimirror', "
        "'artifactory', 'devpi')" in SPECS[dimension].sql
    )


@pytest.mark.parametrize("dimension", ["overall", "python_minor", "system"])
def test_series_are_windowed(dimension):
    assert f"a.d - {WINDOW_DAYS}" in SPECS[dimension].sql
