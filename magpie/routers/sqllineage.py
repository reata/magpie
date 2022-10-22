from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqllineage.drawing import (
    directory as o_directory,
    script as o_script,
    lineage as o_lineage,
)

from magpie.models import Directory, SQLLineageInput

router = APIRouter(prefix="/api/sqllineage")


@router.post("/directory")
def directory(_dir: Directory):
    data = o_directory(_dir.dict())
    return JSONResponse(data)


@router.post("/script")
def script(sinput: SQLLineageInput):
    data = o_script(sinput.dict())
    return JSONResponse(data)


@router.post("/lineage")
def lineage(sinput: SQLLineageInput):
    data = o_lineage(sinput.dict())
    return JSONResponse(data)
