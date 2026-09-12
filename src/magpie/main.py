"""Application assembly: middleware, shared error handling, and route wiring.

Routes live in ``magpie.routers`` and everything that talks to another service
lives in ``magpie.clients``; this module only puts them together plus the
startup/shutdown hooks.
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import cast

import uvicorn
from a2wsgi import WSGIMiddleware
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqllineage.drawing import app as sqllineage_app
from starlette.types import ASGIApp

from magpie.clients import clickhouse
from magpie.clients.http import http
from magpie.errors import RemoteError
from magpie.routers import downloads, github, pypistats

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = (
        asyncio.create_task(downloads.prewarm()) if downloads.PREWARM_ENABLED else None
    )
    yield
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await http.aclose()
    await clickhouse.aclose()


app = FastAPI(lifespan=lifespan)

origins = ["http://localhost:3000", "http://localhost:8000", "https://reata.github.io"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(downloads.router)
app.include_router(github.router)
app.include_router(pypistats.router)

# Mounted after the routers on purpose: Starlette matches in registration order
# and a Mount handles everything under its prefix, so any route declared below
# /api/sqllineage would never be reached. sqllineage ships its own WSGI
# controllers, hence the mount rather than re-declared routes.
app.mount("/api/sqllineage", cast(ASGIApp, WSGIMiddleware(sqllineage_app)))


@app.get("/")
async def root():
    return {"message": "Hello World from magpie"}


@app.exception_handler(RemoteError)
async def remote_error(request: Request, exc: RemoteError) -> JSONResponse:
    """Turn a failed remote call into a 503 for the client.

    Views raise instead of returning an error ``Response``: anything a cached
    view *returns* is stored as a successful result for the whole TTL, so one
    429 -- or the plain-text "404" pypistats serves -- would pin the endpoint to
    that error for hours. Raising keeps every failure out of the cache.
    """
    logger.warning("remote call failed: %s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=503)


def dev():
    """Start the development server with hot reload."""
    uvicorn.run("magpie.main:app", port=8081, reload=True)


if __name__ == "__main__":
    dev()
