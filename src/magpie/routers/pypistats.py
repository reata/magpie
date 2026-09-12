"""``/api/pypistats`` -- pass-through to pypistats.org.

The dashboard still reads its download charts from here; it stays until they read
``/api/clickpy`` instead, which serves the same JSON without the 180 day retention
window or the 30/minute rate limit.
"""

from async_lru import alru_cache
from fastapi import APIRouter, Response

from magpie.clients.http import http
from magpie.errors import RemoteError

router = APIRouter(prefix="/api/pypistats", tags=["pypistats"])


#: Cached on the route: this proxy has no service layer, and it forwards the
#: upstream body verbatim.
@router.get("/{pypistats_path:path}")
@alru_cache(maxsize=256, ttl=6 * 60 * 60)
async def pypistats(pypistats_path: str):
    proxy = await http.get(f"https://pypistats.org/{pypistats_path}")
    if not proxy.is_success:
        # Only a 2xx may leave a cached view: anything else would be stored for
        # the whole TTL. pypistats answers errors with plain text, so a 404 body
        # would otherwise be pinned here as application/json for hours.
        raise RemoteError(f"pypistats returned HTTP {proxy.status_code}")
    return Response(
        content=proxy.content,
        status_code=proxy.status_code,
        media_type="application/json",
    )
