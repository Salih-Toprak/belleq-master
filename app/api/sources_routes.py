"""Data source configuration HTTP API."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.sources.models import DataSourceRecord
from app.sources.notion_source import NotionSource
from app.sources.registry import DataSourceRegistry
from app.sources.slack_source import SlackSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/sources", tags=["Data Sources"])


def _redact_credentials(creds: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (creds or {}).items():
        if v:
            out[k] = "***set***"
        else:
            out[k] = ""
    return out


def _source_to_public(rec: DataSourceRecord) -> dict[str, Any]:
    la = rec.last_synced_at.isoformat() if rec.last_synced_at else None
    aa = rec.added_at.isoformat() if rec.added_at else None
    ua = rec.updated_at.isoformat() if rec.updated_at else None
    return {
        "source_id": rec.source_id,
        "source_type": rec.source_type,
        "display_name": rec.display_name,
        "enabled": rec.enabled,
        "added_at": aa,
        "updated_at": ua,
        "credentials": _redact_credentials(rec.credentials),
        "access_control": rec.access_control or {},
        "last_synced_at": la,
        "sync_status": rec.sync_status,
        "total_chunks_indexed": rec.total_chunks_indexed,
        "last_error": rec.last_error or "",
        "metadata": rec.metadata or {},
    }


class SlackSourceCreate(BaseModel):
    """Register a Slack-backed data source."""

    source_id: str
    source_type: str = Field(default="slack")
    display_name: str
    credentials: dict[str, Any]
    access_control: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotionSourceCreate(BaseModel):
    """Register a Notion-backed data source."""

    source_id: str
    source_type: str = Field(default="notion")
    display_name: str
    credentials: dict[str, Any]
    access_control: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PatchSourceBody(BaseModel):
    """Update mutable fields on a data source."""

    display_name: str | None = None
    enabled: bool | None = None
    access_control: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


@router.get("", summary="List configured data sources")
async def list_sources(
    request: Request,
    enabled_only: bool = False,
    source_type: Annotated[str | None, Query()] = None,
    _: None = Depends(require_admin),
) -> dict:
    """List all configured data sources (credentials redacted)."""
    reg: DataSourceRegistry = request.app.state.source_registry
    rows = reg.list_all(enabled_only=enabled_only, source_type=source_type)
    return {"count": len(rows), "sources": [_source_to_public(r) for r in rows]}


@router.get("/{source_id}", summary="Get one data source")
async def get_source(
    source_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Get single source configuration (credentials redacted)."""
    reg: DataSourceRegistry = request.app.state.source_registry
    r = reg.get(source_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return _source_to_public(r)


@router.post("", summary="Register a new data source", status_code=201)
async def create_source(
    body: dict[str, Any],
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """
    Register a new data source after validating credentials against the external API.
    """
    st = str(body.get("source_type", "")).strip().lower()
    if st == "slack":
        parsed = SlackSourceCreate.model_validate(body)
        rec = DataSourceRecord(
            source_id=parsed.source_id.strip(),
            source_type="slack",
            display_name=parsed.display_name,
            credentials=dict(parsed.credentials),
            access_control=dict(parsed.access_control or {}),
            metadata=dict(parsed.metadata or {}),
        )
        src = SlackSource(rec)
        v = await src.validate_credentials()
        await src.close()
        if not v.get("valid"):
            raise HTTPException(status_code=422, detail=v.get("error", "credential validation failed"))
    elif st == "notion":
        parsed = NotionSourceCreate.model_validate(body)
        rec = DataSourceRecord(
            source_id=parsed.source_id.strip(),
            source_type="notion",
            display_name=parsed.display_name,
            credentials=dict(parsed.credentials),
            access_control=dict(parsed.access_control or {}),
            metadata=dict(parsed.metadata or {}),
        )
        src = NotionSource(rec)
        v = await src.validate_credentials()
        await src.close()
        if not v.get("valid"):
            raise HTTPException(status_code=422, detail=v.get("error", "credential validation failed"))
    else:
        raise HTTPException(status_code=422, detail="source_type must be slack or notion")

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rec.added_at = now
    rec.updated_at = now
    reg: DataSourceRegistry = request.app.state.source_registry
    try:
        reg.add(rec)
    except ValueError:
        raise HTTPException(status_code=422, detail="source_id already exists") from None
    created = reg.get(rec.source_id)
    assert created is not None
    logger.info("data_source_registered source_id=%s type=%s", rec.source_id, st)
    return _source_to_public(created)


@router.patch("/{source_id}", summary="Update a data source")
async def patch_source(
    source_id: str,
    body: PatchSourceBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """
    Update display_name, enabled, access_control, or metadata.
    Credentials and source_type cannot be changed — delete and recreate instead.
    """
    reg: DataSourceRegistry = request.app.state.source_registry
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        r = reg.get(source_id)
        if r is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return _source_to_public(r)
    try:
        updated = reg.update(source_id, updates)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found") from None
    return _source_to_public(updated)


@router.delete("/{source_id}", summary="Remove a data source")
async def delete_source(
    source_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Remove a source and its sync log history."""
    reg: DataSourceRegistry = request.app.state.source_registry
    try:
        reg.remove(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found") from None
    return {"removed": source_id}


@router.post("/{source_id}/enable", summary="Enable a data source")
async def enable_source(
    source_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg: DataSourceRegistry = request.app.state.source_registry
    try:
        reg.enable(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found") from None
    r = reg.get(source_id)
    assert r is not None
    return _source_to_public(r)


@router.post("/{source_id}/disable", summary="Disable a data source")
async def disable_source(
    source_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    reg: DataSourceRegistry = request.app.state.source_registry
    try:
        reg.disable(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found") from None
    r = reg.get(source_id)
    assert r is not None
    return _source_to_public(r)


@router.post("/{source_id}/validate", summary="Re-validate data source credentials")
async def validate_source(
    source_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Re-validate credentials for an existing source."""
    reg: DataSourceRegistry = request.app.state.source_registry
    rec = reg.get(source_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if rec.source_type == "slack":
        src = SlackSource(rec)
        v = await src.validate_credentials()
        await src.close()
        return {"valid": bool(v.get("valid")), "detail": v.get("error") or v.get("workspace", "")}
    if rec.source_type == "notion":
        src = NotionSource(rec)
        v = await src.validate_credentials()
        await src.close()
        return {"valid": bool(v.get("valid")), "detail": v.get("error", "")}
    raise HTTPException(status_code=422, detail="unsupported source_type")


@router.get("/{source_id}/sync-history", summary="Recent sync log for a source")
async def sync_history(
    source_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    _: None = Depends(require_admin),
) -> dict:
    """Last N sync log entries for this source."""
    reg: DataSourceRegistry = request.app.state.source_registry
    if reg.get(source_id) is None:
        raise HTTPException(status_code=404, detail="Source not found")
    hist = reg.get_sync_history(source_id, limit=limit)
    rows = []
    for e in hist:
        rows.append(
            {
                "log_id": e.log_id,
                "source_id": e.source_id,
                "started_at": e.started_at.isoformat() if e.started_at else None,
                "finished_at": e.finished_at.isoformat() if e.finished_at else None,
                "status": e.status,
                "chunks_upserted": e.chunks_upserted,
                "chunks_deleted": e.chunks_deleted,
                "docs_processed": e.docs_processed,
                "error_message": e.error_message,
                "metadata": e.metadata,
            }
        )
    return {"source_id": source_id, "history": rows}
