"""`/api/clickpy`: PyPI download statistics straight from ClickHouse.

Serves the dashboard's download charts from ClickPy's public dataset, in the JSON
shapes, the 180 day window and the category names of the pypistats.org API (see
https://pypistats.org/api/).

The route only maps HTTP: which dimensions exist and what a missing package
means. The queries, their cache and the warm-up live in
``magpie.services.clickpy``.
"""

from fastapi import APIRouter, HTTPException

from magpie.routers import docs_tag
from magpie.services import clickpy

TAG = docs_tag(__name__)

router = APIRouter(prefix="/api/clickpy", tags=[TAG])


@router.get("/{package}/{dimension}")
async def clickpy_stats(package: str, dimension: clickpy.Dimension):
    """One dimension of one package's download statistics."""
    payload = await clickpy.fetch(package=package, dimension=dimension)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no download data for {package}")
    return payload
