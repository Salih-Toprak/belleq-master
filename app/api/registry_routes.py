"""Container registry HTTP API (admin)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_registry, require_admin
from app.database import MasterDB
from app.registry.models import ContainerRecord
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/registry", tags=["Container Registry"])

_VALID_TYPES = frozenset({"chatbot", "user", "agent"})


def _record_to_dict(r: ContainerRecord) -> dict[str, Any]:
    return {
        "container_id": r.container_id,
        "display_name": r.display_name,
        "container_type": r.container_type,
        "base_url": r.base_url,
        "api_key": r.api_key,
        "enabled": r.enabled,
        "added_at": r.added_at.isoformat() if r.added_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "metadata": r.metadata or {},
    }


class RegisterContainerBody(BaseModel):
    """Body for registering a new user container."""

    container_id: str = Field(..., description="Unique stable id for this container.")
    display_name: str = Field(..., description="Human-readable name for dashboards.")
    container_type: str = Field(
        ...,
        description='One of: "chatbot", "user", "agent".',
    )
    base_url: str = Field(
        ...,
        description="Base URL including scheme and port, e.g. http://user-001:8100",
    )
    api_key: str = Field(default="", description="Optional shared secret sent as X-Container-Key.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary JSON metadata.")


class PatchContainerBody(BaseModel):
    """Subset of fields that may be updated on a container."""

    display_name: str | None = None
    container_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


@router.get("/containers", summary="List registered containers")
async def list_containers(
    enabled_only: bool = False,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """List all registered containers."""
    rows = registry.list_enabled() if enabled_only else registry.list_all()
    return {"count": len(rows), "containers": [_record_to_dict(r) for r in rows]}


@router.get("/containers/{container_id}", summary="Get one container record")
async def get_container(
    container_id: str,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """Single container record."""
    r = registry.get(container_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Container not found")
    return _record_to_dict(r)


@router.post("/containers", summary="Register a new container", status_code=201)
async def register_container(
    body: RegisterContainerBody,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """
    Register a new user container.
    container_id must be unique. added_at and updated_at are set automatically.
    """
    if body.container_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"container_type must be one of: {sorted(_VALID_TYPES)}",
        )
    now = datetime.now(timezone.utc)
    rec = ContainerRecord(
        container_id=body.container_id.strip(),
        display_name=body.display_name,
        container_type=body.container_type,
        base_url=body.base_url.rstrip("/"),
        api_key=body.api_key or "",
        enabled=True,
        added_at=now,
        updated_at=now,
        metadata=dict(body.metadata or {}),
    )
    try:
        registry.add(rec)
    except ValueError:
        raise HTTPException(status_code=422, detail="container_id already exists") from None
    created = registry.get(rec.container_id)
    assert created is not None
    logger.info("container_registered container_id=%s", rec.container_id)
    return _record_to_dict(created)


@router.patch("/containers/{container_id}", summary="Update a container")
async def patch_container(
    container_id: str,
    body: PatchContainerBody,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """Update any fields except container_id and added_at."""
    updates = body.model_dump(exclude_unset=True)
    if "base_url" in updates and updates["base_url"] is not None:
        updates["base_url"] = str(updates["base_url"]).rstrip("/")
    if not updates:
        r = registry.get(container_id)
        if r is None:
            raise HTTPException(status_code=404, detail="Container not found")
        return _record_to_dict(r)
    if "container_type" in updates and updates["container_type"] not in _VALID_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"container_type must be one of: {sorted(_VALID_TYPES)}",
        )
    try:
        updated = registry.update(container_id, updates)
    except KeyError:
        raise HTTPException(status_code=404, detail="Container not found") from None
    return _record_to_dict(updated)


@router.delete("/containers/{container_id}", summary="Remove a container")
async def delete_container(
    container_id: str,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """Remove container from registry."""
    try:
        registry.remove(container_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Container not found") from None
    return {"removed": container_id}


@router.post("/containers/{container_id}/enable", summary="Enable a container")
async def enable_container(
    container_id: str,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """Enable a disabled container."""
    try:
        registry.enable(container_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Container not found") from None
    r = registry.get(container_id)
    assert r is not None
    return _record_to_dict(r)


@router.post("/containers/{container_id}/disable", summary="Disable a container")
async def disable_container(
    container_id: str,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """Disable a container (excluded from aggregates)."""
    try:
        registry.disable(container_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Container not found") from None
    r = registry.get(container_id)
    assert r is not None
    return _record_to_dict(r)


@router.post("/containers/{container_id}/health-check", summary="Ping container /health now")
async def health_check_one(
    container_id: str,
    request: Request,
    _: None = Depends(require_admin),
    registry: ContainerRegistry = Depends(get_registry),
) -> dict:
    """
    Ping this container's /health endpoint immediately.
    Logs result to health_check_log table.
    Returns live result — not cached.
    """
    rec = registry.get(container_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Container not found")
    client = request.app.state.client
    reachable, response_ms, err = await client.health_check(rec)
    db: MasterDB = request.app.state.db
    db.log_health_check(container_id, reachable, response_ms, err)
    checked_at = datetime.now(timezone.utc).isoformat()
    return {
        "container_id": container_id,
        "reachable": reachable,
        "response_ms": response_ms,
        "checked_at": checked_at,
        "error": err,
    }


@router.get("/containers/{container_id}/health-history", summary="Recent health checks for a container")
async def health_history(
    container_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    _: None = Depends(require_admin),
) -> dict:
    """Last N health check results for a container."""
    db: MasterDB = request.app.state.db
    hist = db.get_health_history(container_id, limit=limit)
    return {"container_id": container_id, "history": hist}