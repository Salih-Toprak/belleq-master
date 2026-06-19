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

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.api.deps import get_client, get_registry, require_admin
from app.clients.container_client import ContainerClient
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/master/kb",
    tags=["Knowledge Base REST passthrough"],
    dependencies=[Depends(require_admin)],
)

_VALID_OPS = {"recall", "query", "record", "flush"}


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
