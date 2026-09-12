"""Tests for the ClickHouse client: the rows it returns and the error it raises."""

import asyncio

import pytest

from magpie.clients import clickhouse
from magpie.errors import RemoteError


class _Result:
    def named_results(self):
        return [{"downloads": 7}]


class _Client:
    """Minimal stand-in for the clickhouse-connect async client."""

    def __init__(self, error=None):
        self._error = error

    async def query(self, query, parameters=None):
        if self._error is not None:
            raise self._error
        return _Result()


def _use_client(monkeypatch, client):
    async def fake_get_client():
        return client

    monkeypatch.setattr(clickhouse, "get_client", fake_get_client)


def test_execute_returns_named_rows(monkeypatch):
    _use_client(monkeypatch, _Client())

    assert asyncio.run(clickhouse.execute("SELECT 1")) == [{"downloads": 7}]


def test_driver_failures_become_remote_errors(monkeypatch):
    """The API turns a RemoteError into a 503, so every driver failure has to
    reach it as one instead of leaking a driver-specific exception."""
    _use_client(monkeypatch, _Client(error=RuntimeError("boom")))

    with pytest.raises(RemoteError, match="clickhouse query failed: boom"):
        asyncio.run(clickhouse.execute("SELECT 1"))


def test_remote_errors_are_not_rewrapped(monkeypatch):
    _use_client(monkeypatch, _Client(error=RemoteError("clickhouse query failed: x")))

    with pytest.raises(RemoteError, match="^clickhouse query failed: x$"):
        asyncio.run(clickhouse.execute("SELECT 1"))
