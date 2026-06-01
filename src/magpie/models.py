from pydantic import BaseModel


class Directory(BaseModel):
    f: str | None = None
    d: str | None = None
    dialect: str = "non-validating"


class SQLLineageInput(BaseModel):
    f: str | None = None
    e: str | None = None
    dialect: str = "non-validating"
