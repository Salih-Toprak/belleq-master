"""Build FastMCP clients for upstream connectors and probe them.

This is the single place that knows how to turn a stored
:class:`MCPConnectorRecord` into a live MCP client transport. Both the
``/test`` endpoint and the per-container aggregator reuse it so transport
handling stays in one spot.
"""

from __future__ import annotations

import logging

from app.mcp_connectors.models import MCPConnectorRecord

logger = logging.getLogger(__name__)


def build_client(record: MCPConnectorRecord):
    """Return a configured ``fastmcp.Client`` for this connector.

    Imports fastmcp lazily so the rest of the master still imports cleanly
    in environments where fastmcp is not installed.
    """
    from fastmcp import Client
    from fastmcp.client.transports import (
        SSETransport,
        StdioTransport,
        StreamableHttpTransport,
    )

    transport = (record.transport or "streamable_http").strip().lower()
    if transport == "stdio":
        if not record.command:
            raise ValueError("stdio connector requires a command")
        return Client(
            StdioTransport(
                command=record.command,
                args=list(record.args or []),
                env=dict(record.env or {}),
            )
        )

    if not record.url:
        raise ValueError(f"{transport} connector requires a url")
    headers = dict(record.headers or {})
    if transport == "sse":
        return Client(SSETransport(url=record.url, headers=headers))
    # default: streamable_http
    return Client(StreamableHttpTransport(url=record.url, headers=headers))


async def test_connection(record: MCPConnectorRecord) -> dict:
    """Connect to the upstream, list tools, and return a result summary.

    Returns ``{"ok": bool, "error": str, "tool_count": int, "tools": [name]}``.
    Never raises — connection failures are reported in the result.
    """
    try:
        client = build_client(record)
    except Exception as e:  # noqa: BLE001 — config error, report it
        return {"ok": False, "error": str(e), "tool_count": 0, "tools": []}

    try:
        async with client:
            tools = await client.list_tools()
        names = [t.name for t in tools]
        logger.info(
            "mcp_connector_test_ok connector_id=%s tools=%s",
            record.connector_id,
            len(names),
        )
        return {"ok": True, "error": "", "tool_count": len(names), "tools": names}
    except Exception as e:  # noqa: BLE001 — upstream/network failure
        logger.warning(
            "mcp_connector_test_failed connector_id=%s error=%s",
            record.connector_id,
            e,
        )
        return {"ok": False, "error": str(e), "tool_count": 0, "tools": []}
