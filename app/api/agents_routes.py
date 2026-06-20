"""Master-side passthrough for agent task execution.

The agent execution engine runs inside the per-context belleq-user container.
The backend reaches it through the master (the only route to a container):

    backend  --X-Admin-Key-->  master /master/agents/{container}/run
    master   --X-Master-Key->  container /internal/agents/run

The master injects ``connectors_mcp_url`` — its own aggregated MCP endpoint for
this container (``{self_base_url}/mcp/{container}``) — so the container can reach
the workspace's connectors as tools. When ``self_base_url`` is unset, the field
is empty and the agent simply runs without connector tools.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import get_client, get_registry, require_admin
from app.clients.container_client import ContainerClient
from app.config import settings
from app.registry.registry import ContainerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/master/agents",
    tags=["Agent execution passthrough"],
    dependencies=[Depends(require_admin)],
)


@router.post("/{container_name}/run")
async def run_agent(
    container_name: str,
    payload: dict = Body(default_factory=dict),
    registry: ContainerRegistry = Depends(get_registry),
    client: ContainerClient = Depends(get_client),
) -> dict:
    rec = registry.get(container_name)
    if rec is None:
        raise HTTPException(status_code=404, detail="Context container not found")

    base = (settings.self_base_url or "").rstrip("/")
    payload = {
        **payload,
        "connectors_mcp_url": f"{base}/mcp/{container_name}" if base else "",
    }
    try:
        return await client.run_agent(rec, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_passthrough_failed container=%s err=%s", container_name, exc)
        raise HTTPException(status_code=502, detail=f"Context unavailable: {exc}") from exc
