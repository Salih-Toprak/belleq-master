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


def build_client(record: MCPConnectorRecord, *, transport: str | None = None):
    """Return a configured ``fastmcp.Client`` for this connector.

    In FastMCP 3.x, pass OAuth tokens via ``auth=<token_string>`` rather than
    injecting an Authorization header manually. FastMCP treats a plain string
    as a Bearer token and handles 401 retries correctly; manually-set headers
    can be overridden by FastMCP's own auth flow.
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

    # Use the OAuth access token via FastMCP's `auth=` parameter (bearer string).
    # Only fall back to explicit headers for non-OAuth connectors.
    access_token = (record.oauth or {}).get("access_token")
    extra_headers = dict(record.headers or {})

    if t == "sse":
        return Client(
            SSETransport(
                url=record.url,
                headers=extra_headers or None,
                auth=access_token or None,
            )
        )
    return Client(
        StreamableHttpTransport(
            url=record.url,
            headers=extra_headers or None,
            auth=access_token or None,
        )
    )


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
