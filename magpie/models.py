from pydantic import BaseModel


class Directory(BaseModel):
    f: str | None = None
    d: str | None = None


class SQLLineageInput(BaseModel):
    f: str | None = None
    e: str | None = None
