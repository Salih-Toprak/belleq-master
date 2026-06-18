"""Container provisioning HTTP API (admin).

Spins up / tears down belleq-user containers and keeps the registry in sync.
Called both by the platform backend's docker_manager and directly by the
dashboard through the platform proxy.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_registry, require_admin
from app.containers.provisioner import (
    ProvisionError,
    delete_user_container,
    provision_user_container,
)
from app.registry.models import ContainerRecord
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/containers", tags=["Container Provisioning"])

_VALID_TYPES = frozenset({"chatbot", "user", "agent"})


class ProvisionBody(BaseModel):
    """All fields optional — the master fills in sensible defaults.

    The platform backend sends the full set (workspace_id, caps, labels,
    qdrant_collection, …); a one-click dashboard call can send just display_name.
    """

    container_name: str | None = None
    display_name: str | None = None
    api_key: str | None = None
    user_id: str | None = None
    container_type: str = Field(default="user")
    # EC2-packing additions (sent by the platform backend):
    workspace_id: str | None = None
    plan: str | None = None
    caps: dict | None = None  # {ram_mb, cpu_vcpu, disk_gb}
    labels: dict | None = None  # belleq.* labels computed by the backend
    qdrant_collection: str | None = None
    vector_db: dict | None = None  # bring-your-own provider config
    # Conversation fact-extraction config, pushed down by the (static) backend
    # so the ephemeral master never stores the keys.
    extraction: dict | None = None
    # Rebuild: re-pull the newest published image before (re)creating the
    # container. The named -data volume is preserved, so the KB survives.
    force_pull: bool = False


@router.post("/provision", summary="Provision a new user container", status_code=201)
async def provision_container(
    body: ProvisionBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    if body.container_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"container_type must be one of: {sorted(_VALID_TYPES)}",
        )

    container_name = (body.container_name or f"belleq-user-{uuid.uuid4().hex[:8]}").strip()
    api_key = body.api_key or secrets.token_hex(32)
    user_id = body.user_id or container_name
    display_name = (body.display_name or container_name).strip()

    try:
        result = await provision_user_container(
            container_name=container_name,
            api_key=api_key,
            user_id=user_id,
            qdrant_collection=body.qdrant_collection,
            vector_db=body.vector_db,
            caps=body.caps,
            labels=body.labels,
            extraction=body.extraction,
            force_pull=body.force_pull,
        )
    except ProvisionError as e:
        logger.error("provision_failed name=%s error=%s", container_name, e)
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    registry: ContainerRegistry = get_registry(request)
    now = datetime.now(timezone.utc)
    metadata = {
        "docker_id": result["docker_id"],
        "user_id": user_id,
        "workspace_id": body.workspace_id or "",
        "plan": body.plan or "",
        "qdrant_collection": body.qdrant_collection or "",
        "caps": body.caps or {},
    }
    rec = ContainerRecord(
        container_id=container_name,
        display_name=display_name,
        container_type=body.container_type,
        base_url=result["base_url"],
        api_key=api_key,
        enabled=True,
        added_at=now,
        updated_at=now,
        metadata=metadata,
    )
    try:
        registry.add(rec)
    except ValueError:
        registry.update(
            container_name,
            {
                "display_name": display_name,
                "base_url": result["base_url"],
                "api_key": api_key,
                "enabled": True,
                "metadata": metadata,
            },
        )

    logger.info(
        "container_provisioned name=%s workspace=%s healthy=%s",
        container_name,
        body.workspace_id,
        result["healthy"],
    )
    return {
        "container_id": container_name,
        "container_name": container_name,
        "display_name": display_name,
        "container_type": body.container_type,
        "workspace_id": body.workspace_id,
        "qdrant_collection": body.qdrant_collection,
        "base_url": result["base_url"],
        "port": result["port"],
        "api_key": api_key,
        "healthy": result["healthy"],
        "status": "running" if result["healthy"] else "starting",
    }


@router.delete("/{container_name}", summary="Stop and remove a user container")
async def delete_container(
    container_name: str,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    await delete_user_container(container_name)
    registry: ContainerRegistry = get_registry(request)
    try:
        registry.remove(container_name)
    except KeyError:
        pass
    return {"removed": container_name}
