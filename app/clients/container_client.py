"""HTTP client for calling registered user containers (no shared code with rag-wiki)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.registry.models import ContainerRecord

logger = logging.getLogger(__name__)


def _unreachable(record: ContainerRecord, error: str) -> dict:
    """Standard shape for unreachable container response."""
    return {
        "container_id": record.container_id,
        "reachable": False,
        "error": error,
    }


class ContainerClient:
    """Async httpx wrapper with short health timeouts and graceful failures."""

    def __init__(self, timeout: float = 10.0, health_timeout: float = 3.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout)
        self._health_timeout = health_timeout
        logger.debug("container_client_init timeout=%s health_timeout=%s", timeout, health_timeout)

    def _headers(self, record: ContainerRecord) -> dict[str, str]:
        if record.api_key:
            return {"X-Container-Key": record.api_key}
        return {}

    def _internal_headers(self) -> dict[str, str]:
        """Auth for the container's ``/internal/*`` endpoints.

        Those require ``X-Master-Key`` == the container's ``MASTER_API_KEY`` env,
        which the provisioner sets to this master's ``admin_api_key``.
        """
        from app.config import settings

        key = (settings.admin_api_key or "").strip()
        return {"X-Master-Key": key} if key else {}

    async def health_check(self, record: ContainerRecord) -> tuple[bool, int | None, str]:
        """
        Returns (reachable, response_ms, error_message).
        Timeout: health_timeout (shorter than regular calls).
        """
        start = time.monotonic()
        try:
            r = await self._client.get(
                f"{record.base_url.rstrip('/')}/health",
                headers=self._headers(record),
                timeout=self._health_timeout,
            )
            ms = int((time.monotonic() - start) * 1000)
            return r.status_code == 200, ms, ""
        except Exception as e:  # noqa: BLE001
            ms = int((time.monotonic() - start) * 1000)
            return False, ms, str(e)

    async def get_stats(self, record: ContainerRecord) -> dict:
        """GET {base_url}/stats"""
        try:
            r = await self._client.get(
                f"{record.base_url.rstrip('/')}/stats",
                headers=self._headers(record),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def get_docs(
        self,
        record: ContainerRecord,
        status: str | None = None,
        department: str | None = None,
        limit: int = 100,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if department:
            params["department"] = department
        try:
            r = await self._client.get(
                f"{record.base_url.rstrip('/')}/docs",
                headers=self._headers(record),
                params=params,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def get_doc(self, record: ContainerRecord, doc_id: str) -> dict:
        try:
            r = await self._client.get(
                f"{record.base_url.rstrip('/')}/docs/{doc_id}",
                headers=self._headers(record),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def flag_doc(self, record: ContainerRecord, doc_id: str, reason: str) -> dict:
        try:
            r = await self._client.post(
                f"{record.base_url.rstrip('/')}/docs/{doc_id}/flag",
                headers=self._headers(record),
                json={"reason": reason},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def unflag_doc(self, record: ContainerRecord, doc_id: str) -> dict:
        try:
            r = await self._client.post(
                f"{record.base_url.rstrip('/')}/docs/{doc_id}/unflag",
                headers=self._headers(record),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def delete_doc(self, record: ContainerRecord, doc_id: str) -> dict:
        try:
            r = await self._client.delete(
                f"{record.base_url.rstrip('/')}/docs/{doc_id}",
                headers=self._headers(record),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def get_conversation_stats(self, record: ContainerRecord) -> dict:
        """GET {base_url}/internal/conversations/stats (conversation capture)."""
        try:
            r = await self._client.get(
                f"{record.base_url.rstrip('/')}/internal/conversations/stats",
                headers=self._internal_headers(),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def flush_conversations(self, record: ContainerRecord) -> dict:
        """POST {base_url}/internal/conversations/flush — force-ingest now."""
        try:
            r = await self._client.post(
                f"{record.base_url.rstrip('/')}/internal/conversations/flush",
                headers=self._internal_headers(),
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return _unreachable(record, str(e))

    async def health_check_all(
        self,
        records: list[ContainerRecord],
    ) -> list[tuple[ContainerRecord, bool, int | None, str]]:
        """
        Health check all containers concurrently.
        Returns list of (record, reachable, response_ms, error).
        Uses asyncio.gather with return_exceptions=True.
        """
        async def one(rec: ContainerRecord) -> tuple[ContainerRecord, bool, int | None, str]:
            try:
                ok, ms, err = await self.health_check(rec)
                return rec, ok, ms, err
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "health_check_unexpected_error container_id=%s error=%s",
                    rec.container_id,
                    e,
                )
                return rec, False, None, str(e)

        tasks = [one(r) for r in records]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[tuple[ContainerRecord, bool, int | None, str]] = []
        for rec, item in zip(records, results, strict=True):
            if isinstance(item, BaseException):
                logger.warning(
                    "health_check_gather_exception container_id=%s error=%s",
                    rec.container_id,
                    item,
                )
                out.append((rec, False, None, str(item)))
            else:
                out.append(item)
        return out

    async def get_stats_all(self, records: list[ContainerRecord]) -> list[dict]:
        """
        Fetch stats from all containers concurrently.
        Uses asyncio.gather with return_exceptions=True.
        Exceptions mapped to unreachable dicts.
        """

        async def one(rec: ContainerRecord) -> dict:
            try:
                return await self.get_stats(rec)
            except Exception as e:  # noqa: BLE001
                return _unreachable(rec, str(e))

        tasks = [one(r) for r in records]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        mapped: list[dict] = []
        for rec, res in zip(records, results, strict=True):
            if isinstance(res, BaseException):
                mapped.append(_unreachable(rec, str(res)))
            else:
                mapped.append(res)
        return mapped

    async def close(self) -> None:
        await self._client.aclose()
        logger.debug("container_client_closed")
