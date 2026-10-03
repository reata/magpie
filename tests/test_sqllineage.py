"""Smoke tests for the mounted ``/api/sqllineage`` application.

The controllers are sqllineage's own, with their own test suite; magpie only mounts them, so this checks no more than
that the mount is reachable and each controller answers with its envelope.
"""

import pytest


@pytest.mark.parametrize(
    ("path", "payload", "key"),
    [
        ("/api/sqllineage/script", {"e": "SELECT 1"}, "content"),
        ("/api/sqllineage/lineage", {"e": "INSERT INTO tab_x SELECT * FROM tab_y"}, "dag"),
        ("/api/sqllineage/directory", {}, "children"),
    ],
)
def test_the_mounted_controllers_answer(client, path, payload, key):
    response = client.post(path, json=payload)

    assert response.status_code == 200
    assert key in response.json()
