"""ClickPy business logic: the queries, their execution, and the shaping.

ClickPy publishes the PyPI download dataset on a public, read-only ClickHouse instance. This module owns what the
download endpoints mean: the SQL they run and the shaping that keeps the resulting JSON identical to the
pypistats.org API (see https://pypistats.org/api/).

The dashboard draws two different shapes, so there are two entry points with two response models: ``fetch_recent``
serves one package's last day/week/month totals and ``fetch_series`` serves the daily ``overall``, ``python_minor``
and ``system`` time series. Callers -- and the OpenAPI schema -- see a concrete type instead of JSON whose shape
depends on a ``dimension`` parameter.

Every query reads ClickPy's pre-aggregated tables instead of the 2.2 trillion row ``pypi`` table: those are ordered
by project, so a single-package query prunes to that package's rows, which is what the public read-only instance is
sized for.
"""

import datetime
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from async_lru import alru_cache
from pydantic import BaseModel

from magpie.clients import clickhouse
from magpie.services import CACHE_TTL

logger = logging.getLogger(__name__)

# pypistats retains 180 days and serves them as 181 inclusive dates ending on the newest one; the same span is kept
# here. The dashboard labels its x axis "MM-DD", which only stays unambiguous inside a single year.
WINDOW_DAYS = 180

# The mirrors pypistats.org excludes from its numbers. ClickPy also sees Nexus and other installers; keeping this list
# identical is what makes the numbers line up with pypistats.org.
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

OVERALL_SQL = f"""
WITH {_ANCHOR}
SELECT
    date,
    sum(count)                                          AS with_mirrors,
    sumIf(count, lower(installer) NOT IN {_MIRRORS_SQL}) AS without_mirrors
FROM pypi.pypi_downloads_per_day_by_version_by_installer_by_type, anchor AS a
WHERE project = %(package)s
  AND date >= a.d - {WINDOW_DAYS}
GROUP BY date
ORDER BY date
"""


def _category_sql(table: str, column: str) -> str:
    return f"""
WITH {_ANCHOR}
SELECT
    date,
    {column}   AS category,
    sum(count) AS downloads
FROM {table}, anchor AS a
WHERE project = %(package)s
  AND date >= a.d - {WINDOW_DAYS}
GROUP BY date, category
ORDER BY date, category
"""


PYTHON_MINOR_SQL = _category_sql("pypi.pypi_downloads_per_day_by_version_by_python", "python_minor")
SYSTEM_SQL = _category_sql("pypi.pypi_downloads_per_day_by_version_by_system", "system")


def normalize_project(name: str) -> str:
    """PEP 503 normalisation.

    ClickHouse stores normalised project names: ``SQLAlchemy`` and ``ruamel.yaml`` match nothing, ``sqlalchemy`` and
    ``ruamel-yaml`` do.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


class RecentTotals(BaseModel):
    """The three windows pypistats' ``recent`` reports."""

    last_day: int
    last_month: int
    last_week: int


class RecentDownloads(BaseModel):
    """One package's ``recent`` download totals."""

    data: RecentTotals
    package: str
    type: Literal["recent_downloads"]


class DownloadPoint(BaseModel):
    """One day of one series: pypistats' own ``(category, date, downloads)``."""

    category: str
    date: datetime.date
    downloads: int


class SeriesDownloads(BaseModel):
    """One package's daily download series for one dimension."""

    data: list[DownloadPoint]
    package: str
    type: Literal["overall_downloads", "python_minor_downloads", "system_downloads"]


def shape_recent(package: str, rows: list[dict[str, Any]]) -> RecentDownloads | None:
    """Shape the one-row ``recent`` aggregate, or ``None`` for a package with no download records at all -- the row is
    all zeros in that case, so the query reports how many rows it saw to tell the two apart.
    """
    if not rows or not rows[0]["rows_seen"]:
        return None
    row = rows[0]
    return RecentDownloads(
        data=RecentTotals(
            last_day=row["last_day"],
            last_month=row["last_month"],
            last_week=row["last_week"],
        ),
        package=package,
        type="recent_downloads",
    )


def shape_overall(package: str, rows: list[dict[str, Any]]) -> SeriesDownloads | None:
    """Shape the daily time series into pypistats' two mirror categories."""
    if not rows:
        return None
    data: list[DownloadPoint] = []
    for row in rows:
        data.append(
            DownloadPoint(
                category="with_mirrors",
                date=row["date"],
                downloads=row["with_mirrors"],
            )
        )
        data.append(
            DownloadPoint(
                category="without_mirrors",
                date=row["date"],
                downloads=row["without_mirrors"],
            )
        )
    return SeriesDownloads(data=data, package=package, type="overall_downloads")


def shape_category(
    package: str,
    rows: list[dict[str, Any]],
    *,
    type_: Literal["python_minor_downloads", "system_downloads"],
    rename: Callable[[str], str],
) -> SeriesDownloads | None:
    """Shape a daily ``(date, category, downloads)`` series."""
    if not rows:
        return None
    return SeriesDownloads(
        data=[
            DownloadPoint(
                category=rename(row["category"]),
                date=row["date"],
                downloads=row["downloads"],
            )
            for row in rows
        ],
        package=package,
        type=type_,
    )


def _python_category(value: str) -> str:
    return value or NULL_CATEGORY


def _system_category(value: str) -> str:
    if not value:
        return NULL_CATEGORY
    return value if value in KNOWN_SYSTEMS else OTHER_CATEGORY


class SeriesDimension(StrEnum):
    """A time series the dashboard draws."""

    OVERALL = "overall"
    PYTHON_MINOR = "python_minor"
    SYSTEM = "system"


@dataclass(frozen=True)
class SeriesSpec:
    """A supported series: its query and its response shaper."""

    sql: str
    shape: Callable[[str, list[dict[str, Any]]], SeriesDownloads | None]


SERIES_SPECS: dict[SeriesDimension, SeriesSpec] = {
    SeriesDimension.OVERALL: SeriesSpec(OVERALL_SQL, shape_overall),
    SeriesDimension.PYTHON_MINOR: SeriesSpec(
        PYTHON_MINOR_SQL,
        lambda package, rows: shape_category(package, rows, type_="python_minor_downloads", rename=_python_category),
    ),
    SeriesDimension.SYSTEM: SeriesSpec(
        SYSTEM_SQL,
        lambda package, rows: shape_category(package, rows, type_="system_downloads", rename=_system_category),
    ),
}


@alru_cache(maxsize=256, ttl=CACHE_TTL)
async def fetch_recent(package: str) -> RecentDownloads | None:
    """Run the ``recent`` query, shape it for the API, and memoize it.

    Cached because ClickPy is refreshed about once a day: how long an answer stays fresh is a property of the data
    source. ``None`` means the package has no download records at all, which the route turns into a 404.
    """
    rows = await clickhouse.execute(RECENT_SQL, {"package": normalize_project(package)})
    return shape_recent(package, rows)


@alru_cache(maxsize=256, ttl=CACHE_TTL)
async def fetch_series(package: str, dimension: SeriesDimension) -> SeriesDownloads | None:
    """Run one series dimension's query, shape it for the API, and memoize it.

    Same caching and ``None`` contract as :func:`fetch_recent`.
    """
    spec = SERIES_SPECS[dimension]
    rows = await clickhouse.execute(spec.sql, {"package": normalize_project(package)})
    return spec.shape(package, rows)


# The dashboard's package, warmed at startup.
PREWARM_PACKAGE = "sqllineage"


async def prewarm() -> None:
    """Fill the cache for every response of the dashboard package.

    Best effort: a cold or unreachable ClickHouse must not stop the app from starting, and a later request just pays
    for its own query instead.
    """
    # Keyword arguments to match the routes' calls: alru_cache keys positional and keyword calls separately.
    warmups: list[tuple[str, Awaitable[object]]] = [
        ("recent", fetch_recent(package=PREWARM_PACKAGE)),
        *((dimension, fetch_series(package=PREWARM_PACKAGE, dimension=dimension)) for dimension in SERIES_SPECS),
    ]
    for label, warmup in warmups:
        try:
            await warmup
        except Exception:  # noqa: BLE001 -- any query failure is non-fatal here
            logger.warning("prewarm failed for %s/%s", PREWARM_PACKAGE, label, exc_info=True)
