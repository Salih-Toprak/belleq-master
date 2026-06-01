"""MCP connector domain models (dataclasses, no persistence logic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Supported upstream transports. http/sse connect to a remote URL; stdio
# spawns a local process on the environment host.
VALID_TRANSPORTS = frozenset({"streamable_http", "sse", "stdio"})


@dataclass
class MCPConnectorRecord:
    """A registered upstream MCP server.

    For ``streamable_http`` / ``sse`` transports, ``url`` is required and
    ``headers`` carries auth (e.g. ``{"Authorization": "Bearer ..."}``).
    For ``stdio`` transport, ``command`` + ``args`` describe the process to
    spawn and ``env`` carries its environment (also stored encrypted).
    """

    connector_id: str
    display_name: str
    transport: str = "streamable_http"  # streamable_http | sse | stdio
    url: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    # Secret material (auth headers for http/sse, env for stdio). Stored
    # encrypted at rest via credential_crypto, redacted on read APIs.
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    added_at: datetime | None = None
    updated_at: datetime | None = None
    # Last connection-test result, for dashboard display.
    last_status: str = "unknown"  # unknown | ok | error
    last_error: str = ""
    last_checked_at: datetime | None = None
    tool_count: int = 0
    # OAuth: "none" (no auth needed), "pending" (authorize started),
    # "connected" (have a token), "error".
    auth_status: str = "none"
    # OAuth material (tokens + client registration), stored encrypted at rest.
    # e.g. {access_token, refresh_token, expires_at, token_endpoint, client_id, ...}
    oauth: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class ContainerConnectorLink:
    """One row of the per-container connector whitelist."""

    container_id: str
    connector_id: str
    added_at: datetime | None = None
