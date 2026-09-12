"""ClickPy business logic: the queries, their execution, and the shaping.

ClickPy publishes the PyPI download dataset on a public, read-only ClickHouse
instance. This module owns what the download endpoints mean: the SQL they run and
the shaping that keeps the resulting JSON identical to the pypistats.org API (see
https://pypistats.org/api/) -- only the three dimensions the dashboard draws
(``overall``, ``python_minor``, ``system``) plus ``recent``.

Every query reads ClickPy's pre-aggregated tables instead of the 2.2 trillion row
``pypi`` table: those are ordered by project, so a single-package query prunes to
that package's rows, which is what the public read-only instance is sized for.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from async_lru import alru_cache

from magpie.clients import clickhouse
from magpie.services import CACHE_TTL

#: pypistats retains 180 days and serves them as 181 inclusive dates ending on
#: the newest one; the same span is kept here. The dashboard labels its x axis
#: "MM-DD", which only stays unambiguous inside a single year.
WINDOW_DAYS = 180

#: The mirrors pypistats.org excludes from its numbers. ClickPy also sees Nexus
#: and other installers; keeping this list identical is what makes the numbers
#: line up with pypistats.org.
MIRRORS = ("bandersnatch", "z3c.pypimirror", "artifactory", "devpi")
_MIRRORS_SQL = "(" + ", ".join(f"'{name}'" for name in MIRRORS) + ")"

#: pypistats reports unknown values as the string "null" and folds every
#: operating system it does not track into "other".
#:
#: Note that the two category tables carry no installer dimension, so unlike
#: ``recent`` and ``overall`` these series include mirror downloads. ClickPy has
#: no (installer x python/system) aggregate, and reading the detail table would
#: hit the public instance's read quota; the difference is a fraction of a
#: percent for packages mirrors do not sync heavily.
NULL_CATEGORY = "null"
OTHER_CATEGORY = "other"
KNOWN_SYSTEMS = frozenset({"Linux", "Windows", "Darwin"})

#: The window is anchored on the newest date the dataset has rather than on
#: ``today()``: ClickPy is updated once a day, so the two can differ.
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


PYTHON_MINOR_SQL = _category_sql(
    "pypi.pypi_downloads_per_day_by_version_by_python", "python_minor"
)
SYSTEM_SQL = _category_sql("pypi.pypi_downloads_per_day_by_version_by_system", "system")


def normalize_project(name: str) -> str:
    """PEP 503 normalisation.

    ClickHouse stores normalised project names: ``SQLAlchemy`` and
    ``ruamel.yaml`` match nothing, ``sqlalchemy`` and ``ruamel-yaml`` do.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def shape_recent(package: str, rows: list[dict[str, Any]]) -> dict | None:
    """Shape the one-row ``recent`` aggregate, or ``None`` for a package with no
    download records at all -- the row is all zeros in that case, so the query
    reports how many rows it saw to tell the two apart."""
    if not rows or not rows[0]["rows_seen"]:
        return None
    row = rows[0]
    return {
        "data": {
            "last_day": row["last_day"],
            "last_month": row["last_month"],
            "last_week": row["last_week"],
        },
        "package": package,
        "type": "recent_downloads",
    }


def shape_overall(package: str, rows: list[dict[str, Any]]) -> dict | None:
    """Shape the daily time series into pypistats' two mirror categories."""
    if not rows:
        return None
    data: list[dict[str, Any]] = []
    for row in rows:
        date = str(row["date"])
        data.append(
            {
                "category": "with_mirrors",
                "date": date,
                "downloads": row["with_mirrors"],
            }
        )
        data.append(
            {
                "category": "without_mirrors",
                "date": date,
                "downloads": row["without_mirrors"],
            }
        )
    return {"data": data, "package": package, "type": "overall_downloads"}


def shape_category(
    package: str,
    rows: list[dict[str, Any]],
    *,
    type_: str,
    rename: Callable[[str], str],
) -> dict | None:
    """Shape a daily ``(date, category, downloads)`` series."""
    if not rows:
        return None
    return {
        "data": [
            {
                "category": rename(row["category"]),
                "date": str(row["date"]),
                "downloads": row["downloads"],
            }
            for row in rows
        ],
        "package": package,
        "type": type_,
    }


def _python_category(value: str) -> str:
    return value or NULL_CATEGORY


def _system_category(value: str) -> str:
    if not value:
        return NULL_CATEGORY
    return value if value in KNOWN_SYSTEMS else OTHER_CATEGORY


@dataclass(frozen=True)
class Spec:
    """A supported dimension: its query and its response shaper."""

    sql: str
    shape: Callable[[str, list[dict[str, Any]]], dict | None]


SPECS: dict[str, Spec] = {
    "recent": Spec(RECENT_SQL, shape_recent),
    "overall": Spec(OVERALL_SQL, shape_overall),
    "python_minor": Spec(
        PYTHON_MINOR_SQL,
        lambda package, rows: shape_category(
            package, rows, type_="python_minor_downloads", rename=_python_category
        ),
    ),
    "system": Spec(
        SYSTEM_SQL,
        lambda package, rows: shape_category(
            package, rows, type_="system_downloads", rename=_system_category
        ),
    ),
}


@alru_cache(maxsize=256, ttl=CACHE_TTL)
async def fetch(package: str, dimension: str) -> dict | None:
    """Run one dimension's query, shape the rows for the API, and memoize it.

    Cached because ClickPy is refreshed about once a day: how long an answer stays
    fresh is a property of the data source. ``None`` means the package has no
    download records at all, which the route turns into a 404. The caller is
    expected to have checked ``dimension`` against ``SPECS``.
    """
    spec = SPECS[dimension]
    rows = await clickhouse.execute(spec.sql, {"package": normalize_project(package)})
    return spec.shape(package, rows)
