"""Upstream MCP connector + per-container whitelist HTTP API (admin).

Routes live under ``/master/mcp`` and are reached by the dashboard through
the platform proxy passthrough. Secrets (auth headers, stdio env) are
redacted on every read.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_registry, require_admin
from app.config import settings
from app.mcp_connectors import google_oauth, oauth
from app.mcp_connectors.models import VALID_TRANSPORTS, MCPConnectorRecord
from app.mcp_connectors.registry import MCPConnectorRegistry
from app.mcp_connectors.upstream import test_connection
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/mcp", tags=["MCP Connectors"])


def _get_connector_registry(request: Request) -> MCPConnectorRegistry:
    return request.app.state.mcp_connector_registry


def _ws(request: Request) -> str:
    """Workspace id injected by the platform's workspace proxy (X-Workspace-Id).

    Empty on direct admin calls (no header) — those stay unscoped for back-compat.
    """
    return request.headers.get("X-Workspace-Id", "")


def _visible(rec: MCPConnectorRecord, ws: str) -> bool:
    if not ws:
        return True
    owner = (rec.metadata or {}).get("workspace_id", "")
    # Legacy connectors (no owner) remain visible; otherwise must match.
    return owner in ("", ws)


def _owned_or_404(reg: MCPConnectorRegistry, connector_id: str, request: Request) -> MCPConnectorRecord:
    rec = reg.get(connector_id)
    if rec is None or not _visible(rec, _ws(request)):
        raise HTTPException(status_code=404, detail="Connector not found")
    return rec


def _redact(secrets: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***set***" if v else "") for k, v in (secrets or {}).items()}


def _connector_to_public(rec: MCPConnectorRecord) -> dict[str, Any]:
    return {
        "connector_id": rec.connector_id,
        "display_name": rec.display_name,
        "transport": rec.transport,
        "url": rec.url,
        "command": rec.command,
        "args": rec.args or [],
        "headers": _redact(rec.headers),
        "env": _redact(rec.env),
        "enabled": rec.enabled,
        "last_status": rec.last_status,
        "last_error": rec.last_error or "",
        "last_checked_at": rec.last_checked_at.isoformat() if rec.last_checked_at else None,
        "tool_count": rec.tool_count,
        "auth_status": rec.auth_status or "none",
        "added_at": rec.added_at.isoformat() if rec.added_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
        "metadata": rec.metadata or {},
    }


class ConnectorCreate(BaseModel):
    connector_id: str = Field(..., description="Unique stable id, e.g. 'notion'.")
    display_name: str
    transport: str = Field(default="streamable_http")
    url: str = Field(default="", description="Required for streamable_http/sse.")
    command: str = Field(default="", description="Required for stdio.")
    args: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict, description="Auth headers for http/sse.")
    env: dict[str, str] = Field(default_factory=dict, description="Process env for stdio.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorPatch(BaseModel):
    display_name: str | None = None
    transport: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] | None = None
    headers: dict[str, str] | None = None
    env: dict[str, str] | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class WhitelistSet(BaseModel):
    connector_ids: list[str] = Field(default_factory=list)


def _validate_transport(transport: str, url: str, command: str) -> None:
    t = (transport or "").strip().lower()
    if t not in VALID_TRANSPORTS:
        raise HTTPException(
            status_code=422,
            detail=f"transport must be one of: {sorted(VALID_TRANSPORTS)}",
        )
    if t in ("streamable_http", "sse") and not (url or "").strip():
        raise HTTPException(status_code=422, detail=f"{t} connector requires a url")
    if t == "stdio" and not (command or "").strip():
        raise HTTPException(status_code=422, detail="stdio connector requires a command")


# --- Connector CRUD ---------------------------------------------------


@router.get("/connectors", summary="List registered MCP connectors")
async def list_connectors(
    request: Request,
    enabled_only: bool = False,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    ws = _ws(request)
    rows = [r for r in reg.list_all(enabled_only=enabled_only) if _visible(r, ws)]
    return {"count": len(rows), "connectors": [_connector_to_public(r) for r in rows]}


@router.get("/connectors/{connector_id}", summary="Get one MCP connector")
async def get_connector(
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    return _connector_to_public(_owned_or_404(reg, connector_id, request))


@router.post("/connectors", summary="Register a new MCP connector", status_code=201)
async def create_connector(
    body: ConnectorCreate,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    _validate_transport(body.transport, body.url, body.command)
    now = datetime.now(timezone.utc)
    rec = MCPConnectorRecord(
        connector_id=body.connector_id.strip(),
        display_name=body.display_name,
        transport=body.transport.strip().lower(),
        url=body.url.strip(),
        command=body.command.strip(),
        args=list(body.args or []),
        headers=dict(body.headers or {}),
        env=dict(body.env or {}),
        enabled=True,
        added_at=now,
        updated_at=now,
        metadata=dict(body.metadata or {}),
    )
    ws = _ws(request)
    if ws:
        rec.metadata["workspace_id"] = ws
    reg = _get_connector_registry(request)
    try:
        reg.add(rec)
    except ValueError:
        raise HTTPException(status_code=422, detail="connector_id already exists") from None
    created = reg.get(rec.connector_id)
    assert created is not None
    logger.info("mcp_connector_registered connector_id=%s", rec.connector_id)
    return _connector_to_public(created)


class ConnectorImportItem(BaseModel):
    connector_id: str
    display_name: str = ""
    transport: str = "streamable_http"
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    enabled: bool = True
    auth_status: str = "none"
    tool_count: int = 0
    last_status: str = "unknown"
    # The secrets blob exactly as stored on the source master (already encrypted
    # with the shared key); written verbatim, never decrypted here.
    secrets_encrypted: str = "{}"
    metadata: dict[str, Any] = Field(default_factory=dict)
    added_at: str | None = None
    updated_at: str | None = None


class ConnectorImportBody(BaseModel):
    connectors: list[ConnectorImportItem] = Field(default_factory=list)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.post("/connectors/import", summary="Bulk-hydrate connectors from the backend")
async def import_connectors(
    body: ConnectorImportBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Restore a workspace's connectors onto this (possibly fresh) master.

    Called by the backend after provisioning. Secrets are stored verbatim;
    rows whose local copy is newer are left untouched.
    """
    reg = _get_connector_registry(request)
    written = 0
    skipped = 0
    for item in body.connectors:
        rec = MCPConnectorRecord(
            connector_id=item.connector_id.strip(),
            display_name=item.display_name,
            transport=(item.transport or "streamable_http").strip().lower(),
            url=item.url or "",
            command=item.command or "",
            args=list(item.args or []),
            enabled=bool(item.enabled),
            auth_status=item.auth_status or "none",
            tool_count=int(item.tool_count or 0),
            last_status=item.last_status or "unknown",
            added_at=_parse_dt(item.added_at),
            updated_at=_parse_dt(item.updated_at),
            metadata=dict(item.metadata or {}),
        )
        if reg.import_record(rec, item.secrets_encrypted or "{}"):
            written += 1
        else:
            skipped += 1
    logger.info("mcp_connectors_imported written=%s skipped=%s", written, skipped)
    return {"written": written, "skipped": skipped}


@router.patch("/connectors/{connector_id}", summary="Update an MCP connector")
async def patch_connector(
    connector_id: str,
    body: ConnectorPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    current = _owned_or_404(reg, connector_id, request)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return _connector_to_public(current)
    # Re-validate transport when any of the relevant fields change.
    if {"transport", "url", "command"} & updates.keys():
        _validate_transport(
            updates.get("transport", current.transport),
            updates.get("url", current.url),
            updates.get("command", current.command),
        )
    if "transport" in updates:
        updates["transport"] = str(updates["transport"]).strip().lower()
    try:
        updated = reg.update(connector_id, updates)
    except KeyError:
        raise HTTPException(status_code=404, detail="Connector not found") from None
    return _connector_to_public(updated)


@router.delete("/connectors/{connector_id}", summary="Remove an MCP connector")
async def delete_connector(
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    _owned_or_404(reg, connector_id, request)
    try:
        reg.remove(connector_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Connector not found") from None
    return {"removed": connector_id}


@router.post("/connectors/{connector_id}/enable", summary="Enable a connector")
async def enable_connector(
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    _owned_or_404(reg, connector_id, request)
    try:
        rec = reg.enable(connector_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Connector not found") from None
    return _connector_to_public(rec)


@router.post("/connectors/{connector_id}/disable", summary="Disable a connector")
async def disable_connector(
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    _owned_or_404(reg, connector_id, request)
    try:
        rec = reg.disable(connector_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Connector not found") from None
    return _connector_to_public(rec)


@router.post("/connectors/{connector_id}/test", summary="Live-test a connector")
async def test_connector(
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Connect to the upstream MCP server, list its tools, and record the result."""
    reg = _get_connector_registry(request)
    rec = _owned_or_404(reg, connector_id, request)
    result = await test_connection(rec, registry=reg)
    reg.record_test_result(
        connector_id,
        status="ok" if result["ok"] else "error",
        error=result.get("error", ""),
        tool_count=result.get("tool_count", 0),
    )
    # Persist the transport that actually worked so the aggregator reuses it.
    detected = result.get("transport")
    if result["ok"] and detected and detected != rec.transport:
        reg.update(connector_id, {"transport": detected})
    return result


# --- OAuth ("Connect") ------------------------------------------------


class AuthorizeBody(BaseModel):
    # The dashboard's own callback URL (its origin handles HTTPS/localhost).
    redirect_uri: str = Field(..., description="Where the provider returns the user.")


class ExchangeBody(BaseModel):
    state: str
    code: str


@router.post("/connectors/{connector_id}/authorize", summary="Begin OAuth for a connector")
async def authorize_connector(
    connector_id: str,
    body: AuthorizeBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Discover the connector's OAuth server, register a client, and return an
    authorization URL for the user's browser. If the server needs no auth, the
    connector is marked connected immediately.
    """
    reg = _get_connector_registry(request)
    rec = _owned_or_404(reg, connector_id, request)

    # Google apps (Gmail, Calendar — each its own connector) aren't MCP servers:
    # no protected-resource discovery, no dynamic client registration. belleq owns
    # one registered OAuth app, and each connector asks only for its app's scopes.
    provider = (rec.metadata or {}).get("provider", "")
    if provider in google_oauth.PROVIDERS:
        if not settings.google_client_id or not settings.google_client_secret:
            raise HTTPException(
                status_code=503,
                detail="Google connections aren't configured on this server yet.",
            )
        verifier, challenge = oauth.make_pkce()
        state = _secrets.token_urlsafe(24)
        auth_url = google_oauth.build_authorization_url(
            client_id=settings.google_client_id,
            redirect_uri=body.redirect_uri,
            state=state,
            code_challenge=challenge,
            scopes=google_oauth.scopes_for(provider),
        )
        request.app.state.db.save_oauth_state(
            state,
            connector_id,
            {
                "provider": provider,
                "code_verifier": verifier,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "token_endpoint": google_oauth.TOKEN_ENDPOINT,
                "redirect_uri": body.redirect_uri,
                "resource": "",
            },
        )
        reg.update(connector_id, {"auth_status": "pending"})
        return {"auth_required": True, "authorization_url": auth_url, "state": state}

    try:
        meta = await oauth.discover(rec.url)
    except oauth.OAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    if not meta.get("auth_required"):
        reg.update(connector_id, {"auth_status": "none"})
        return {"auth_required": False, "connector_id": connector_id}

    if not meta.get("registration_endpoint"):
        raise HTTPException(
            status_code=422,
            detail=(
                "This server requires OAuth but does not support dynamic client "
                "registration. Manual client setup isn't wired up yet."
            ),
        )

    try:
        client = await oauth.register_client(meta["registration_endpoint"], body.redirect_uri)
    except oauth.OAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    verifier, challenge = oauth.make_pkce()
    state = _secrets.token_urlsafe(24)
    auth_url = oauth.build_authorization_url(
        authorization_endpoint=meta["authorization_endpoint"],
        client_id=client["client_id"],
        redirect_uri=body.redirect_uri,
        state=state,
        code_challenge=challenge,
        scopes=meta.get("scopes_supported", []),
        resource=meta.get("resource", ""),
    )

    request.app.state.db.save_oauth_state(
        state,
        connector_id,
        {
            "code_verifier": verifier,
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "token_endpoint": meta.get("token_endpoint", ""),
            "redirect_uri": body.redirect_uri,
            "resource": meta.get("resource", ""),
        },
    )
    reg.update(connector_id, {"auth_status": "pending"})
    return {"auth_required": True, "authorization_url": auth_url, "state": state}


@router.post("/oauth/exchange", summary="Complete OAuth (code -> tokens)")
async def oauth_exchange(
    body: ExchangeBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Called by the dashboard callback with the ?code&state from the provider."""
    db = request.app.state.db
    saved = db.get_oauth_state(body.state)
    if saved is None:
        raise HTTPException(status_code=400, detail="Unknown or expired OAuth state")
    p = saved["payload"]
    reg = _get_connector_registry(request)
    connector_id = saved["connector_id"]

    try:
        tokens = await oauth.exchange_code(
            token_endpoint=p["token_endpoint"],
            client_id=p["client_id"],
            client_secret=p.get("client_secret", ""),
            code=body.code,
            redirect_uri=p["redirect_uri"],
            code_verifier=p["code_verifier"],
            resource=p.get("resource", ""),
        )
    except oauth.OAuthError as e:
        reg.update(connector_id, {"auth_status": "error"})
        db.delete_oauth_state(body.state)
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    # Persist tokens + the client info needed for later refresh.
    tokens.update(
        {
            "client_id": p["client_id"],
            "client_secret": p.get("client_secret", ""),
            "token_endpoint": p["token_endpoint"],
            "resource": p.get("resource", ""),
        }
    )
    updates: dict[str, Any] = {"oauth": tokens, "auth_status": "connected"}

    # Google apps run as bundled stdio servers, which read their grant from the
    # process env rather than the aggregator's Authorization header — so mirror
    # the refresh token into env (encrypted at rest like every other secret).
    # Google only returns a refresh_token on first consent, so keep the existing
    # one if this exchange didn't carry a new one.
    if p.get("provider") in google_oauth.PROVIDERS:
        current = reg.get(connector_id)
        existing = dict((current.env if current else {}) or {})
        refresh = tokens.get("refresh_token") or existing.get("GOOGLE_REFRESH_TOKEN", "")
        if not refresh:
            reg.update(connector_id, {"auth_status": "error"})
            db.delete_oauth_state(body.state)
            raise HTTPException(
                status_code=422,
                detail=(
                    "Google didn't return a refresh token. Remove belleq at "
                    "myaccount.google.com/permissions and connect again."
                ),
            )
        existing["GOOGLE_REFRESH_TOKEN"] = refresh
        updates["env"] = existing

    reg.update(connector_id, updates)
    db.delete_oauth_state(body.state)
    logger.info("oauth_connected connector_id=%s", connector_id)
    return {"connector_id": connector_id, "auth_status": "connected"}


# --- Per-container whitelist ------------------------------------------


def _ensure_container(request: Request, container_id: str) -> None:
    """Validate a container for whitelist ops, scoped to the workspace.

    Raises 404 if the container belongs to a *different* workspace (so one
    workspace can't manage another's whitelist on a shared master). Unknown
    containers are allowed (dashboard may pre-configure an ACL).
    """
    creg: ContainerRegistry = get_registry(request)
    rec = creg.get(container_id)
    if rec is None:
        logger.info("mcp_acl_for_unregistered_container container_id=%s", container_id)
        return
    ws = _ws(request)
    if ws and (rec.metadata or {}).get("workspace_id", "") not in ("", ws):
        raise HTTPException(status_code=404, detail="Container not found")


@router.get(
    "/containers/{container_id}/connectors",
    summary="List a container's whitelisted connectors",
)
async def list_container_connectors(
    container_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    _ensure_container(request, container_id)
    reg = _get_connector_registry(request)
    ids = reg.connectors_for_container(container_id)
    resolved = reg.enabled_connectors_for_container(container_id)
    return {
        "container_id": container_id,
        "connector_ids": ids,
        "connectors": [_connector_to_public(r) for r in resolved],
    }


@router.put(
    "/containers/{container_id}/connectors",
    summary="Replace a container's connector whitelist",
)
async def set_container_connectors(
    container_id: str,
    body: WhitelistSet,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    _ensure_container(request, container_id)
    reg = _get_connector_registry(request)
    # Drop ids that don't resolve to a known connector.
    known = {c.connector_id for c in reg.list_all()}
    unknown = [c for c in body.connector_ids if c not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown connector_ids: {unknown}")
    applied = reg.set_container_connectors(container_id, body.connector_ids)
    return {"container_id": container_id, "connector_ids": applied}


@router.post(
    "/containers/{container_id}/connectors/{connector_id}",
    summary="Whitelist one connector on a container",
    status_code=201,
)
async def add_container_connector(
    container_id: str,
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    if reg.get(connector_id) is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    _ensure_container(request, container_id)
    reg.add_container_connector(container_id, connector_id)
    return {"container_id": container_id, "connector_ids": reg.connectors_for_container(container_id)}


@router.delete(
    "/containers/{container_id}/connectors/{connector_id}",
    summary="Remove one connector from a container's whitelist",
)
async def remove_container_connector(
    container_id: str,
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    reg.remove_container_connector(container_id, connector_id)
    return {"container_id": container_id, "connector_ids": reg.connectors_for_container(container_id)}
