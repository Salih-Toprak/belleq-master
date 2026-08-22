# Shared adapter — keep in sync with belleq-master/app/embeddings/
"""Pinecone Inference — hosted embedding models.

Lets a context use Pinecone for both storage and embeddings, so no separate
embedding provider (and no local Ollama) is needed.

Talks to the Inference REST API directly with httpx rather than through the
Pinecone SDK: the pinned ``pinecone-client==3.2.2`` has no ``inference`` support
at all, and bumping it would mean re-validating the vector-store adapter that
already works. Only the API version below is a compatibility surface.

Model output sizes (reference):
- multilingual-e5-large      -> 1024 (fixed)
- llama-text-embed-v2        -> 1024 default; also 384, 512, 768, 2048

Note ``input_type``: these are asymmetric models, so documents must be embedded
as "passage" and searches as "query". Getting that backwards degrades retrieval
quietly rather than failing, which is why the two paths are kept separate here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.embeddings.base import EmbeddingAdapter, EmbeddingError

logger = logging.getLogger(__name__)

EMBED_URL = "https://api.pinecone.io/embed"

# Pinned deliberately: the response shape is versioned, and a floating version
# could change `data[].values` under us.
API_VERSION = "2025-04"

# The Inference API rejects larger batches; chunk rather than fail on a big
# document.
MAX_BATCH = 96

# Models whose output dimension can be requested. Everything else has a fixed
# size and rejects a `dimension` parameter.
_VARIABLE_DIMENSION_MODELS = frozenset({"llama-text-embed-v2"})

INPUT_TYPE_PASSAGE = "passage"
INPUT_TYPE_QUERY = "query"


class PineconeEmbeddingAdapter(EmbeddingAdapter):
    """Pinecone-hosted embedding models."""

    backend_name = "pinecone"

    def __init__(
        self,
        api_key: str,
        model: str = "multilingual-e5-large",
        vector_size: int = 1024,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._vector_size = int(vector_size)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        logger.info(
            "pinecone_embedding_init model=%s vector_size=%s", model, vector_size
        )

    @property
    def vector_size(self) -> int:
        return self._vector_size

    @property
    def model_name(self) -> str:
        return self._model

    # ── request building / parsing ───────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Api-Key": self._api_key,
            "Content-Type": "application/json",
            "X-Pinecone-API-Version": API_VERSION,
        }

    def _payload(self, texts: list[str], input_type: str) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "input_type": input_type,
            # Long chunks are truncated rather than rejected; the caller already
            # chunks to ~512 chars, so this only guards outliers.
            "truncate": "END",
        }
        if self._model in _VARIABLE_DIMENSION_MODELS:
            parameters["dimension"] = self._vector_size
        return {
            "model": self._model,
            "parameters": parameters,
            "inputs": [{"text": t} for t in texts],
        }

    def _parse(self, body: Any, expected: int) -> list[list[float]]:
        data = (body or {}).get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise EmbeddingError("malformed embedding response", self.backend_name)

        vectors: list[list[float]] = []
        for item in data:
            values = item.get("values") if isinstance(item, dict) else None
            if values is None:
                # A sparse model returns indices/values instead of a dense
                # vector, which this pipeline cannot store.
                raise EmbeddingError(
                    f"model {self._model} did not return dense vectors — "
                    "sparse models are not supported",
                    self.backend_name,
                )
            vectors.append([float(v) for v in values])

        if len(vectors) != expected:
            raise EmbeddingError(
                f"embedding count mismatch: got {len(vectors)} expected {expected}",
                self.backend_name,
            )
        return vectors

    def _explain(self, response: httpx.Response) -> str:
        """Pinecone puts a useful message in the body; surface it, not just 4xx."""
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text.strip()[:200]}"
        detail = body.get("message") or body.get("error") or body
        return f"HTTP {response.status_code}: {detail}"

    @staticmethod
    def _chunks(texts: list[str]) -> list[list[str]]:
        return [texts[i : i + MAX_BATCH] for i in range(0, len(texts), MAX_BATCH)]

    # ── async interface ──────────────────────────────────────────
    def _async_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _embed_async(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        client = self._async_client()
        for chunk in self._chunks(texts):
            try:
                response = await client.post(
                    EMBED_URL, json=self._payload(chunk, input_type), headers=self._headers()
                )
            except Exception as e:  # noqa: BLE001
                raise EmbeddingError(str(e), self.backend_name, detail=str(e)) from e
            if response.status_code >= 400:
                message = self._explain(response)
                raise EmbeddingError(message, self.backend_name, detail=message)
            out.extend(self._parse(response.json(), len(chunk)))
        return out

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self._embed_async([text], INPUT_TYPE_PASSAGE)
        if not vectors:
            raise EmbeddingError("empty embedding response", self.backend_name)
        return vectors[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed_async(texts, INPUT_TYPE_PASSAGE)

    async def health(self) -> dict:
        try:
            vector = await self._embed_async(["health check"], INPUT_TYPE_QUERY)
            size = len(vector[0]) if vector else 0
            if size != self._vector_size:
                # Same guard the Ollama adapter applies: a size mismatch would
                # otherwise only surface as silently wrong retrieval.
                return {
                    "status": "error",
                    "backend": self.backend_name,
                    "model": self._model,
                    "vector_size": size,
                    "detail": (
                        f"vector length mismatch: got {size} expected {self._vector_size}"
                    ),
                }
            return {
                "status": "ok",
                "backend": self.backend_name,
                "model": self._model,
                "vector_size": size,
                "detail": "pinecone inference",
            }
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "backend": self.backend_name,
                "model": self._model,
                "vector_size": self._vector_size,
                "detail": str(e),
            }

    async def aclose(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            # Shutdown-only path. Closing from a different event loop than the
            # one the client was created in raises, and a failed cleanup must
            # not turn into a traceback on the way out.
            logger.debug("pinecone_embedding_close_failed", exc_info=True)
        finally:
            self._client = None

    # ── sync interface (rag-wiki / LangChain) ────────────────────
    def _embed_sync(self, texts: list[str], input_type: str) -> list[list[float]]:
        """Short-lived sync client, mirroring the Ollama adapter.

        Called from worker threads, so it must not touch the shared async client
        or spin up event loops — doing either corrupts the async httpx client the
        vector-store adapter shares.
        """
        out: list[list[float]] = []
        try:
            with httpx.Client(timeout=self._timeout) as client:
                for chunk in self._chunks(texts):
                    response = client.post(
                        EMBED_URL,
                        json=self._payload(chunk, input_type),
                        headers=self._headers(),
                    )
                    if response.status_code >= 400:
                        message = self._explain(response)
                        raise EmbeddingError(message, self.backend_name, detail=message)
                    out.extend(self._parse(response.json(), len(chunk)))
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(str(e), self.backend_name) from e
        return out

    def embed_query(self, text: str) -> list[float]:
        """Embed a search. Uses input_type=query — see the module docstring."""
        vectors = self._embed_sync([text], INPUT_TYPE_QUERY)
        if not vectors:
            raise EmbeddingError("empty embedding response", self.backend_name)
        return vectors[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents. Uses input_type=passage — see the module docstring."""
        if not texts:
            return []
        return self._embed_sync(texts, INPUT_TYPE_PASSAGE)
