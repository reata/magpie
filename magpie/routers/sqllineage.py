from argparse import Namespace
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqllineage import DATA_FOLDER
from sqllineage.runner import LineageRunner
from sqllineage.utils.constant import LineageLevel
from sqllineage.utils.helpers import extract_sql_from_args

from magpie.models import Directory, SQLLineageInput

router = APIRouter(prefix="/api/sqllineage")


@router.post("/directory")
def directory(dir: Directory):
    if dir.f:
        dir_root = Path(dir.f).parent
    elif dir.d:
        dir_root = Path(dir.d)
    else:
        dir_root = Path(DATA_FOLDER)
    data = {
        "id": str(dir_root),
        "name": dir_root.name,
        "is_dir": True,
        "children": [
            {"id": str(p), "name": p.name, "is_dir": p.is_dir()}
            for p in sorted(dir_root.iterdir(), key=lambda _: (not _.is_dir(), _.name))
        ],
    }
    return JSONResponse(data)


@router.post("/script")
def script(sinput: SQLLineageInput):
    req_args = Namespace(**sinput.dict())
    sql = extract_sql_from_args(req_args)
    return JSONResponse({"content": sql})


@router.post("/lineage")
def lineage(sinput: SQLLineageInput):
    req_args = Namespace(**sinput.dict())
    sql = extract_sql_from_args(req_args)
    lr = LineageRunner(sql, verbose=True)
    return JSONResponse(
        {
            "verbose": str(lr),
            "dag": lr.to_cytoscape(),
            "column": lr.to_cytoscape(LineageLevel.COLUMN),
        }
    )
