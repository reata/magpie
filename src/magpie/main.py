import logging
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from typing import cast

import uvicorn
from a2wsgi import WSGIMiddleware
from async_lru import alru_cache
from dateutil.rrule import DAILY, rrule
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pandas import DataFrame
from sqllineage.drawing import app as sqllineage_app
from starlette.types import ASGIApp

from magpie.settings import GITHUB_ACCESS_TOKEN
from magpie.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger(__name__)

#: Shared, connection-pooling client that retries transient 5xx with backoff.
upstream = UpstreamClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await upstream.aclose()


app = FastAPI(lifespan=lifespan)
# Expose sqllineage's own WSGI controllers as-is
app.mount("/api/sqllineage", cast(ASGIApp, WSGIMiddleware(sqllineage_app)))

origins = ["http://localhost:3000", "http://localhost:8000", "https://reata.github.io"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Hello World from magpie"}


@app.exception_handler(UpstreamError)
async def upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
    """Turn a failed upstream call into a 503 for the client.

    Views raise instead of returning an error ``Response``: anything a cached
    view *returns* is stored as a successful result for the whole TTL, so one
    429 -- or the plain-text "404" pypistats serves -- would pin the endpoint to
    that error for hours. Raising keeps every failure out of the cache.
    """
    logger.warning("upstream call failed: %s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.get("/api/pypistats/{pypistats_path:path}")
@alru_cache(maxsize=256, ttl=6 * 60 * 60)
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


@app.get("/api/github/{github_path:path}")
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


@app.get("/api/starhistory/{repo:path}")
@alru_cache(maxsize=64, ttl=6 * 60 * 60)
async def starhistory(repo: str):
    per_page = 100
    page = 1
    star_ts = []
    while True:
        proxy = await upstream.get(
            f"https://api.github.com/repos/{repo}/stargazers?per_page={per_page}&page={page}",
            headers={
                "Accept": "application/vnd.github.v3.star+json",
                "Authorization": f"token {GITHUB_ACCESS_TOKEN}",
            },
        )
        if not proxy.is_success:
            raise UpstreamError(f"github returned HTTP {proxy.status_code}")
        ts = [stargazer["starred_at"] for stargazer in proxy.json()]
        star_ts.extend(ts)
        page += 1
        if len(ts) < per_page:
            break
    if not star_ts:
        # A repository with no stargazers: nothing to aggregate, and the date
        # range below needs at least one date to anchor on.
        return Response(content=b"[]", media_type="application/json")
    star_dt = [datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").date() for ts in star_ts]
    all_dt = list(
        dt.date() for dt in rrule(DAILY, dtstart=star_dt[0], until=star_dt[-1])
    )
    no_star_dt = list(set(all_dt).difference(set(star_dt)))
    df = DataFrame.from_records(
        [(k, v) for k, v in Counter(star_dt).items()] + [(k, 0) for k in no_star_dt]
    )
    df.columns = ["date", "star_cnt"]
    df = df.sort_values("date")
    df["date"] = df["date"].apply(str)
    df["star_cum_cnt"] = df["star_cnt"].cumsum()
    return Response(content=df.to_json(orient="records"), media_type="application/json")


def dev():
    """Start the development server with hot reload."""
    uvicorn.run("magpie.main:app", port=8081, reload=True)


if __name__ == "__main__":
    dev()
