"""Shared text chunking and vector payload metadata for ingestion."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
_MAX_CHUNK_SOFT = int(CHUNK_SIZE * 1.2)


def chunk_text(text: str) -> list[str]:
    """
    Split text into overlapping chunks of ~CHUNK_SIZE characters.
    Split on sentence boundaries (". ") where possible.
    Guarantee: no chunk exceeds CHUNK_SIZE * 1.2 characters.
    """
    t = (text or "").strip()
    if not t:
        return [""]
    if len(t) <= CHUNK_SIZE:
        return [t[:_MAX_CHUNK_SOFT]]

    chunks: list[str] = []
    start = 0
    n = len(t)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        window = t[start:end]
        if end < n:
            split_at = window.rfind(". ")
            if split_at >= max(0, CHUNK_SIZE // 4):
                end = start + split_at + 2
                window = t[start:end]
        if len(window) > _MAX_CHUNK_SOFT:
            window = window[:_MAX_CHUNK_SOFT]
        chunks.append(window.strip() or window)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [c for c in chunks if c]


def build_chunk_metadata(
    doc_id: str,
    doc_title: str,
    doc_path: str,
    source_type: str,
    chunk_index: int,
    total_chunks: int,
    department: str = "general",
    access_control: dict | None = None,
    extra: dict | None = None,
    *,
    source_id: str = "",
) -> dict[str, Any]:
    """
    Standard metadata for vector DB payload including access-control tags.
    ``ac_*`` fields are always present (lists may be empty).
    """
    ac = access_control or {}
    now = datetime.now(timezone.utc).isoformat()

    ac_channels: list[str] = list(ac.get("allowed_channels") or []) if source_type == "slack" else []
    ac_page_ids: list[str] = []
    if source_type == "notion":
        if extra and extra.get("page_id"):
            ac_page_ids = [str(extra["page_id"])]
        else:
            ac_page_ids = list(ac.get("allowed_page_ids") or [])

    depts = ac.get("departments")
    if isinstance(depts, list) and depts:
        ac_departments = [str(x) for x in depts]
    else:
        ac_departments = [department] if department else ["general"]

    meta: dict[str, Any] = {
        "doc_id": doc_id,
        "doc_title": doc_title,
        "doc_path": doc_path,
        "source": source_type,
        "chunk_index": int(chunk_index),
        "total_chunks": int(total_chunks),
        "department": department,
        "indexed_at": now,
        "ac_source_id": source_id or "",
        "ac_channels": ac_channels,
        "ac_page_ids": ac_page_ids,
        "ac_departments": ac_departments,
    }
    if extra:
        meta.update(extra)
    return meta


def make_point_id(doc_id: str, chunk_index: int) -> str:
    """
    Deterministic UUID point ID from doc_id + chunk_index.
    Uses first 16 bytes of SHA256 hash formatted as UUID.
    Qdrant requires either UUID or unsigned integer as point ID.
    """
    import uuid
    hash_bytes = hashlib.sha256(
        f"{doc_id}::{chunk_index}".encode()
    ).digest()
    return str(uuid.UUID(bytes=hash_bytes[:16]))
