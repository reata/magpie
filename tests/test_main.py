"""Tests for the application itself: its metadata and its docs."""


def test_docs_and_schema_are_served(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200

    schema = client.get("/openapi.json").json()

    assert schema["info"]["title"] == "magpie"
    assert schema["info"]["version"]  # read from the installed distribution
    assert schema["info"]["description"]  # the README, likewise
    assert [tag["name"] for tag in schema["tags"]] == ["meta", "downloads", "github"]
