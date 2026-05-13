"""Embedding adapter contract."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Normalized error for embedding backends."""

    def __init__(self, message: str, backend: str, detail: str = "") -> None:
        self.backend = backend
        self.detail = detail
        super().__init__(message)
        logger.debug("embedding_error backend=%s message=%s", backend, message)


class EmbeddingAdapter(ABC):
    """Abstract text embedding backend."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Identifier: 'ollama', 'openai'."""

    @property
    @abstractmethod
    def vector_size(self) -> int:
        """Dimensionality of produced embeddings."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier string."""

    @abstractmethod
    async def embed_one(self, text: str) -> list[float]:
        """Embed a single string. Raises EmbeddingError on failure."""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed many strings preserving order. Raises EmbeddingError on failure."""

    @abstractmethod
    async def health(self) -> dict:
        """
        Returns {"status": "ok"|"error", "backend": str, "model": str,
                 "vector_size": int, "detail": str}
        Never raises.
        """
