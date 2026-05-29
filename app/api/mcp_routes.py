"""Upstream MCP connector + per-container whitelist HTTP API (admin).

Routes live under ``/master/mcp`` and are reached by the dashboard through
the platform proxy passthrough. Secrets (auth headers, stdio env) are
redacted on every read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_registry, require_admin
from app.mcp_connectors.models import VALID_TRANSPORTS, MCPConnectorRecord
from app.mcp_connectors.registry import MCPConnectorRegistry
from app.mcp_connectors.upstream import test_connection
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/mcp", tags=["MCP Connectors"])


def _get_connector_registry(request: Request) -> MCPConnectorRegistry:
    return request.app.state.mcp_connector_registry


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
    rows = reg.list_all(enabled_only=enabled_only)
    return {"count": len(rows), "connectors": [_connector_to_public(r) for r in rows]}


@router.get("/connectors/{connector_id}", summary="Get one MCP connector")
async def get_connector(
    connector_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    rec = reg.get(connector_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return _connector_to_public(rec)


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
    reg = _get_connector_registry(request)
    try:
        reg.add(rec)
    except ValueError:
        raise HTTPException(status_code=422, detail="connector_id already exists") from None
    created = reg.get(rec.connector_id)
    assert created is not None
    logger.info("mcp_connector_registered connector_id=%s", rec.connector_id)
    return _connector_to_public(created)


@router.patch("/connectors/{connector_id}", summary="Update an MCP connector")
async def patch_connector(
    connector_id: str,
    body: ConnectorPatch,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg = _get_connector_registry(request)
    current = reg.get(connector_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Connector not found")
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
    rec = reg.get(connector_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    result = await test_connection(rec)
    reg.record_test_result(
        connector_id,
        status="ok" if result["ok"] else "error",
        error=result.get("error", ""),
        tool_count=result.get("tool_count", 0),
    )
    return result


# --- Per-container whitelist ------------------------------------------


def _ensure_container(request: Request, container_id: str) -> None:
    """Best-effort: warn but do not block if the container is unknown.

    The aggregator keys off the registry container_id; we allow setting an
    ACL before a container is registered so the dashboard can pre-configure.
    """
    creg: ContainerRegistry = get_registry(request)
    if creg.get(container_id) is None:
        logger.info("mcp_acl_for_unregistered_container container_id=%s", container_id)


@router.get(
    "/containers/{container_id}/connectors",
    summary="List a container's whitelisted connectors",
)
async def list_container_connectors(
    container_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
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
