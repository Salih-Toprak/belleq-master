"""OpenAI embeddings API (async).

Model output sizes (reference):
- text-embedding-3-small -> 1536 (default dimensions)
- text-embedding-3-large -> 3072
- text-embedding-ada-002 -> 1536
"""

from __future__ import annotations

import logging
from typing import Any

from openai import APIError, AsyncOpenAI

from app.embeddings.base import EmbeddingAdapter, EmbeddingError

logger = logging.getLogger(__name__)


class OpenAIEmbeddingAdapter(EmbeddingAdapter):
    """OpenAI text-embedding-* models."""

    backend_name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        vector_size: int = 1536,
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._vector_size = int(vector_size)
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        logger.info("openai_embedding_init model=%s vector_size=%s", model, vector_size)

    @property
    def vector_size(self) -> int:
        return self._vector_size

    @property
    def model_name(self) -> str:
        return self._model

    def _parse_batch(self, response: Any) -> list[list[float]]:
        data_list = getattr(response, "data", None) or []
        by_index: dict[int, list[float]] = {}
        for item in data_list:
            idx = getattr(item, "index", 0)
            emb = getattr(item, "embedding", None)
            if emb is None:
                continue
            by_index[int(idx)] = list(map(float, emb))
        return [by_index[i] for i in sorted(by_index)]

    async def embed_one(self, text: str) -> list[float]:
        try:
            resp = await self._client.embeddings.create(model=self._model, input=text)
            vecs = self._parse_batch(resp)
            if not vecs:
                raise EmbeddingError("empty embedding response", self.backend_name)
            return vecs[0]
        except APIError as e:
            raise EmbeddingError(str(e), self.backend_name, detail=str(e)) from e
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(str(e), self.backend_name, detail=str(e)) from e

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = await self._client.embeddings.create(model=self._model, input=texts)
            out = self._parse_batch(resp)
            if len(out) != len(texts):
                raise EmbeddingError(
                    f"embedding count mismatch: got {len(out)} expected {len(texts)}",
                    self.backend_name,
                )
            return out
        except APIError as e:
            raise EmbeddingError(str(e), self.backend_name, detail=str(e)) from e
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            out2: list[list[float]] = []
            for t in texts:
                out2.append(await self.embed_one(t))
            return out2

    async def health(self) -> dict:
        try:
            v = await self.embed_one("health check")
            return {
                "status": "ok",
                "backend": self.backend_name,
                "model": self._model,
                "vector_size": len(v) if v else self._vector_size,
                "detail": "openai",
            }
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "backend": self.backend_name,
                "model": self._model,
                "vector_size": self._vector_size,
                "detail": str(e),
            }
