"""``/api/pypistats`` -- pass-through to pypistats.org.

The dashboard's PyPI download charts read this, so it inherits the upstream's
180 day retention and its 30/minute rate limit.
"""

from async_lru import alru_cache
from fastapi import APIRouter, Response

from magpie.clients.upstream import UpstreamError, upstream
from magpie.routers import CACHE_TTL

router = APIRouter(prefix="/api/pypistats", tags=["pypistats"])


@router.get("/{pypistats_path:path}")
@alru_cache(maxsize=256, ttl=CACHE_TTL)
async def pypistats(pypistats_path: str):
    proxy = await upstream.get(f"https://pypistats.org/{pypistats_path}")
    if not proxy.is_success:
        # Only a 2xx may leave a cached view: anything else would be stored for
        # the whole TTL. pypistats answers errors with plain text, so a 404 body
        # would otherwise be pinned here as application/json for hours.
        raise UpstreamError(f"pypistats returned HTTP {proxy.status_code}")
    return Response(
        content=proxy.content,
        status_code=proxy.status_code,
        media_type="application/json",
    )
