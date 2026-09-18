"""``/api/clickpy`` -- download statistics straight from ClickHouse.

Serves the dashboard's download charts from ClickPy's public dataset, in the JSON
shapes, the 180 day window and the category names of the pypistats.org API (see
https://pypistats.org/api/).

The route only maps HTTP: which dimensions exist and what a missing package
means. The queries, their cache and the warm-up live in
``magpie.services.clickpy``.
"""

import logging

from fastapi import APIRouter, HTTPException

from magpie.services import clickpy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clickpy", tags=["downloads"])

#: The dashboard's package, fetched once at startup so its first visitor does not
#: pay for the ClickHouse round trip -- a cold query takes a couple of seconds.
#: Tests turn this off, since their lifespan would race the per-test fakes.
PREWARM_ENABLED: bool = True
PREWARM_PACKAGE = "sqllineage"


@router.get("/{package}/{dimension}")
async def clickpy_stats(package: str, dimension: str):
    """One dimension of one package's download statistics."""
    if dimension not in clickpy.SPECS:
        raise HTTPException(
            status_code=404, detail=f"unsupported dimension: {dimension}"
        )
    payload = await clickpy.fetch(package=package, dimension=dimension)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no download data for {package}")
    return payload


async def prewarm() -> None:
    """Fill the cache for every dimension of the dashboard package.

    Best effort: a cold or unreachable ClickHouse must not keep the app from
    starting, and a later request just pays for its own query instead.
    """
    for dimension in clickpy.SPECS:
        try:
            # Keyword arguments on purpose: alru_cache keys positional and
            # keyword calls separately, and the route calls fetch with keywords.
            await clickpy.fetch(package=PREWARM_PACKAGE, dimension=dimension)
        except Exception:  # noqa: BLE001 -- any query failure is non-fatal here
            logger.warning(
                "prewarm failed for %s/%s", PREWARM_PACKAGE, dimension, exc_info=True
            )
