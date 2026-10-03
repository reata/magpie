"""The root route: service metadata and links to this documentation."""

from fastapi import APIRouter

from magpie.routers import docs_tag

TAG = docs_tag(__name__)

router = APIRouter(tags=[TAG])


@router.get("/")
async def root():
    # Relative links: the service answers on a different host per environment.
    return {
        "message": "Hello World from magpie",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }
