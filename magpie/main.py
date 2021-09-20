import httpx
import uvicorn
from fastapi import FastAPI
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

origins = [
    "http://localhost:8000",
    "https://reata.github.io"
]

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
        proxy: httpx.Response = await client.get(f"https://pypistats.org/{pypistats_path}")
    return Response(content=proxy.content, status_code=proxy.status_code, media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(f"{__name__}:app", port=8081, reload=True)
