"""Aggregated operations across all enabled user containers."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_client, get_registry, require_admin
from app.clients.container_client import ContainerClient, _unreachable
from app.config import settings
from app.database import MasterDB
from app.registry.models import ContainerRecord
from app.registry.registry import ContainerRegistry
from app.vectordb.base import VectorDBAdapter, VectorDBError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/aggregate", tags=["Aggregate"])


def _status_from_record(
    rec: ContainerRecord,
    reachable: bool,
    response_ms: int | None,
    stats: dict | None,
) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    return {
        "container_id": rec.container_id,
        "display_name": rec.display_name,
        "container_type": rec.container_type,
        "base_url": rec.base_url,
        "enabled": rec.enabled,
        "reachable": reachable,
        "checked_at": checked_at,
        "response_ms": response_ms,
        "stats": stats,
    }


def _is_reachable_stats_payload(payload: dict[str, Any]) -> bool:
    if payload.get("reachable") is False:
        return False
    if payload.get("error") and payload.get("container_id") is not None:
        if set(payload.keys()) <= {"container_id", "reachable", "error"}:
            return False
    return True


def _stats_shape_ok(payload: dict[str, Any]) -> bool:
    keys = {
        "total_documents",
        "flagged_documents",
        "healthy_documents",
        "needs_review_documents",
        "average_decay_score",
    }
    return any(k in payload for k in keys)


def _pick_int(d: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for n in names:
        v = d.get(n)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _merge_totals(stats_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "total_documents": 0,
        "flagged_documents": 0,
        "healthy_documents": 0,
        "needs_review_documents": 0,
        "average_decay_score": None,
    }
    decay_values: list[float] = []
    for s in stats_payloads:
        td = _pick_int(s, ("total_documents", "total_docs", "documents"))
        if td is not None:
            totals["total_documents"] += td
        fd = _pick_int(s, ("flagged_documents", "flagged"))
        if fd is not None:
            totals["flagged_documents"] += fd
        hd = _pick_int(s, ("healthy_documents", "healthy"))
        if hd is not None:
            totals["healthy_documents"] += hd
        nd = _pick_int(s, ("needs_review_documents", "needs_review"))
        if nd is not None:
            totals["needs_review_documents"] += nd
        ads = s.get("average_decay_score")
        if isinstance(ads, (int, float)):
            decay_values.append(float(ads))
    if decay_values:
        totals["average_decay_score"] = sum(decay_values) / len(decay_values)
    return totals


class FlagBody(BaseModel):
    """Reason for flagging a document in a user container."""

    reason: str = Field(..., min_length=1, description="Human-readable reason shown in dashboards.")


@router.get("/health", summary="Health-check all enabled containers")
async def aggregate_health(
    request: Request,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """
    Health check all enabled containers in parallel.
    Logs each result to health_check_log.
    Returns results immediately — does not wait for slow containers beyond health_timeout.
    """
    records = registry.list_enabled()
    db: MasterDB = request.app.state.db
    results = await client.health_check_all(records)
    containers_out: list[dict[str, Any]] = []
    reachable_n = 0
    for rec, reachable, response_ms, err in results:
        db.log_health_check(rec.container_id, reachable, response_ms, err)
        if reachable:
            reachable_n += 1
        containers_out.append(_status_from_record(rec, reachable, response_ms, None))
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "generated_at": generated_at,
        "total": len(records),
        "reachable": reachable_n,
        "unreachable": len(records) - reachable_n,
        "containers": containers_out,
    }


@router.get("/stats", summary="Fetch /stats from all enabled containers")
async def aggregate_stats(
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """
    Fetch /stats from all enabled containers in parallel.
    Unreachable containers included with reachable=False, stats=null.
    Totals computed from reachable containers only.
    If a container responds but with unexpected shape, include with stats=null and reachable=True.
    """
    records = registry.list_enabled()

    async def timed_stats(rec: ContainerRecord) -> tuple[ContainerRecord, dict[str, Any], int | None]:
        start = time.monotonic()
        payload = await client.get_stats(rec)
        ms = int((time.monotonic() - start) * 1000)
        if not isinstance(payload, dict):
            payload = _unreachable(rec, "invalid response")
        return rec, payload, ms

    pairs = await asyncio.gather(*[timed_stats(r) for r in records], return_exceptions=True)

    containers_out: list[dict[str, Any]] = []
    good_stats: list[dict[str, Any]] = []
    reachable_containers = 0
    for rec, item in zip(records, pairs, strict=True):
        if isinstance(item, BaseException):
            logger.warning("aggregate_stats_fetch_failed container_id=%s error=%s", rec.container_id, item)
            checked_at = datetime.now(timezone.utc).isoformat()
            containers_out.append(
                {
                    "container_id": rec.container_id,
                    "display_name": rec.display_name,
                    "container_type": rec.container_type,
                    "base_url": rec.base_url,
                    "enabled": rec.enabled,
                    "reachable": False,
                    "checked_at": checked_at,
                    "response_ms": None,
                    "stats": None,
                }
            )
            continue
        _, payload, response_ms = item
        reachable = _is_reachable_stats_payload(payload)
        stats_obj: dict[str, Any] | None
        if reachable and _stats_shape_ok(payload):
            stats_obj = payload
            good_stats.append(payload)
            reachable_containers += 1
        elif reachable:
            stats_obj = None
            reachable_containers += 1
        else:
            stats_obj = None
        checked_at = datetime.now(timezone.utc).isoformat()
        containers_out.append(
            {
                "container_id": rec.container_id,
                "display_name": rec.display_name,
                "container_type": rec.container_type,
                "base_url": rec.base_url,
                "enabled": rec.enabled,
                "reachable": reachable,
                "checked_at": checked_at,
                "response_ms": response_ms,
                "stats": stats_obj,
            }
        )
    totals = _merge_totals(good_stats)
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "generated_at": generated_at,
        "total_containers": len(records),
        "reachable_containers": reachable_containers,
        "containers": containers_out,
        "totals": totals,
    }


@router.get("/docs", summary="Merged /docs listing from all enabled containers")
async def aggregate_docs(
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
    status: str | None = None,
    department: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    """
    Fetch /docs from all enabled containers in parallel.
    Merges results into one list and adds container_id to each document.
    """
    records = registry.list_enabled()

    async def fetch(rec: ContainerRecord) -> tuple[ContainerRecord, dict[str, Any]]:
        return rec, await client.get_docs(rec, status=status, department=department, limit=limit)

    pairs = await asyncio.gather(*[fetch(r) for r in records], return_exceptions=True)

    documents: list[dict[str, Any]] = []
    sources: list[str] = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for rec, item in zip(records, pairs, strict=True):
        if isinstance(item, BaseException):
            logger.warning("aggregate_docs_fetch_failed container_id=%s error=%s", rec.container_id, item)
            continue
        _, payload = item
        if not isinstance(payload, dict):
            continue
        if payload.get("reachable") is False:
            continue
        docs = payload.get("documents") or payload.get("items") or payload.get("docs")
        if not isinstance(docs, list):
            if isinstance(payload, list):
                docs = payload
            else:
                continue
        sources.append(rec.container_id)
        for d in docs:
            if isinstance(d, dict):
                row = dict(d)
                row["container_id"] = rec.container_id
                documents.append(row)

    return {
        "generated_at": generated_at,
        "total": len(documents),
        "sources": sources,
        "documents": documents,
    }


@router.post(
    "/containers/{container_id}/docs/{doc_id}/flag",
    summary="Proxy flag to a user container",
)
async def proxy_flag(
    container_id: str,
    doc_id: str,
    body: FlagBody,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """
    Proxy flag request to a specific container.
    Looks up container in registry, calls its flag endpoint.
    """
    rec = registry.get(container_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Container not found")
    out = await client.flag_doc(rec, doc_id, body.reason)
    if out.get("reachable") is False:
        raise HTTPException(status_code=503, detail=out.get("error", "unreachable"))
    return out


@router.post(
    "/containers/{container_id}/docs/{doc_id}/unflag",
    summary="Proxy unflag to a user container",
)
async def proxy_unflag(
    container_id: str,
    doc_id: str,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """Proxy unflag to specific container."""
    rec = registry.get(container_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Container not found")
    out = await client.unflag_doc(rec, doc_id)
    if out.get("reachable") is False:
        raise HTTPException(status_code=503, detail=out.get("error", "unreachable"))
    return out


@router.delete(
    "/containers/{container_id}/docs/{doc_id}",
    summary="Delete in user container and master vector DB",
)
async def proxy_full_delete(
    container_id: str,
    doc_id: str,
    request: Request,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    """
    Full delete: removes from user container AND master vector DB.
    Step 1: call container's DELETE /docs/{doc_id}
    Step 2: call vectordb adapter.delete_by_doc_id()
    Both steps attempted independently.
    Both results reported in response.
    """
    rec = registry.get(container_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Container not found")

    container_deleted = False
    container_error: str | None = None
    out = await client.delete_doc(rec, doc_id)
    if isinstance(out, dict) and out.get("reachable") is False:
        container_error = str(out.get("error", "unreachable"))
    else:
        container_deleted = True

    vectordb_deleted = False
    vectordb_points_removed = 0
    vectordb_error: str | None = None
    adapter: VectorDBAdapter | None = getattr(request.app.state, "vectordb", None)
    if settings.vectordb_backend.strip().lower() == "pinecone":
        collection_name = (settings.pinecone_index_name or "").strip()
    else:
        collection_name = (settings.qdrant_collection or "").strip()
    if adapter is None:
        vectordb_error = "Vector database adapter unavailable"
    elif not collection_name:
        vectordb_error = "Master collection/index name is not configured (QDRANT_COLLECTION / PINECONE_INDEX_NAME)"
    else:
        try:
            vectordb_points_removed = await adapter.delete_by_doc_id(collection_name, doc_id)
            vectordb_deleted = True
        except VectorDBError as e:
            vectordb_error = str(e)

    return {
        "doc_id": doc_id,
        "container_id": container_id,
        "container_deleted": container_deleted,
        "container_error": container_error,
        "vectordb_deleted": vectordb_deleted,
        "vectordb_points_removed": vectordb_points_removed,
        "vectordb_error": vectordb_error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
