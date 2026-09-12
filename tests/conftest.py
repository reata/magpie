"""Shared pytest fixtures."""

import pytest
from fastapi.testclient import TestClient

import magpie.main as main


@pytest.fixture(scope="session")
def client():
    """One ``TestClient`` -- and therefore one event loop -- per session.

    ``alru_cache`` clears its entries (with a warning) when it is used from a
    different event loop than the one that first ran it, so sharing a single loop
    keeps the cache observable across a test. Using the client as a context
    manager also runs the app lifespan, closing the shared httpx client on
    teardown.
    """
    with TestClient(main.app) as client:
        yield client


@pytest.fixture(autouse=True)
def clear_caches():
    """Keep the process-wide caches from leaking between tests."""
    main.pypistats.cache_clear()
    main.starhistory.cache_clear()
    yield
    main.pypistats.cache_clear()
    main.starhistory.cache_clear()
