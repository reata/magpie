from collections import Counter
from datetime import datetime

import httpx
import uvicorn
from dateutil.rrule import rrule, DAILY
from fastapi import FastAPI
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from pandas import DataFrame

from magpie.settings import GITHUB_ACCESS_TOKEN


app = FastAPI()

origins = ["http://localhost:8000", "https://reata.github.io"]

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


@app.get("/api/pypistats/{pypistats_path:path}")
async def pypistats(pypistats_path: str):
    async with httpx.AsyncClient() as client:
        proxy: httpx.Response = await client.get(
            f"https://pypistats.org/{pypistats_path}"
        )
    return Response(
        content=proxy.content,
        status_code=proxy.status_code,
        media_type="application/json",
    )


@app.get("/api/github/{github_path:path}")
async def github(github_path: str):
    async with httpx.AsyncClient() as client:
        proxy: httpx.Response = await client.get(
            f"https://api.github.com/{github_path}",
            headers={"Authorization": f"token {GITHUB_ACCESS_TOKEN}"},
        )
    return Response(
        content=proxy.content,
        status_code=proxy.status_code,
        media_type="application/json",
    )


@app.get("/api/starhistory/{repo:path}")
async def starhistory(repo: str):
    per_page = 100
    page = 1
    star_ts = []
    async with httpx.AsyncClient() as client:
        while True:
            proxy: httpx.Response = await client.get(
                f"https://api.github.com/repos/{repo}/stargazers?per_page={per_page}&page={page}",
                headers={
                    "Accept": "application/vnd.github.v3.star+json",
                    "Authorization": f"token {GITHUB_ACCESS_TOKEN}",
                },
            )
            ts = [stargazer["starred_at"] for stargazer in proxy.json()]
            star_ts.extend(ts)
            page += 1
            if len(ts) < per_page:
                break
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


if __name__ == "__main__":
    uvicorn.run(f"{__name__}:app", port=8081, reload=True)
