"""Conversation-extraction control across a workspace's user containers.

``POST /master/conversations/flush`` lets the dashboard push buffered
conversation exchanges into the knowledge base immediately, instead of waiting
for each container's idle-gap sweep (~30 min). ``GET .../stats`` surfaces the
extraction backlog. Both fan out to the workspace's containers in parallel and
merge the per-container results.

Scoped by the ``X-Workspace-Id`` header the platform proxy injects, so one
workspace can't flush another's containers on a shared master.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_client, get_registry, require_admin
from app.clients.container_client import ContainerClient
from app.registry.models import ContainerRecord
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/conversations", tags=["Conversations"])

_FLUSH_KEYS = ("closed", "pending", "skipped", "extracted")


def _ws(request: Request) -> str:
    return request.headers.get("X-Workspace-Id", "")


def _visible(rec: ContainerRecord, ws: str) -> bool:
    if not ws:
        return True
    owner = (rec.metadata or {}).get("workspace_id", "")
    # Legacy containers (no owner) remain visible; otherwise must match.
    return owner in ("", ws)


def _scoped(registry: ContainerRegistry, request: Request) -> list[ContainerRecord]:
    ws = _ws(request)
    return [r for r in registry.list_enabled() if _visible(r, ws)]


def _reachable(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("reachable") is not False


@router.post("/flush", summary="Flush buffered conversations into the KB now")
async def flush_conversations(
    request: Request,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """Force-close open sessions on each container and extract facts immediately."""
    records = _scoped(registry, request)

    async def one(rec: ContainerRecord) -> tuple[ContainerRecord, Any]:
        return rec, await client.flush_conversations(rec)

    pairs = await asyncio.gather(*[one(r) for r in records], return_exceptions=True)

    containers_out: list[dict[str, Any]] = []
    totals = {k: 0 for k in _FLUSH_KEYS}
    reachable_n = 0
    for rec, item in zip(records, pairs, strict=True):
        if isinstance(item, BaseException):
            logger.warning("flush_fetch_failed container_id=%s error=%s", rec.container_id, item)
            containers_out.append(
                {"container_id": rec.container_id, "reachable": False, "error": str(item)}
            )
            continue
        _, payload = item
        if not _reachable(payload):
            containers_out.append(
                {
                    "container_id": rec.container_id,
                    "reachable": False,
                    "error": (payload.get("error") if isinstance(payload, dict) else "invalid response"),
                }
            )
            continue
        reachable_n += 1
        counts = {k: int(payload.get(k, 0) or 0) for k in _FLUSH_KEYS}
        for k in _FLUSH_KEYS:
            totals[k] += counts[k]
        containers_out.append({"container_id": rec.container_id, "reachable": True, **counts})

    logger.info(
        "conversations_flush containers=%d reachable=%d extracted=%d",
        len(records),
        reachable_n,
        totals["extracted"],
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_containers": len(records),
        "reachable_containers": reachable_n,
        "totals": totals,
        "containers": containers_out,
    }


@router.get("/stats", summary="Conversation-extraction stats across the workspace")
async def conversation_stats(
    request: Request,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """Aggregate session/turn counts and per-status backlog across containers."""
    records = _scoped(registry, request)

    async def one(rec: ContainerRecord) -> tuple[ContainerRecord, Any]:
        return rec, await client.get_conversation_stats(rec)

    pairs = await asyncio.gather(*[one(r) for r in records], return_exceptions=True)

    containers_out: list[dict[str, Any]] = []
    totals = {"sessions": 0, "turns": 0}
    by_status: dict[str, int] = {}
    reachable_n = 0
    for rec, item in zip(records, pairs, strict=True):
        if isinstance(item, BaseException) or not _reachable(item[1] if isinstance(item, tuple) else item):
            payload = item[1] if isinstance(item, tuple) else item
            err = (
                str(item)
                if isinstance(item, BaseException)
                else (payload.get("error") if isinstance(payload, dict) else "invalid response")
            )
            containers_out.append({"container_id": rec.container_id, "reachable": False, "error": err})
            continue
        _, payload = item
        reachable_n += 1
        sessions = int(payload.get("sessions", 0) or 0)
        turns = int(payload.get("turns", 0) or 0)
        totals["sessions"] += sessions
        totals["turns"] += turns
        statuses = payload.get("sessions_by_status") or {}
        if isinstance(statuses, dict):
            for status, count in statuses.items():
                by_status[status] = by_status.get(status, 0) + int(count or 0)
        containers_out.append(
            {
                "container_id": rec.container_id,
                "reachable": True,
                "sessions": sessions,
                "turns": turns,
                "sessions_by_status": statuses if isinstance(statuses, dict) else {},
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_containers": len(records),
        "reachable_containers": reachable_n,
        "totals": totals,
        "sessions_by_status": by_status,
        "containers": containers_out,
    }
