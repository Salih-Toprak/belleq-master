"""Singleton factory for EmbeddingAdapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.embeddings.base import EmbeddingAdapter
from app.embeddings.ollama_adapter import OllamaEmbeddingAdapter
from app.embeddings.openai_adapter import OpenAIEmbeddingAdapter
from app.embeddings.pinecone_adapter import PineconeEmbeddingAdapter

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

_embedding_instance: EmbeddingAdapter | None = None


def get_embedding_adapter(settings: "Settings") -> EmbeddingAdapter:
    """
    Singleton. Reads EMBEDDING_BACKEND from settings.
    Supported: "ollama", "openai", "pinecone"
    Raises ValueError for unknown backend.
    """
    global _embedding_instance
    if _embedding_instance is not None:
        return _embedding_instance

    backend = (settings.embedding_backend or "").strip().lower()
    if backend == "ollama":
        _embedding_instance = OllamaEmbeddingAdapter(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embed_model,
            vector_size=settings.embedding_vector_size,
        )
        logger.info("embedding_factory_created backend=ollama")
    elif backend == "openai":
        if not (settings.openai_api_key or "").strip():
            raise ValueError("OPENAI_API_KEY is required when EMBEDDING_BACKEND=openai")
        _embedding_instance = OpenAIEmbeddingAdapter(
            api_key=settings.openai_api_key.strip(),
            model=settings.openai_embed_model,
            vector_size=settings.embedding_vector_size,
        )
        logger.info("embedding_factory_created backend=openai")
    elif backend == "pinecone":
        # Reuses PINECONE_API_KEY: a context embedding through Pinecone is
        # already storing there, so there is no second credential to manage.
        if not (settings.pinecone_api_key or "").strip():
            raise ValueError("PINECONE_API_KEY is required when EMBEDDING_BACKEND=pinecone")
        _embedding_instance = PineconeEmbeddingAdapter(
            api_key=settings.pinecone_api_key.strip(),
            model=settings.pinecone_embed_model,
            vector_size=settings.embedding_vector_size,
        )
        logger.info("embedding_factory_created backend=pinecone")
    else:
        raise ValueError(
            f"Unknown EMBEDDING_BACKEND: '{settings.embedding_backend}'. "
            f"Supported: 'ollama', 'openai', 'pinecone'"
        )
    return _embedding_instance


def reset_embedding_adapter_for_tests() -> None:
    """Clear singleton (tests only)."""
    global _embedding_instance
    _embedding_instance = None
