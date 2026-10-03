"""ClickPy business logic: the queries, their execution, and the shaping.

ClickPy publishes the PyPI download dataset on a public, read-only ClickHouse instance. This module owns what the
download endpoints mean: the SQL they run and the shaping of their JSON.

The response shapes, the category names and the ``type`` values come from pypistats.org (see
https://pypistats.org/api/), the API magpie started from, and they are kept for backward compatibility. magpie is not
a pypistats mirror any more: the numbers come from ClickPy, and the responses have grown fields pypistats has no
equivalent for -- the monthly rank below -- so the JSON is a superset of the old shape, not identical to it.

The dashboard draws two different shapes, so there are two entry points with two response models: ``fetch_recent``
serves one package's last day/week/month totals, and ``fetch_series`` serves the ``overall``, ``python_minor`` and
``system`` time series for a caller-chosen window (180 days by default, or a year, three years, or everything ClickPy
has, with the long windows bucketed by week). Callers -- and the OpenAPI schema -- see a concrete type instead of JSON
whose shape depends on a ``dimension`` parameter.

``fetch_recent`` also reports where the package's month sits among every package. That rank comes from the per-month
table rather than the per-day one: the public instance stops a query at a billion rows read and returns the partial
result, and the per-day table is already a billion rows, so only the per-month table (ordered by ``month, project``)
can rank the whole catalogue inside that cap. Its window and ordering follow hugovk's top-pypi-packages monthly dump
(https://hugovk.dev/top-pypi-packages/), so the two agree.

Every query reads one of the pre-aggregated tables that ClickPy's materialized views maintain, never the raw
multi-trillion row ``pypi`` table they are built from:

* ``pypi_downloads_per_day_by_version_by_installer_by_type`` -- ``recent`` and
  ``overall``, the smallest view that carries the installer dimension, which is
  what mirror exclusion needs;
* ``pypi_downloads_per_day_by_version_by_python`` and ``..._by_system`` -- the
  two category series;
* ``pypi_downloads_per_day`` -- the newest date each window anchors on;
* ``pypi_downloads_per_month`` -- the rank, the only view ordered by time first
  (``month, project``), so one month can be aggregated without a full scan.
"""

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, Literal

from async_lru import alru_cache
from pydantic import BaseModel, Field

from magpie.clients import clickhouse
from magpie.services import CACHE_TTL


class SeriesWindow(StrEnum):
    """How far back a series reaches."""

    DAYS_180 = "180d"
    YEAR_1 = "1y"
    YEARS_3 = "3y"
    ALL = "all"


class SeriesInterval(StrEnum):
    """How finely a series is bucketed."""

    DAY = "day"
    WEEK = "week"


class SeriesDimension(StrEnum):
    """A time series the dashboard draws."""

    OVERALL = "overall"
    PYTHON_MINOR = "python_minor"
    SYSTEM = "system"


@dataclass(frozen=True)
class WindowSpec:
    """How far a window reaches, and how finely its dates are bucketed.

    ``days`` is subtracted from the package's newest date; ``None`` is every date ClickPy has.
    """

    days: int | None
    interval: SeriesInterval


# The window includes its anchor date, so ``180d`` covers 181 dates: the span pypistats served, kept as the default. The
# two long windows are bucketed by week -- a point per day would run to tens of thousands of rows, more than the public
# instance will return at all (``max_result_rows`` is 10000) and more than a chart can draw usefully.
WINDOW_SPECS: dict[SeriesWindow, WindowSpec] = {
    SeriesWindow.DAYS_180: WindowSpec(days=180, interval=SeriesInterval.DAY),
    SeriesWindow.YEAR_1: WindowSpec(days=365, interval=SeriesInterval.DAY),
    SeriesWindow.YEARS_3: WindowSpec(days=3 * 365, interval=SeriesInterval.WEEK),
    SeriesWindow.ALL: WindowSpec(days=None, interval=SeriesInterval.WEEK),
}

DEFAULT_WINDOW = SeriesWindow.DAYS_180

# The mirrors pypistats.org excludes from its numbers; the list is inherited with the API and kept, so a mirror replay
# is not counted as a download. ClickPy also sees Nexus and other installers, which the list does not cover.
MIRRORS = ("bandersnatch", "z3c.pypimirror", "artifactory", "devpi")
_MIRRORS_SQL = "(" + ", ".join(f"'{name}'" for name in MIRRORS) + ")"

# pypistats reports unknown values as the string "null" and folds every operating system it does not track into "other".
#
# Note that the two category tables carry no installer dimension, so unlike ``recent`` and ``overall`` these series
# include mirror downloads. ClickPy has no (installer x python/system) aggregate, and reading the detail table would hit
# the public instance's read quota; the difference is a fraction of a percent for packages mirrors do not sync heavily.
NULL_CATEGORY = "null"
OTHER_CATEGORY = "other"
KNOWN_SYSTEMS = frozenset({"Linux", "Windows", "Darwin"})

# The window is anchored on the newest date the dataset has rather than on ``today()``: ClickPy is updated once a day,
# so the two can differ.
_ANCHOR = """
anchor AS (
    SELECT max(date) AS d
    FROM pypi.pypi_downloads_per_day
    WHERE project = %(package)s
)
"""

RECENT_SQL = f"""
WITH {_ANCHOR}
SELECT
    sumIf(count, date = a.d)       AS last_day,
    sumIf(count, date >= a.d - 6)  AS last_week,
    sumIf(count, date >= a.d - 29) AS last_month,
    count()                        AS rows_seen
FROM pypi.pypi_downloads_per_day_by_version_by_installer_by_type, anchor AS a
WHERE project = %(package)s
  AND date >= a.d - 29
  AND lower(installer) NOT IN {_MIRRORS_SQL}
"""

# The package's position among every package for the last complete calendar month, plus how many packages that is, so a
# percentile can be derived.
#
# The table, the window and the ordering are hugovk's ``top-pypi-packages`` monthly dump
# (https://hugovk.dev/top-pypi-packages/), which ranks this same table on this same public instance with
# ``ORDER BY download_count DESC, project``. Reproducing that tie-break keeps the two rankings equal for tied packages
# as well, not just for packages with a unique total.
#
# Two constraints shape the query. It reads the per-month table and never the per-day one, because the per-day table is
# a billion rows and the public instance truncates a scan that large; ``pypi_downloads_per_month`` is ordered by
# ``(month, project)``, so a single month prunes to a couple of million rows. And a package with no downloads that month
# is put at the bottom rather than above the whole field, which a bare ``total > 0`` count would do.
RANK_SQL = """
WITH (
    SELECT toStartOfMonth(today()) - INTERVAL 1 MONTH
) AS ranked_month,
(
    SELECT sum(count)
    FROM pypi.pypi_downloads_per_month
    WHERE month = ranked_month AND project = %(package)s
) AS package_total
SELECT
    if(
        package_total = 0,
        count(),
        1 + countIf(total > package_total)
          + countIf(total = package_total AND project < %(package)s)
    ) AS rank_month,
    count() AS total_packages
FROM (
    SELECT project, sum(count) AS total
    FROM pypi.pypi_downloads_per_month
    WHERE month = ranked_month
    GROUP BY project
)
"""


def _bucket(interval: SeriesInterval) -> str:
    return "toStartOfWeek(date)" if interval is SeriesInterval.WEEK else "date"


def _overall_sql(spec: WindowSpec) -> str:
    """The two mirror categories per bucket, over one window.

    A window with no ``days`` asks for every date ClickPy has, which needs no anchor: there is nothing to window
    against.
    """
    bucket = _bucket(spec.interval)
    if spec.days is None:
        return f"""
SELECT
    {bucket} AS bucket,
    sum(count)                                          AS with_mirrors,
    sumIf(count, lower(installer) NOT IN {_MIRRORS_SQL}) AS without_mirrors
FROM pypi.pypi_downloads_per_day_by_version_by_installer_by_type
WHERE project = %(package)s
GROUP BY {bucket}
ORDER BY {bucket}
"""
    return f"""
WITH {_ANCHOR}
SELECT
    {bucket} AS bucket,
    sum(count)                                          AS with_mirrors,
    sumIf(count, lower(installer) NOT IN {_MIRRORS_SQL}) AS without_mirrors
FROM pypi.pypi_downloads_per_day_by_version_by_installer_by_type, anchor AS a
WHERE project = %(package)s
  AND date >= a.d - {spec.days}
GROUP BY {bucket}
ORDER BY {bucket}
"""


def _category_sql(table: str, column: str, spec: WindowSpec) -> str:
    bucket = _bucket(spec.interval)
    if spec.days is None:
        return f"""
SELECT
    {bucket} AS bucket,
    {column}   AS category,
    sum(count) AS downloads
FROM {table}
WHERE project = %(package)s
GROUP BY {bucket}, category
ORDER BY {bucket}, category
"""
    return f"""
WITH {_ANCHOR}
SELECT
    {bucket} AS bucket,
    {column}   AS category,
    sum(count) AS downloads
FROM {table}, anchor AS a
WHERE project = %(package)s
  AND date >= a.d - {spec.days}
GROUP BY {bucket}, category
ORDER BY {bucket}, category
"""


def normalize_project(name: str) -> str:
    """PEP 503 normalisation.

    ClickHouse stores normalised project names: ``SQLAlchemy`` and ``ruamel.yaml`` match nothing, ``sqlalchemy`` and
    ``ruamel-yaml`` do.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


class RecentStats(BaseModel):
    """One package's download totals, plus where its month sits."""

    last_day: int
    last_month: int
    last_week: int
    # Of the last complete calendar month, not of ``last_month``: a global rank over the rolling 30 days would have to
    # scan the whole per-day table, which the public instance will not do (see ``RANK_SQL``).
    rank_month: int = Field(description="Rank by downloads in the last complete calendar month.")
    rank_month_percentile: float = Field(
        description=("Percentage of packages ranked at or above this one; lower is better.")
    )


class RecentDownloads(BaseModel):
    """One package's ``recent`` download totals."""

    data: RecentStats
    package: str
    type: Literal["recent_downloads"]


class DownloadPoint(BaseModel):
    """One point of one series: pypistats' own ``(category, date, downloads)``, dated by day or by week."""

    category: str
    date: datetime.date
    downloads: int


class SeriesDownloads(BaseModel):
    """One package's download series for one dimension and window."""

    data: list[DownloadPoint]
    interval: SeriesInterval = Field(description="Whether each point covers a day or a week.")
    package: str
    type: Literal["overall_downloads", "python_minor_downloads", "system_downloads"]


def shape_recent(package: str, rows: list[dict[str, Any]], rank_rows: list[dict[str, Any]]) -> RecentDownloads | None:
    """Shape the one-row ``recent`` aggregate and its rank row.

    ``None`` for a package with no download records at all -- the aggregate row is all zeros in that case, so the
    query reports how many rows it saw to tell the two apart. The caller skips the rank query in that case, so
    ``rank_rows`` is only read for a package ClickPy has seen.
    """
    if not rows or not rows[0]["rows_seen"]:
        return None
    row = rows[0]
    rank = rank_rows[0]
    return RecentDownloads(
        data=RecentStats(
            last_day=row["last_day"],
            last_month=row["last_month"],
            last_week=row["last_week"],
            rank_month=rank["rank_month"],
            rank_month_percentile=_percentile(rank),
        ),
        package=package,
        type="recent_downloads",
    )


def _percentile(rank: dict[str, Any]) -> float:
    """The share of ranked packages at or above this one.

    ``rank / total`` as a percentage, so the top package is near 0 and the last is 100: the package sits in the top
    ``rank_month_percentile`` percent.
    """
    total = rank["total_packages"]
    if not total:
        return 0.0
    return round(rank["rank_month"] / total * 100, 4)


def shape_overall(package: str, rows: list[dict[str, Any]], interval: SeriesInterval) -> SeriesDownloads | None:
    """Shape the time series into pypistats' two mirror categories."""
    if not rows:
        return None
    data: list[DownloadPoint] = []
    for row in rows:
        data.append(
            DownloadPoint(
                category="with_mirrors",
                date=row["bucket"],
                downloads=row["with_mirrors"],
            )
        )
        data.append(
            DownloadPoint(
                category="without_mirrors",
                date=row["bucket"],
                downloads=row["without_mirrors"],
            )
        )
    return SeriesDownloads(data=data, interval=interval, package=package, type="overall_downloads")


def shape_category(
    package: str,
    rows: list[dict[str, Any]],
    interval: SeriesInterval,
    *,
    type_: Literal["python_minor_downloads", "system_downloads"],
    rename: Callable[[str], str],
) -> SeriesDownloads | None:
    """Shape a ``(bucket, category, downloads)`` series."""
    if not rows:
        return None
    return SeriesDownloads(
        data=[
            DownloadPoint(
                category=rename(row["category"]),
                date=row["bucket"],
                downloads=row["downloads"],
            )
            for row in rows
        ],
        interval=interval,
        package=package,
        type=type_,
    )


def _python_category(value: str) -> str:
    return value or NULL_CATEGORY


def _system_category(value: str) -> str:
    if not value:
        return NULL_CATEGORY
    return value if value in KNOWN_SYSTEMS else OTHER_CATEGORY


@dataclass(frozen=True)
class SeriesSpec:
    """A supported series: how to build its query for a window, and its response shaper."""

    sql: Callable[[WindowSpec], str]
    shape: Callable[[str, list[dict[str, Any]], SeriesInterval], SeriesDownloads | None]


SERIES_SPECS: dict[SeriesDimension, SeriesSpec] = {
    SeriesDimension.OVERALL: SeriesSpec(sql=_overall_sql, shape=shape_overall),
    SeriesDimension.PYTHON_MINOR: SeriesSpec(
        sql=partial(_category_sql, "pypi.pypi_downloads_per_day_by_version_by_python", "python_minor"),
        shape=partial(shape_category, type_="python_minor_downloads", rename=_python_category),
    ),
    SeriesDimension.SYSTEM: SeriesSpec(
        sql=partial(_category_sql, "pypi.pypi_downloads_per_day_by_version_by_system", "system"),
        shape=partial(shape_category, type_="system_downloads", rename=_system_category),
    ),
}


@alru_cache(maxsize=256, ttl=CACHE_TTL)
async def fetch_recent(package: str) -> RecentDownloads | None:
    """Run the ``recent`` query and its rank, shape them, and memoize the result.

    Cached because ClickPy is refreshed about once a day: how long an answer stays fresh is a property of the data
    source. ``None`` means the package has no download records at all, which the route turns into a 404.
    """
    normalized = normalize_project(package)
    rows = await clickhouse.execute(RECENT_SQL, {"package": normalized})
    if not rows or not rows[0]["rows_seen"]:
        # Nothing to rank: skip the extra query for a package ClickPy never saw.
        return None
    rank_rows = await clickhouse.execute(RANK_SQL, {"package": normalized})
    return shape_recent(package, rows, rank_rows)


@alru_cache(maxsize=256, ttl=CACHE_TTL)
async def fetch_series(package: str, dimension: SeriesDimension, window: SeriesWindow) -> SeriesDownloads | None:
    """Run one series dimension's query for one window, shape it, and memoize it.

    Same caching and ``None`` contract as :func:`fetch_recent`. ``window`` is required rather than defaulted because
    ``alru_cache`` keys calls by their arguments: a default here would give one request two cache entries.
    """
    spec = SERIES_SPECS[dimension]
    window_spec = WINDOW_SPECS[window]
    rows = await clickhouse.execute(spec.sql(window_spec), {"package": normalize_project(package)})
    return spec.shape(package, rows, window_spec.interval)
