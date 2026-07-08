"""Master-side passthrough for per-context retention (stale-doc housekeeping).

The dashboard Settings page manages each context's retention through here:

    backend workspace proxy --X-Admin-Key--> master /master/retention/{container}/...
    master --X-Master-Key--> container /internal/retention/... (or PATCH /internal/config)

Workspace-scoped: on shared masters a container is only reachable when its
registered workspace matches the ``X-Workspace-Id`` the backend injects.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.api.deps import get_client, get_registry, require_admin
from app.clients.container_client import ContainerClient
from app.registry.models import ContainerRecord
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/master/retention",
    tags=["Retention passthrough"],
    dependencies=[Depends(require_admin)],
)

# Only these runtime-config keys may be patched through this router.
_RETENTION_CONFIG_KEYS = {
    "retention_enabled",
    "retention_archive_after_days",
    "retention_purge_enabled",
    "retention_purge_after_days",
}


def _record(
    registry: ContainerRegistry, request: Request, container_name: str
) -> ContainerRecord:
    rec = registry.get(container_name)
    if rec is None:
        raise HTTPException(status_code=404, detail="Context container not found")
    ws = request.headers.get("X-Workspace-Id", "")
    owner = (rec.metadata or {}).get("workspace_id", "")
    if ws and owner and owner != ws:
        raise HTTPException(status_code=404, detail="Context container not found")
    return rec


async def _call(
    client: ContainerClient,
    rec: ContainerRecord,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
) -> dict:
    try:
        return await client.retention_call(rec, path, method=method, payload=payload)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
        raise HTTPException(status_code=502, detail=f"Context unavailable: {detail}") from exc


@router.get("/{container_name}/status")
async def retention_status(
    container_name: str,
    request: Request,
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    rec = _record(registry, request, container_name)
    return await _call(client, rec, "/internal/retention/status")


@router.get("/{container_name}/archived")
async def retention_archived(
    container_name: str,
    request: Request,
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    rec = _record(registry, request, container_name)
    return await _call(client, rec, "/internal/retention/archived")


@router.post("/{container_name}/sweep")
async def retention_sweep(
    container_name: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    rec = _record(registry, request, container_name)
    body = {"dry_run": bool(payload.get("dry_run", False))}
    return await _call(client, rec, "/internal/retention/sweep", method="POST", payload=body)


@router.post("/{container_name}/restore")
async def retention_restore(
    container_name: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    rec = _record(registry, request, container_name)
    doc_id = str(payload.get("doc_id") or "").strip()
    if not doc_id:
        raise HTTPException(status_code=422, detail="doc_id is required")
    return await _call(
        client, rec, "/internal/retention/restore", method="POST", payload={"doc_id": doc_id}
    )


@router.patch("/{container_name}/config")
async def retention_config(
    container_name: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    rec = _record(registry, request, container_name)
    body = {k: v for k, v in payload.items() if k in _RETENTION_CONFIG_KEYS and v is not None}
    if not body:
        raise HTTPException(status_code=422, detail="No retention fields to update")
    return await _call(client, rec, "/internal/config", method="PATCH", payload=body)
