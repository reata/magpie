"""Tests for the ClickPy service's startup warm-up.

Everything a client can see -- the response envelopes, the window and dimension validation, the cache, the OpenAPI
contract -- is asserted through its endpoints, in ``tests/routers/test_downloads.py``.
"""

import asyncio

from magpie.errors import RemoteError
from magpie.services import clickpy
from magpie.services.clickpy import DEFAULT_WINDOW, SeriesDimension


def test_prewarm_queries_recent_and_every_series(monkeypatch):
    """The charts the dashboard opens with are the ones warmed, so the first page view is not the slow one."""
    calls = []

    # Keyword-only: the warm-up must key the caches exactly as the routes do.
    async def fake_recent(*, package):
        calls.append(("recent", package))

    async def fake_series(*, package, dimension, window):
        calls.append((dimension, window, package))

    monkeypatch.setattr(clickpy, "fetch_recent", fake_recent)
    monkeypatch.setattr(clickpy, "fetch_series", fake_series)

    asyncio.run(clickpy.prewarm())

    assert calls == [
        ("recent", clickpy.PREWARM_PACKAGE),
        *((dimension, DEFAULT_WINDOW, clickpy.PREWARM_PACKAGE) for dimension in SeriesDimension),
    ]


def test_prewarm_survives_a_failing_series(monkeypatch):
    """A cold ClickHouse must not stop the app from starting, nor keep the remaining warm-ups from running."""
    calls = []

    async def fake_recent(*, package):
        calls.append("recent")

    async def flaky_series(*, package, dimension, window):
        calls.append(dimension)
        if dimension is SeriesDimension.OVERALL:
            raise RemoteError("clickhouse query failed: boom")

    monkeypatch.setattr(clickpy, "fetch_recent", fake_recent)
    monkeypatch.setattr(clickpy, "fetch_series", flaky_series)

    asyncio.run(clickpy.prewarm())

    assert calls == ["recent", *SeriesDimension]
