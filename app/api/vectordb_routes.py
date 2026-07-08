"""Master vector database HTTP API (admin)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import get_vectordb, require_admin, require_vectordb
from app.vectordb.base import VectorDBAdapter, VectorDBError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/master/vectordb", tags=["Vector DB"])


def _vdb_http_error(e: VectorDBError) -> HTTPException:
    """Map a VectorDBError to the right HTTP status.

    A missing collection must surface as 404, not 503: contexts get their
    collection name at provision time but Qdrant only creates it on the first
    write, so "collection doesn't exist" is the normal empty state the
    dashboard renders as "no records yet". qdrant-client wraps the upstream
    404 in an UnexpectedResponse whose text carries the raw body.
    """
    msg = str(e).lower()
    if "not found" in msg or "doesn't exist" in msg or "does not exist" in msg:
        return HTTPException(status_code=404, detail=str(e))
    return HTTPException(status_code=503, detail=str(e))


def _must_filter(source: str | None, department: str | None) -> dict | None:
    must: list[dict] = []
    if source:
        must.append({"field": "source", "value": source})
    if department:
        must.append({"field": "department", "value": department})
    if not must:
        return None
    return {"must": must}


class CreateCollectionBody(BaseModel):
    """Request body for creating a vector collection/index."""

    name: str = Field(..., description="Collection (Qdrant) or index (Pinecone) name.")
    vector_size: int = Field(..., ge=1, description="Embedding vector dimension.")
    distance: str = Field(
        default="Cosine",
        description='Distance metric, e.g. "Cosine" or "Euclidean".',
    )


@router.get("/health", summary="Check vector DB connection")
async def vectordb_health(
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter | None = Depends(get_vectordb),
) -> dict:
    """Check vector DB connection."""
    if adapter is None:
        raise HTTPException(
            status_code=503,
            detail="Vector database adapter is unavailable (see startup logs).",
        )
    return await adapter.health()


@router.get("/collections", summary="List all collections/indexes")
async def list_collections(
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """List all collections/indexes."""
    try:
        names = await adapter.list_collections()
        return {"collections": names}
    except VectorDBError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/collections/{collection_name}", summary="Detailed info about a collection")
async def get_collection(
    collection_name: str,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """Detailed info about a collection."""
    try:
        return await adapter.get_collection_info(collection_name)
    except VectorDBError as e:
        raise _vdb_http_error(e) from e


@router.get("/collections/{collection_name}/count", summary="Point count with optional filters")
async def count_points(
    collection_name: str,
    source: Annotated[str | None, Query(description="Filter payload field `source`.")] = None,
    department: Annotated[str | None, Query(description="Filter payload field `department`.")] = None,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """Document count. Optional filter by source or department."""
    flt = _must_filter(source, department)
    try:
        n = await adapter.count(collection_name, flt)
        return {"collection": collection_name, "count": n, "filters": flt}
    except VectorDBError as e:
        raise _vdb_http_error(e) from e


@router.get("/collections/{collection_name}/docs", summary="Paginated payload listing (no vectors)")
async def list_docs(
    collection_name: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    department: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """
    Paginated document listing. Payload only, no vectors.
    Query params: limit (default 50), offset (default 0), department, source.
    """
    flt = _must_filter(source, department)
    try:
        total = await adapter.count(collection_name, flt)
        rows = await adapter.scroll(collection_name, filters=flt, limit=limit, offset=offset)
        # ``scroll`` yields {id, payload}; the dashboard expects each document's
        # payload fields at the top level (doc_title, source, indexed_at, …), so
        # flatten the payload up and keep the point id alongside it.
        documents = [{"id": r.get("id"), **(r.get("payload") or {})} for r in rows]
        return {
            "collection": collection_name,
            "total": total,
            "offset": offset,
            "limit": limit,
            "documents": documents,
        }
    except VectorDBError as e:
        raise _vdb_http_error(e) from e


@router.get("/collections/{collection_name}/docs/{doc_id}", summary="All chunks for a doc_id")
async def get_doc_chunks(
    collection_name: str,
    doc_id: str,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """All chunks for a specific doc_id."""
    try:
        chunks = await adapter.get_by_doc_id(collection_name, doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail=f"No vectors for doc_id={doc_id}")
        return {"doc_id": doc_id, "chunk_count": len(chunks), "chunks": chunks}
    except HTTPException:
        raise
    except VectorDBError as e:
        raise _vdb_http_error(e) from e


@router.delete("/collections/{collection_name}/docs/{doc_id}", summary="Delete vectors for doc_id")
async def delete_doc_vectors(
    collection_name: str,
    doc_id: str,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """
    Delete all vector DB points for a doc_id.
    Removes from vector DB only — user container records unaffected.
    """
    try:
        removed = await adapter.delete_by_doc_id(collection_name, doc_id)
        return {"deleted": doc_id, "points_removed": removed}
    except VectorDBError as e:
        raise _vdb_http_error(e) from e


@router.post("/collections", summary="Create a new collection")
async def create_collection(
    body: CreateCollectionBody,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """Create a new collection/index."""
    try:
        await adapter.create_collection(
            collection_name=body.name,
            vector_size=body.vector_size,
            distance=body.distance,
        )
        return {"created": body.name, "vector_size": body.vector_size, "distance": body.distance}
    except VectorDBError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.delete("/collections/{collection_name}", summary="Delete entire collection")
async def delete_collection(
    collection_name: str,
    _: None = Depends(require_admin),
    adapter: VectorDBAdapter = Depends(require_vectordb),
) -> dict:
    """Delete entire collection. Irreversible."""
    try:
        await adapter.delete_collection(collection_name)
        return {"deleted": collection_name}
    except VectorDBError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
