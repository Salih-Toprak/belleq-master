"""Ingestion control and scheduler HTTP API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/ingestion", tags=["Ingestion"])


class SchedulerBody(BaseModel):
    """Update ingestion scheduler interval."""

    interval_minutes: int = Field(..., ge=1, description="Minutes between automatic ingestion runs.")


@router.get("/status", summary="Ingestion status for all sources")
async def ingestion_status(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Current ingestion-related fields for every registered source."""
    reg = request.app.state.source_registry
    out = []
    for r in reg.list_all(enabled_only=False):
        la = r.last_synced_at.isoformat() if r.last_synced_at else None
        out.append(
            {
                "source_id": r.source_id,
                "display_name": r.display_name,
                "source_type": r.source_type,
                "sync_status": r.sync_status,
                "last_synced_at": la,
                "total_chunks_indexed": r.total_chunks_indexed,
                "last_error": r.last_error or "",
            }
        )
    return {"sources": out}


@router.post("/sync", summary="Trigger background ingestion for all enabled sources")
async def sync_all(
    request: Request,
    full_resync: bool = Query(default=False),
    _: None = Depends(require_admin),
) -> dict:
    """
    Trigger a full ingestion cycle across all enabled sources in the background.
    """
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Ingestion pipeline unavailable")
    reg = request.app.state.source_registry
    ids = [r.source_id for r in reg.list_all(enabled_only=True)]

    async def _job() -> None:
        try:
            await pipeline.ingest_all(full_resync=full_resync)
        except Exception:
            logger.error("background_ingest_all_failed", exc_info=True)

    asyncio.create_task(_job())
    return {"triggered": True, "sources": ids}


@router.post("/sync/{source_id}", summary="Trigger background ingestion for one source")
async def sync_one(
    source_id: str,
    request: Request,
    full_resync: bool = Query(default=False),
    _: None = Depends(require_admin),
) -> dict:
    """Trigger ingestion for a single enabled source in the background."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Ingestion pipeline unavailable")
    reg = request.app.state.source_registry
    rec = reg.get(source_id)
    if rec is None or not rec.enabled:
        raise HTTPException(status_code=422, detail="source not found or not enabled")

    async def _job() -> None:
        try:
            await pipeline.ingest_source(source_id, full_resync=full_resync)
        except Exception:
            logger.error("background_ingest_source_failed source_id=%s", source_id, exc_info=True)

    asyncio.create_task(_job())
    return {"triggered": True, "source_id": source_id}


@router.get("/scheduler", summary="Scheduler status")
async def get_scheduler(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Return scheduler configuration and running status."""
    sched = getattr(request.app.state, "ingestion_scheduler", None)
    db = request.app.state.db
    db_min = db.get_scheduler_interval()
    interval = db_min if db_min is not None else settings.ingestion_interval_minutes
    if sched is None:
        return {"running": False, "interval_minutes": interval, "next_run": None}
    return {
        "running": sched.running,
        "interval_minutes": sched.interval_minutes,
        "next_run": sched.get_next_run_iso(),
    }


@router.put("/scheduler", summary="Update scheduler interval")
async def put_scheduler(
    body: SchedulerBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Update scheduler interval; takes effect immediately on the running scheduler."""
    db = request.app.state.db
    db.set_scheduler_interval(body.interval_minutes)
    sched = getattr(request.app.state, "ingestion_scheduler", None)
    if sched is not None:
        sched.reschedule(body.interval_minutes)
    db_min = db.get_scheduler_interval()
    interval = db_min if db_min is not None else settings.ingestion_interval_minutes
    if sched is None:
        return {"running": False, "interval_minutes": interval, "next_run": None}
    return {
        "running": sched.running,
        "interval_minutes": sched.interval_minutes,
        "next_run": sched.get_next_run_iso(),
    }
