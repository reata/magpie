"""Tests for the star-history service's startup warm-up.

The series itself is asserted through its endpoint, in ``tests/routers/test_github.py``.
"""

import asyncio

from magpie.errors import RemoteError
from magpie.services import starhistory


def test_prewarm_warms_the_dashboard_repository(monkeypatch):
    warmed = []

    async def fake_star_history(repo):
        warmed.append(repo)
        return []

    monkeypatch.setattr(starhistory, "star_history", fake_star_history)

    asyncio.run(starhistory.prewarm())

    assert warmed == [starhistory.PREWARM_REPO]


def test_prewarm_survives_a_failing_repository(monkeypatch):
    """GitHub being slow or unreachable must not stop the app from starting."""

    async def failing_star_history(repo):
        raise RemoteError("github returned HTTP 403")

    monkeypatch.setattr(starhistory, "star_history", failing_star_history)

    asyncio.run(starhistory.prewarm())
