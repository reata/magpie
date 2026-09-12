"""``/api/github`` and ``/api/starhistory`` -- the two GitHub-backed routes."""

from async_lru import alru_cache
from fastapi import APIRouter, Response

from magpie.clients.upstream import upstream
from magpie.routers import CACHE_TTL
from magpie.services.starhistory import star_history
from magpie.settings import GITHUB_ACCESS_TOKEN

router = APIRouter(prefix="/api", tags=["github"])


@router.get("/github/{github_path:path}")
async def github(github_path: str):
    proxy = await upstream.get(
        f"https://api.github.com/{github_path}",
        headers={"Authorization": f"token {GITHUB_ACCESS_TOKEN}"},
    )
    return Response(
        content=proxy.content,
        status_code=proxy.status_code,
        media_type="application/json",
    )


@router.get("/starhistory/{repo:path}")
@alru_cache(maxsize=64, ttl=CACHE_TTL)
async def starhistory(repo: str):
    return await star_history(repo)
