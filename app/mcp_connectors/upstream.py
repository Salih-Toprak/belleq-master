"""Build FastMCP clients for upstream connectors and probe them.

Single place that knows how to turn a stored :class:`MCPConnectorRecord` into a
live MCP client transport. Auth is whatever the connector has — an OAuth bearer
token (preferred) or explicit headers. Transport is auto-detected so users
never have to choose one.
"""

from __future__ import annotations

import logging
import os
import time

from app.mcp_connectors.models import MCPConnectorRecord

logger = logging.getLogger(__name__)

# HTTP transports we try, in order, when auto-detecting.
_HTTP_TRANSPORTS = ("streamable_http", "sse")

# Refresh a token 60 seconds before it actually expires.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0


# ── stdio policy ─────────────────────────────────────────────────────────────
# A stdio connector is a command executed ON THE MASTER HOST, so an arbitrary
# command from an API caller would be remote code execution with the master's
# privileges (and its secrets). stdio is therefore reserved for belleq's OWN
# bundled servers: an explicit allowlist, checked both when a connector is
# stored and again right before launch. Third-party servers use http/sse.

ALLOWED_STDIO_COMMANDS = frozenset({"python", "python3"})

ALLOWED_STDIO_MODULES = frozenset({
    "app.mcp_servers.telegram_server",
    "app.mcp_servers.gmail_server",
    "app.mcp_servers.google_calendar_server",
})

# Host env a bundled server needs to run at all (find the interpreter, imports).
# NOT the master's full environment — that would hand every connector the
# admin key, the credential-encryption key, and the platform OAuth secrets.
_ENV_PASSTHROUGH = (
    "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONUNBUFFERED",
    "HOME", "LANG", "LC_ALL", "TZ",
)

# Platform credentials handed to specific bundled servers only.
_ENV_FOR_MODULE: dict[str, tuple[str, ...]] = {
    "app.mcp_servers.gmail_server": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    "app.mcp_servers.google_calendar_server": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
}


def stdio_module(command: str, args: list[str] | None) -> str:
    """The bundled module a stdio spec runs, or "" if it isn't an allowed one.

    Only the exact shape ``python -m <allowlisted module>`` is accepted — no
    shell, no extra arguments, no arbitrary scripts.
    """
    argv = [str(a) for a in (args or [])]
    if (command or "").strip() not in ALLOWED_STDIO_COMMANDS:
        return ""
    if len(argv) != 2 or argv[0] != "-m":
        return ""
    return argv[1] if argv[1] in ALLOWED_STDIO_MODULES else ""


def validate_stdio(command: str, args: list[str] | None) -> str:
    """Return an error message for a rejected stdio spec, or "" when allowed."""
    if stdio_module(command, args):
        return ""
    return (
        "stdio connectors are reserved for belleq's built-in servers. "
        "Add third-party MCP servers by URL (http/sse) instead."
    )


def _stdio_env(module: str, record_env: dict[str, str] | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    for k in _ENV_FOR_MODULE.get(module, ()):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update(record_env or {})
    return env


async def ensure_fresh_token(
    record: MCPConnectorRecord,
    *,
    registry=None,
) -> MCPConnectorRecord:
    """If the connector uses OAuth and the token is expired (or about to
    expire), refresh it and persist the new tokens.

    Returns the (possibly updated) record. If refresh fails or no refresh
    token is available, returns the original record unchanged and logs a
    warning — the caller can still try with the stale token.
    """
    oauth = record.oauth or {}
    access_token = oauth.get("access_token")
    if not access_token:
        return record  # no OAuth in use

    expires_at = oauth.get("expires_at", 0.0)
    if not expires_at:
        return record  # no expiry recorded — assume still valid

    if time.time() < float(expires_at) - _TOKEN_REFRESH_MARGIN_SECONDS:
        return record  # token is still fresh

    # Token is expired or about to expire — try to refresh.
    refresh_token = oauth.get("refresh_token")
    token_endpoint = oauth.get("token_endpoint")
    client_id = oauth.get("client_id")
    if not refresh_token or not token_endpoint or not client_id:
        logger.warning(
            "token_expired_no_refresh connector_id=%s (missing refresh_token, "
            "token_endpoint, or client_id)",
            record.connector_id,
        )
        return record

    from app.mcp_connectors import oauth as oauth_mod

    try:
        new_tokens = await oauth_mod.refresh_tokens(
            token_endpoint=token_endpoint,
            client_id=client_id,
            client_secret=oauth.get("client_secret", ""),
            refresh_token=refresh_token,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "token_refresh_failed connector_id=%s",
            record.connector_id,
            exc_info=True,
        )
        return record

    # Merge into the existing oauth dict (preserve client_id, etc.).
    merged = dict(oauth)
    merged.update(new_tokens)
    logger.info("token_refreshed connector_id=%s", record.connector_id)

    # Persist if a registry is provided.
    if registry is not None:
        try:
            return registry.refresh_oauth(record.connector_id, merged)
        except Exception:  # noqa: BLE001
            logger.warning(
                "token_refresh_persist_failed connector_id=%s",
                record.connector_id,
                exc_info=True,
            )
            # Fall through — update the in-memory record at least.

    # Return an updated copy even if not persisted.
    from dataclasses import replace

    return replace(record, oauth=merged)


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
        # Re-check the allowlist at launch, not just at write time: a record could
        # predate the policy or have been changed by another path.
        module = stdio_module(record.command, record.args)
        if not module:
            logger.warning(
                "stdio_connector_blocked connector_id=%s command=%r",
                record.connector_id, record.command,
            )
            raise ValueError(validate_stdio(record.command, record.args))
        # Curated env only — enough to find the interpreter and import the app,
        # plus this module's own platform credentials, plus the connector's
        # secrets. Never the master's full environment.
        return Client(
            StdioTransport(
                command=record.command,
                args=list(record.args or []),
                env=_stdio_env(module, record.env),
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


async def test_connection(record: MCPConnectorRecord, *, registry=None) -> dict:
    """Connect, list tools, and report which transport worked.

    Returns {ok, error, tool_count, tools, transport}. Never raises — failures
    are reported in the result. For HTTP connectors the working transport is
    auto-detected (streamable_http then sse).

    If ``registry`` is provided, expired OAuth tokens are refreshed and
    persisted before the test.
    """
    # Auto-refresh expired tokens before testing.
    record = await ensure_fresh_token(record, registry=registry)

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
