"""`/api/clickpy`: PyPI download statistics straight from ClickHouse.

Serves the dashboard's download charts from ClickPy's public dataset, in the JSON shapes, the 180 day window and the
category names of the pypistats.org API (see https://pypistats.org/api/).

The routes only map HTTP: which dimensions exist and what a missing package means. The queries, their cache and the
warm-up live in ``magpie.services.clickpy``.
"""

from fastapi import APIRouter, HTTPException

from magpie.routers import docs_tag
from magpie.services import clickpy

TAG = docs_tag(__name__)

router = APIRouter(prefix="/api/clickpy", tags=[TAG])


# ``recent`` is declared before the series route on purpose: Starlette matches routes in registration order, so
# ``/{dimension}`` would otherwise swallow the literal ``recent`` and reject it as an unknown series.
@router.get("/{package}/recent", response_model=clickpy.RecentDownloads)
async def clickpy_recent(package: str) -> clickpy.RecentDownloads:
    """One package's download totals for the last day, week and month."""
    payload = await clickpy.fetch_recent(package=package)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no download data for {package}")
    return payload


@router.get("/{package}/{dimension}", response_model=clickpy.SeriesDownloads)
async def clickpy_series(package: str, dimension: clickpy.SeriesDimension) -> clickpy.SeriesDownloads:
    """One package's daily download series for one dimension."""
    payload = await clickpy.fetch_series(package=package, dimension=dimension)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no download data for {package}")
    return payload
