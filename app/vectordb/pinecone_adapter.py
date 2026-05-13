"""Pinecone implementation of VectorDBAdapter (metadata = payload)."""

from __future__ import annotations

import logging
from typing import Any

from pinecone import Pinecone, ServerlessSpec

from app.vectordb import filters as filter_utils
from app.vectordb.base import VectorDBAdapter, VectorDBError

logger = logging.getLogger(__name__)


def _distance_to_pinecone_metric(distance: str) -> str:
    d = (distance or "Cosine").strip().lower()
    if d == "cosine":
        return "cosine"
    if d in ("euclidean", "l2"):
        return "euclidean"
    return "cosine"


class PineconeAdapter(VectorDBAdapter):
    """Pinecone serverless indexes; payload is stored as metadata."""

    backend_name = "pinecone"

    def __init__(
        self,
        api_key: str,
        environment: str = "",
        index_name: str = "",
        cloud: str = "aws",
    ) -> None:
        self._pc = Pinecone(api_key=api_key)
        self._default_index_name = (index_name or "").strip()
        self._region = (environment or "").strip() or "us-east-1"
        self._cloud = (cloud or "aws").strip() or "aws"
        logger.info(
            "pinecone_adapter_init default_index=%s region=%s cloud=%s",
            self._default_index_name or "(none)",
            self._region,
            self._cloud,
        )

    def _index_name(self, collection_name: str) -> str:
        name = (collection_name or "").strip()
        if name:
            return name
        if self._default_index_name:
            return self._default_index_name
        raise VectorDBError(
            "collection_name is required when PINECONE_INDEX_NAME is empty",
            self.backend_name,
        )

    def _index(self, collection_name: str) -> Any:
        return self._pc.Index(self._index_name(collection_name))

    async def health(self) -> dict:
        try:
            _ = self._pc.list_indexes()
            detail = self._default_index_name or "connected"
            return {"status": "ok", "backend": self.backend_name, "detail": detail}
        except Exception as e:  # noqa: BLE001
            logger.warning("pinecone_health_failed error=%s", e)
            return {
                "status": "error",
                "backend": self.backend_name,
                "detail": str(e),
            }

    async def list_collections(self) -> list[str]:
        try:
            li = self._pc.list_indexes()
            names_fn = getattr(li, "names", None)
            if callable(names_fn):
                return list(names_fn())
            return [getattr(x, "name", str(x)) for x in li]
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    def _describe_dimension(self, collection_name: str) -> int:
        desc = self._pc.describe_index(self._index_name(collection_name))
        d = getattr(desc, "dimension", None)
        if d is not None:
            return int(d)
        raise VectorDBError(
            "could not determine index dimension",
            self.backend_name,
            detail=str(desc),
        )

    async def get_collection_info(self, collection_name: str) -> dict:
        try:
            name = self._index_name(collection_name)
            desc = self._pc.describe_index(name)
            dim = int(getattr(desc, "dimension", 0) or 0)
            metric = str(getattr(desc, "metric", "cosine") or "cosine").lower()
            distance_label = "Cosine" if metric == "cosine" else "Euclidean" if metric == "euclidean" else metric
            stats = self._index(name).describe_index_stats()
            if isinstance(stats, dict):
                total = int(stats.get("total_vector_count", 0))
            else:
                total = int(getattr(stats, "total_vector_count", 0) or 0)
            status = str(getattr(desc, "status", "ready") or "ready")
            return {
                "name": name,
                "vector_count": total,
                "indexed_vector_count": total,
                "status": status,
                "vector_size": dim,
                "distance": distance_label,
            }
        except VectorDBError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "not found" in msg or "404" in msg:
                raise VectorDBError(
                    f"collection not found: {collection_name}",
                    self.backend_name,
                    detail=str(e),
                ) from e
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def create_collection(
        self,
        collection_name: str,
        vector_size: int,
        distance: str = "Cosine",
    ) -> None:
        try:
            metric = _distance_to_pinecone_metric(distance)
            self._pc.create_index(
                name=self._index_name(collection_name),
                dimension=vector_size,
                metric=metric,
                spec=ServerlessSpec(cloud=self._cloud, region=self._region),
            )
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def delete_collection(self, collection_name: str) -> None:
        try:
            self._pc.delete_index(self._index_name(collection_name))
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def upsert(self, collection_name: str, points: list[dict]) -> int:
        try:
            rows: list[dict[str, Any]] = []
            for p in points:
                pid = str(p.get("id"))
                vec = p.get("vector") or []
                meta = dict(p.get("payload") or {})
                rows.append({"id": pid, "values": vec, "metadata": meta})
            self._index(collection_name).upsert(vectors=rows)
            return len(rows)
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def search(
        self,
        collection_name: str,
        query_vector: list[float],
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[dict]:
        try:
            flt = filter_utils.build_pinecone_filter(filters)
            res = self._index(collection_name).query(
                vector=query_vector,
                top_k=top_k,
                filter=flt,
                include_metadata=True,
            )
            matches = getattr(res, "matches", None)
            if matches is None and isinstance(res, dict):
                matches = res.get("matches", [])
            matches = matches or []
            out: list[dict] = []
            for m in matches:
                mid = m.get("id") if isinstance(m, dict) else getattr(m, "id", "")
                score = m.get("score") if isinstance(m, dict) else getattr(m, "score", 0.0)
                meta = m.get("metadata") if isinstance(m, dict) else getattr(m, "metadata", {}) or {}
                out.append({"id": str(mid), "score": float(score or 0.0), "payload": dict(meta)})
            return out
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def delete_by_doc_id(self, collection_name: str, doc_id: str) -> int:
        try:
            idx = self._index(collection_name)
            flt = {"doc_id": {"$eq": doc_id}}
            dim = self._describe_dimension(self._index_name(collection_name))
            dummy = [0.0] * dim
            res = idx.query(vector=dummy, top_k=10000, filter=flt, include_metadata=False)
            matches = getattr(res, "matches", None)
            if matches is None and isinstance(res, dict):
                matches = res.get("matches", [])
            matches = matches or []
            ids = [str(m.get("id") if isinstance(m, dict) else m.id) for m in matches]
            if not ids:
                return 0
            idx.delete(ids=ids)
            return len(ids)
        except VectorDBError:
            raise
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def get_by_doc_id(self, collection_name: str, doc_id: str) -> list[dict]:
        try:
            idx = self._index(collection_name)
            dim = self._describe_dimension(self._index_name(collection_name))
            dummy = [0.0] * dim
            flt = {"doc_id": {"$eq": doc_id}}
            res = idx.query(vector=dummy, top_k=10000, filter=flt, include_metadata=True)
            matches = getattr(res, "matches", None)
            if matches is None and isinstance(res, dict):
                matches = res.get("matches", [])
            matches = matches or []
            out: list[dict] = []
            for m in matches:
                mid = m.get("id") if isinstance(m, dict) else getattr(m, "id", "")
                meta = m.get("metadata") if isinstance(m, dict) else getattr(m, "metadata", {}) or {}
                out.append({"id": str(mid), "payload": dict(meta)})
            return out
        except VectorDBError:
            raise
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def count(self, collection_name: str, filters: dict | None = None) -> int:
        # Pinecone describe_index_stats does not support arbitrary metadata filters.
        if filters:
            raise VectorDBError(
                "count with metadata filters is not supported by Pinecone; "
                "omit filters or use Qdrant backend",
                self.backend_name,
                detail="filters_not_supported",
            )
        try:
            stats = self._index(collection_name).describe_index_stats()
            if isinstance(stats, dict):
                return int(stats.get("total_vector_count", 0))
            return int(getattr(stats, "total_vector_count", 0))
        except Exception as e:  # noqa: BLE001
            raise VectorDBError(str(e), self.backend_name, detail=str(e)) from e

    async def scroll(
        self,
        collection_name: str,
        filters: dict | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        raise VectorDBError(
            "scroll is not supported by Pinecone; use Qdrant or implement export tooling",
            self.backend_name,
            detail="scroll_not_supported",
        )
