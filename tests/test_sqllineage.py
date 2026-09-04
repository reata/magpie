"""Tests for the ``/api/sqllineage/*`` endpoints.

These endpoints proxy sqllineage's drawing controllers.
"""

from pathlib import Path

from fastapi.testclient import TestClient
from sqllineage.config import SQLLineageConfig

from magpie.main import app

client = TestClient(app)

DATA_ROOT = Path(SQLLineageConfig.DIRECTORY)


def test_script_returns_inline_sql_from_e():
    response = client.post("/api/sqllineage/script", json={"e": "SELECT 1"})

    assert response.status_code == 200
    assert response.json() == {"content": "SELECT 1"}


def test_script_with_empty_payload_returns_empty_content():
    response = client.post("/api/sqllineage/script", json={})

    assert response.status_code == 200
    assert response.json() == {"content": ""}


def test_lineage_returns_verbose_dag_and_column():
    response = client.post(
        "/api/sqllineage/lineage",
        json={"e": "INSERT INTO tab_x SELECT * FROM tab_y"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"verbose", "dag", "column"}

    verbose = body["verbose"]
    assert "<default>.tab_x" in verbose
    assert "<default>.tab_y" in verbose

    dag_ids = {node["data"]["id"] for node in body["dag"]}
    assert {"<default>.tab_x", "<default>.tab_y"} <= dag_ids

    column_ids = {node["data"]["id"] for node in body["column"]}
    assert "<default>.tab_x.*" in column_ids


def test_lineage_respects_dialect():
    response = client.post(
        "/api/sqllineage/lineage",
        json={"e": "SELECT col_a FROM tab_src", "dialect": "ansi"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "tab_src" in body["verbose"]
    assert any(node["data"]["id"] == "<default>.tab_src" for node in body["dag"])


def test_directory_defaults_to_sqllineage_data_root():
    response = client.post("/api/sqllineage/directory", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(DATA_ROOT)
    assert body["name"] == DATA_ROOT.name
    assert body["is_dir"] is True
    assert any(
        child["name"] == "tpcds" and child["is_dir"] for child in body["children"]
    )


def test_directory_lists_requested_dir_children():
    tpcds = DATA_ROOT / "tpcds"
    response = client.post("/api/sqllineage/directory", json={"d": str(tpcds)})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(tpcds)
    assert body["name"] == tpcds.name
    assert body["is_dir"] is True

    expected = {p.name: p for p in sorted(tpcds.iterdir())}
    children = {child["name"]: child for child in body["children"]}
    assert set(children) == set(expected)
    for name, child in children.items():
        assert child["id"] == str(expected[name])
        assert child["is_dir"] == expected[name].is_dir()


def test_directory_accepts_file_path_and_lists_its_parent():
    query_file = DATA_ROOT / "tpcds" / "query01.sql"
    response = client.post("/api/sqllineage/directory", json={"f": str(query_file)})

    assert response.status_code == 200
    assert response.json()["id"] == str(query_file.parent)


def test_directory_rejects_path_outside_data_root():
    # The mounted WSGI application guards ``d``/``f`` against
    # SQLLineageConfig.DIRECTORY inside its own __call__, so an arbitrary
    # directory is not listable.
    response = client.post("/api/sqllineage/directory", json={"d": "."})

    assert response.status_code == 403
    assert response.json() == {"message": "File Not Allowed For Accessing"}
