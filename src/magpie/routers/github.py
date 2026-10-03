"""`/api/github` and `/api/starhistory`: GitHub repository data and star history."""

from fastapi import APIRouter, Response

from magpie.clients.http import http
from magpie.routers import docs_tag
from magpie.services.starhistory import star_history
from magpie.settings import GITHUB_ACCESS_TOKEN

TAG = docs_tag(__name__)

router = APIRouter(prefix="/api", tags=[TAG])


@router.get("/github/{github_path:path}")
async def github(github_path: str):
    proxy = await http.get(
        f"https://api.github.com/{github_path}",
        headers={"Authorization": f"token {GITHUB_ACCESS_TOKEN}"},
    )
    return Response(
        content=proxy.content,
        status_code=proxy.status_code,
        media_type="application/json",
    )


@router.get("/starhistory/{repo:path}")
async def starhistory(repo: str):
    return await star_history(repo)
