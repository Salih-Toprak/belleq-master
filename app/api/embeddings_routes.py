"""Embedding adapter HTTP API."""

from __future__ import annotations

import copy
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import require_admin
from app.config import settings
from app.embeddings.base import EmbeddingAdapter
from app.embeddings.ollama_adapter import OllamaEmbeddingAdapter
from app.embeddings.openai_adapter import OpenAIEmbeddingAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/embeddings", tags=["Embeddings"])


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(cfg or {})
    ak = out.get("api_key")
    if ak:
        out["api_key"] = "***set***"
    elif "api_key" in out:
        out["api_key"] = ""
    return out


def _merged_embedding_public(db: Any) -> dict[str, Any]:
    row = db.get_embedding_config()
    if row:
        cfg = copy.deepcopy(row.get("config") or {})
        return {
            "backend": row["backend"],
            "model": row["model_name"],
            "vector_size": int(row["vector_size"]),
            "config": _redact_config(cfg),
        }
    return {
        "backend": settings.embedding_backend,
        "model": (
            settings.ollama_embed_model
            if settings.embedding_backend == "ollama"
            else settings.openai_embed_model
        ),
        "vector_size": int(settings.embedding_vector_size),
        "config": _redact_config(
            {
                "base_url": settings.ollama_base_url,
                "api_key": settings.openai_api_key,
            }
        ),
    }


def _build_temp_adapter(body: "EmbeddingConfigBody") -> EmbeddingAdapter:
    b = (body.backend or "").strip().lower()
    if b == "ollama":
        base = (body.config or {}).get("base_url") or settings.ollama_base_url
        if not str(base).strip():
            raise ValueError("config.base_url is required for ollama")
        return OllamaEmbeddingAdapter(
            base_url=str(base).rstrip("/"),
            model=body.model,
            vector_size=body.vector_size,
        )
    if b == "openai":
        key = (body.config or {}).get("api_key") or ""
        if not str(key).strip():
            raise ValueError("config.api_key is required for openai")
        return OpenAIEmbeddingAdapter(
            api_key=str(key).strip(),
            model=body.model,
            vector_size=body.vector_size,
        )
    raise ValueError("backend must be ollama or openai")


class EmbeddingConfigBody(BaseModel):
    """Persisted embedding configuration (stored in master DB)."""

    backend: str = Field(..., description='"ollama" or "openai"')
    model: str = Field(..., description="Embedding model id for the selected backend.")
    vector_size: int = Field(..., gt=0, description="Must match model output dimension.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description='Backend-specific settings: {"base_url": "..."} or {"api_key": "..."}.',
    )


class EmbedTestBody(BaseModel):
    """Sample text for a one-off embedding test."""

    text: str = Field(..., min_length=1, description="Arbitrary UTF-8 text to embed.")


@router.get("/health", summary="Check embedding adapter health")
async def embeddings_health(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """Check embedding adapter health."""
    emb: EmbeddingAdapter | None = getattr(request.app.state, "embedder", None)
    if emb is None:
        raise HTTPException(status_code=503, detail="Embedding adapter unavailable")
    return await emb.health()


@router.get("/config", summary="Get embedding configuration (credentials redacted)")
async def get_embedding_config(
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """
    Return current embedding configuration.
    Reads from embedding_config table in master DB, falling back to environment settings.
    """
    db = request.app.state.db
    return _merged_embedding_public(db)


@router.put("/config", summary="Update embedding configuration (requires restart to apply)")
async def put_embedding_config(
    body: EmbeddingConfigBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """
    Update embedding backend configuration.
    Validates the new config by attempting a health check before saving.
    Does NOT hot-swap the running adapter — requires process restart.
    """
    b = (body.backend or "").strip().lower()
    if b not in ("ollama", "openai"):
        raise HTTPException(status_code=422, detail="backend must be ollama or openai")
    try:
        temp = _build_temp_adapter(body)
        h = await temp.health()
        if hasattr(temp, "aclose"):
            await temp.aclose()
        if h.get("status") != "ok":
            raise HTTPException(status_code=422, detail=h.get("detail", "health check failed"))
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e)) from e

    db = request.app.state.db
    db.set_embedding_config(
        backend=b,
        model_name=body.model,
        vector_size=body.vector_size,
        config=body.config or {},
    )
    out = _merged_embedding_public(db)
    out["restart_required"] = True
    out["notice"] = "Restart the master container to load the new embedding adapter."
    return out


@router.post("/test", summary="Test current embedding adapter with sample text")
async def embed_test(
    body: EmbedTestBody,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    """
    Test the current embedding adapter with a sample text.
    Returns only the first five dimensions of the embedding vector.
    """
    emb: EmbeddingAdapter | None = getattr(request.app.state, "embedder", None)
    if emb is None:
        raise HTTPException(status_code=503, detail="Embedding adapter unavailable")
    vec = await emb.embed_one(body.text)
    preview = [float(x) for x in vec[:5]]
    return {
        "backend": emb.backend_name,
        "model": emb.model_name,
        "vector_size": len(vec),
        "preview": preview,
    }
