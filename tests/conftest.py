"""Shared pytest fixtures."""

import pytest
from fastapi.testclient import TestClient

import magpie.main as main
from magpie import services
from magpie.services import clickpy, starhistory

# The startup warm-up would race the per-test fakes; the caches are exercised directly in the tests instead.
services.PREWARM_ENABLED = False


@pytest.fixture(scope="session")
def client():
    """One ``TestClient`` -- and therefore one event loop -- per session.

    ``alru_cache`` clears its entries (with a warning) when it is used from a different event loop than the one that
    first ran it, so sharing a single loop keeps the cache observable across a test. Using the client as a context
    manager also runs the app lifespan, closing the shared httpx client on teardown.
    """
    with TestClient(main.app) as client:
        yield client


@pytest.fixture(autouse=True)
def clear_caches():
    """Start every test with empty caches, so nothing an earlier test cached can leak in."""
    for cache in (
        clickpy.fetch_recent,
        clickpy.fetch_series,
        starhistory.star_history,
    ):
        cache.cache_clear()
