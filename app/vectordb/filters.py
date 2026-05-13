"""Normalized metadata filters shared across vector DB adapters."""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client.http import models as qmodels

logger = logging.getLogger(__name__)


def build_qdrant_filter(filters: dict | None) -> qmodels.Filter | None:
    """Translate normalized filter dict to qdrant_client Filter object."""
    if not filters:
        return None
    must_list = filters.get("must") or []
    if not must_list:
        return None
    conditions: list[qmodels.FieldCondition] = []
    for cond in must_list:
        if not isinstance(cond, dict):
            continue
        field = cond.get("field")
        value = cond.get("value")
        if field is None:
            continue
        key = str(field)
        conditions.append(
            qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))
        )
    if not conditions:
        return None
    return qmodels.Filter(must=conditions)


def build_pinecone_filter(filters: dict | None) -> dict | None:
    """Translate normalized filter dict to Pinecone metadata filter dict."""
    if not filters:
        return None
    must_list = filters.get("must") or []
    if not must_list:
        return None
    parts: list[dict[str, Any]] = []
    for cond in must_list:
        if not isinstance(cond, dict):
            continue
        field = cond.get("field")
        value = cond.get("value")
        if field is None:
            continue
        parts.append({str(field): {"$eq": value}})
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}
