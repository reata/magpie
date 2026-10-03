"""Tests for the root route: the service's own name and the links to its documentation."""


def test_root_advertises_the_documentation(client):
    assert client.get("/").json() == {
        "message": "Hello World from magpie",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }
