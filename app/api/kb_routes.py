"""Master-side passthrough for the Belleq KB REST API.

The public REST API (for non-MCP AI providers) lives on the static backend, but
the actual knowledge base lives in a per-context belleq-user container reachable
only from its host's master. This router lets the backend reach those containers:

    backend  --X-Admin-Key-->  master /master/kb/{container}/{op}
    master   --X-Master-Key->  container /internal/kb/{op}

``op`` is one of recall | query | record | flush. Admin-authenticated; the public
api_key check happens upstream at the backend.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request  # noqa: F401

from app.api.deps import get_client, get_registry, require_admin
from app.clients.container_client import ContainerClient
from app.registry.models import ContainerRecord
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/master/kb",
    tags=["Knowledge Base REST passthrough"],
    dependencies=[Depends(require_admin)],
)

# Separate router for workspace-wide ingestion queue stats (different prefix).
ingestion_router = APIRouter(
    prefix="/master/ingestion",
    tags=["Ingestion"],
    dependencies=[Depends(require_admin)],
)

_QUEUE_KEYS = ("queued", "processing", "done", "failed", "dead", "total")


def _scoped(registry: ContainerRegistry, request: Request) -> list[ContainerRecord]:
    """Workspace-scoped, enabled containers (matches conversations_routes)."""
    ws = request.headers.get("X-Workspace-Id", "")
    out = []
    for r in registry.list_enabled():
        owner = (r.metadata or {}).get("workspace_id", "")
        if not ws or owner in ("", ws):
            out.append(r)
    return out


@ingestion_router.get("/stats", summary="Ingestion queue stats across the workspace")
async def ingestion_stats_all(
    request: Request,
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """Fan out ingestion-queue counts across the workspace's containers."""
    records = _scoped(registry, request)

    async def one(rec: ContainerRecord) -> tuple[ContainerRecord, Any]:
        return rec, await client.get_ingestion_stats(rec)

    pairs = await asyncio.gather(*[one(r) for r in records], return_exceptions=True)
    totals = {k: 0 for k in _QUEUE_KEYS}
    containers_out: list[dict[str, Any]] = []
    reachable_n = 0
    for rec, item in zip(records, pairs, strict=True):
        payload = item[1] if isinstance(item, tuple) else item
        if isinstance(item, BaseException) or not (
            isinstance(payload, dict) and payload.get("reachable") is not False
        ):
            containers_out.append({"container_id": rec.container_id, "reachable": False})
            continue
        reachable_n += 1
        counts = {k: int(payload.get(k, 0) or 0) for k in _QUEUE_KEYS}
        for k in _QUEUE_KEYS:
            totals[k] += counts[k]
        containers_out.append({"container_id": rec.container_id, "reachable": True, **counts})

    return {
        "total_containers": len(records),
        "reachable_containers": reachable_n,
        "totals": totals,
        "containers": containers_out,
    }

_VALID_OPS = {"recall", "query", "record", "flush", "upload", "agent_write"}


@router.post("/{container_name}/{op}")
async def kb_passthrough(
    container_name: str,
    op: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    if op not in _VALID_OPS:
        raise HTTPException(status_code=404, detail=f"Unknown KB op: {op}")
    rec = registry.get(container_name)
    if rec is None:
        raise HTTPException(status_code=404, detail="Context container not found")
    try:
        return await client.kb_op(rec, op, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_passthrough_failed container=%s op=%s err=%s", container_name, op, exc)
        raise HTTPException(status_code=502, detail=f"Context unavailable: {exc}") from exc


@router.get("/{container_name}/ingestion-stats")
async def ingestion_stats(
    container_name: str,
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """Queue counts for a context (queued/processing/done/failed/dead)."""
    rec = registry.get(container_name)
    if rec is None:
        raise HTTPException(status_code=404, detail="Context container not found")
    return await client.get_ingestion_stats(rec)
