"""FastAPI dependencies: admin auth and app.state accessors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException, Request

from app.clients.container_client import ContainerClient
from app.config import settings
from app.registry.registry import ContainerRegistry
from app.vectordb.base import VectorDBAdapter

if TYPE_CHECKING:
    from app.embeddings.base import EmbeddingAdapter
    from app.ingestion.pipeline import IngestionPipeline
    from app.ingestion.scheduler import IngestionScheduler
    from app.sources.registry import DataSourceRegistry

logger = logging.getLogger(__name__)


async def require_admin(x_admin_key: str = Header(default="", alias="X-Admin-Key")) -> None:
    """Require X-Admin-Key when ADMIN_API_KEY is set; empty admin key disables auth (dev)."""
    expected = (settings.admin_api_key or "").strip()
    if not expected:
        return
    if x_admin_key != expected:
        logger.warning("admin_auth_failed")
        raise HTTPException(status_code=403, detail="Invalid admin key")


def get_vectordb(request: Request) -> VectorDBAdapter | None:
    return getattr(request.app.state, "vectordb", None)


def require_vectordb(request: Request) -> VectorDBAdapter:
    adapter = get_vectordb(request)
    if adapter is None:
        logger.warning("vectordb_unavailable_request")
        raise HTTPException(
            status_code=503,
            detail="Vector database adapter is unavailable (see startup logs).",
        )
    return adapter


def get_registry(request: Request) -> ContainerRegistry:
    return request.app.state.registry


def get_client(request: Request) -> ContainerClient:
    return request.app.state.client


def get_embedder(request: Request) -> "EmbeddingAdapter | None":
    return getattr(request.app.state, "embedder", None)


def require_embedder(request: Request) -> "EmbeddingAdapter":
    embedder = get_embedder(request)
    if embedder is None:
        logger.warning("embedding_adapter_unavailable_request")
        raise HTTPException(
            status_code=503,
            detail="Embedding adapter is unavailable (see startup logs).",
        )
    return embedder


def get_source_registry(request: Request) -> "DataSourceRegistry":
    return request.app.state.source_registry


def get_pipeline(request: Request) -> "IngestionPipeline":
    return request.app.state.ingestion_pipeline


def get_scheduler(request: Request) -> "IngestionScheduler":
    return request.app.state.ingestion_scheduler
