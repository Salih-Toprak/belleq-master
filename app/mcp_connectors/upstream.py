"""Build FastMCP clients for upstream connectors and probe them.

Single place that knows how to turn a stored :class:`MCPConnectorRecord` into a
live MCP client transport. Auth is whatever the connector has — an OAuth bearer
token (preferred) or explicit headers. Transport is auto-detected so users
never have to choose one.
"""

from __future__ import annotations

import logging

from app.mcp_connectors.models import MCPConnectorRecord

logger = logging.getLogger(__name__)

# HTTP transports we try, in order, when auto-detecting.
_HTTP_TRANSPORTS = ("streamable_http", "sse")


def _effective_headers(record: MCPConnectorRecord) -> dict[str, str]:
    """Merge explicit headers with an OAuth bearer token if present."""
    headers = dict(record.headers or {})
    token = (record.oauth or {}).get("access_token")
    if token and "Authorization" not in headers:
        token_type = (record.oauth or {}).get("token_type") or "Bearer"
        headers["Authorization"] = f"{token_type} {token}"
    return headers


def build_client(record: MCPConnectorRecord, *, transport: str | None = None):
    """Return a configured ``fastmcp.Client`` for this connector.

    fastmcp is imported lazily so the rest of the master imports cleanly even
    where fastmcp is not installed.
    """
    from fastmcp import Client
    from fastmcp.client.transports import (
        SSETransport,
        StdioTransport,
        StreamableHttpTransport,
    )

    t = (transport or record.transport or "streamable_http").strip().lower()
    if t == "stdio":
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
        raise ValueError(f"{t} connector requires a url")
    headers = _effective_headers(record)
    if t == "sse":
        return Client(SSETransport(url=record.url, headers=headers))
    return Client(StreamableHttpTransport(url=record.url, headers=headers))


async def _list_tools_via(record: MCPConnectorRecord, transport: str) -> list[str]:
    client = build_client(record, transport=transport)
    async with client:
        tools = await client.list_tools()
    return [t.name for t in tools]


async def test_connection(record: MCPConnectorRecord) -> dict:
    """Connect, list tools, and report which transport worked.

    Returns {ok, error, tool_count, tools, transport}. Never raises — failures
    are reported in the result. For HTTP connectors the working transport is
    auto-detected (streamable_http then sse).
    """
    if (record.transport or "").lower() == "stdio":
        candidates = ["stdio"]
    else:
        # Try the recorded transport first, then the other HTTP one.
        recorded = (record.transport or "streamable_http").lower()
        candidates = [recorded] + [t for t in _HTTP_TRANSPORTS if t != recorded]

    last_error = ""
    for transport in candidates:
        try:
            names = await _list_tools_via(record, transport)
            logger.info(
                "mcp_connector_test_ok connector_id=%s transport=%s tools=%s",
                record.connector_id,
                transport,
                len(names),
            )
            return {
                "ok": True,
                "error": "",
                "tool_count": len(names),
                "tools": names,
                "transport": transport,
            }
        except Exception as e:  # noqa: BLE001 — try the next transport
            last_error = str(e)
            logger.info(
                "mcp_connector_test_transport_failed connector_id=%s transport=%s error=%s",
                record.connector_id,
                transport,
                e,
            )

    return {
        "ok": False,
        "error": last_error,
        "tool_count": 0,
        "tools": [],
        "transport": (record.transport or "streamable_http"),
    }
