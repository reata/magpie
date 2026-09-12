"""Shared ClickHouse client for the download statistics endpoints.

One lazily created ``AsyncClient`` is reused across requests -- the driver pools
its connections underneath -- and every driver failure becomes a ``RemoteError``,
which is what the API maps to a 503.
"""

import logging
from typing import Any

import clickhouse_connect

from magpie.errors import RemoteError
from magpie.settings import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_SECURE,
    CLICKHOUSE_USER,
)

logger = logging.getLogger(__name__)

#: Seconds to wait for a query, matching the HTTP client's budget.
QUERY_TIMEOUT = 10

_client: Any = None


def _connect_args() -> dict[str, Any]:
    return {
        "host": CLICKHOUSE_HOST,
        "port": int(CLICKHOUSE_PORT),
        "username": CLICKHOUSE_USER,
        "password": CLICKHOUSE_PASSWORD,
        "database": CLICKHOUSE_DATABASE,
        "secure": str(CLICKHOUSE_SECURE).lower() == "true",
        "send_receive_timeout": QUERY_TIMEOUT,
    }


async def get_client() -> Any:
    """Return the shared async client, creating it on first use."""
    global _client
    if _client is None:
        args = _connect_args()
        logger.info("connecting to clickhouse at %s:%s", args["host"], args["port"])
        _client = await clickhouse_connect.get_async_client(**args)
    return _client


async def aclose() -> None:
    """Close the shared client (called from the FastAPI lifespan)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def execute(
    query: str, parameters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Run ``query`` and return its rows as dicts."""
    try:
        client = await get_client()
        result = await client.query(query, parameters=parameters or {})
        return list(result.named_results())
    except RemoteError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the driver raises a broad set
        raise RemoteError(f"clickhouse query failed: {exc}") from exc
