"""Singleton factory for the configured VectorDBAdapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.vectordb.base import VectorDBAdapter
from app.vectordb.pinecone_adapter import PineconeAdapter
from app.vectordb.qdrant_adapter import QdrantAdapter

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

_adapter_instance: VectorDBAdapter | None = None


def get_vector_db_adapter(settings: "Settings") -> VectorDBAdapter:
    """
    Singleton factory. Returns same instance on subsequent calls.
    Reads VECTORDB_BACKEND from settings.
    Supported values: "qdrant", "pinecone"
    Raises ValueError for unknown backend.
    """
    global _adapter_instance
    if _adapter_instance is not None:
        return _adapter_instance

    backend = (settings.vectordb_backend or "").strip().lower()
    if backend == "qdrant":
        key = (settings.qdrant_api_key or "").strip() or None
        _adapter_instance = QdrantAdapter(
            url=settings.qdrant_url,
            api_key=key,
        )
        logger.info("vectordb_factory_created backend=qdrant url=%s", settings.qdrant_url)
    elif backend == "pinecone":
        if not (settings.pinecone_api_key or "").strip():
            raise ValueError("PINECONE_API_KEY is required when VECTORDB_BACKEND=pinecone")
        _adapter_instance = PineconeAdapter(
            api_key=settings.pinecone_api_key.strip(),
            environment=settings.pinecone_environment,
            index_name=settings.pinecone_index_name,
            cloud=settings.pinecone_cloud,
        )
        logger.info("vectordb_factory_created backend=pinecone index=%s", settings.pinecone_index_name)
    else:
        raise ValueError(
            f"Unknown VECTORDB_BACKEND: '{settings.vectordb_backend}'. "
            f"Supported: 'qdrant', 'pinecone'"
        )

    return _adapter_instance
