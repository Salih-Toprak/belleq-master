"""Notion API reader and chunker support."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.sources.models import DataSourceRecord

logger = logging.getLogger(__name__)

_NOTION_VERSION = "2022-06-28"


class NotionSource:
    """Fetch pages and text blocks from Notion."""

    def __init__(self, record: DataSourceRecord) -> None:
        self.record = record
        creds = record.credentials or {}
        self.token = str(creds.get("integration_token", ""))
        self.access_control = dict(record.access_control or {})
        self.allowed_page_ids = list(self.access_control.get("allowed_page_ids") or [])
        self.allowed_database_ids = list(self.access_control.get("allowed_database_ids") or [])
        self.excluded_page_ids = set(self.access_control.get("excluded_page_ids") or [])
        self.max_depth = int(self.access_control.get("max_depth", 2))
        self._client = httpx.AsyncClient(
            base_url="https://api.notion.com/v1",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def validate_credentials(self) -> dict[str, Any]:
        try:
            r = await self._client.get("/users/me")
            if r.status_code == 200:
                return {"valid": True, "error": ""}
            return {"valid": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:  # noqa: BLE001
            return {"valid": False, "error": str(e)}

    async def _get_page_title(self, page: dict[str, Any]) -> str:
        props = page.get("properties") or {}
        for key in ("title", "Name"):
            if key in props:
                t = props[key]
                if isinstance(t, dict) and t.get("type") == "title":
                    arr = t.get("title") or []
                    if arr and isinstance(arr[0], dict):
                        return str(arr[0].get("plain_text", "") or page.get("id", "untitled"))
        for _k, pv in props.items():
            if isinstance(pv, dict) and pv.get("type") == "title":
                arr = pv.get("title") or []
                if arr and isinstance(arr[0], dict):
                    return str(arr[0].get("plain_text", "") or "untitled")
        return str(page.get("id", "untitled"))

    async def _extract_block_text(self, block_id: str, depth: int = 0) -> str:
        if depth > self.max_depth:
            return ""
        parts: list[str] = []
        cursor: str | None = None
        text_types = frozenset(
            {
                "paragraph",
                "heading_1",
                "heading_2",
                "heading_3",
                "bulleted_list_item",
                "numbered_list_item",
                "toggle",
                "quote",
                "callout",
            }
        )
        skip_log = frozenset({"image", "video", "embed", "bookmark", "divider"})
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = await self._client.get(f"/blocks/{block_id}/children", params=params)
            r.raise_for_status()
            data = r.json()
            for block in data.get("results") or []:
                btype = block.get("type") or ""
                if btype in skip_log:
                    logger.debug("notion_block_skipped type=%s id=%s", btype, block.get("id"))
                    continue
                rich = []
                if btype in text_types:
                    obj = block.get(btype) or {}
                    rich = obj.get("rich_text") or []
                line = "".join(str(x.get("plain_text", "")) for x in rich if isinstance(x, dict))
                if line.strip():
                    parts.append(line)
                bid = block.get("id")
                if bid and block.get("has_children") and depth < self.max_depth:
                    child_text = await self._extract_block_text(bid, depth + 1)
                    if child_text.strip():
                        parts.append(child_text)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return "\n".join(parts)

    async def fetch_pages(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pid in self.allowed_page_ids:
            if pid in self.excluded_page_ids:
                continue
            try:
                pr = await self._client.get(f"/pages/{pid}")
                pr.raise_for_status()
                page = pr.json()
                title = await self._get_page_title(page)
                last_edited = page.get("last_edited_time") or ""
                text = await self._extract_block_text(pid, 0)
                url = page.get("url") or ""
                out.append(
                    {
                        "page_id": pid,
                        "title": title,
                        "url": url,
                        "last_edited_time": last_edited,
                        "text": text,
                        "depth": 0,
                    }
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("notion_page_fetch_failed page_id=%s error=%s", pid, e)

        for dbid in self.allowed_database_ids:
            try:
                cursor = None
                while True:
                    body: dict[str, Any] = {"page_size": 100}
                    if cursor:
                        body["start_cursor"] = cursor
                    dr = await self._client.post(f"/databases/{dbid}/query", json=body)
                    dr.raise_for_status()
                    ddata = dr.json()
                    for row in ddata.get("results") or []:
                        rid = row.get("id")
                        if not rid or rid in self.excluded_page_ids:
                            continue
                        title = await self._get_page_title(row)
                        last_edited = row.get("last_edited_time") or ""
                        text = await self._extract_block_text(rid, 0)
                        url = row.get("url") or ""
                        out.append(
                            {
                                "page_id": rid,
                                "title": title,
                                "url": url,
                                "last_edited_time": last_edited,
                                "text": text,
                                "depth": 0,
                            }
                        )
                    if not ddata.get("has_more"):
                        break
                    cursor = ddata.get("next_cursor")
                    if not cursor:
                        break
            except Exception as e:  # noqa: BLE001
                logger.warning("notion_database_query_failed db_id=%s error=%s", dbid, e)

        return out

    async def close(self) -> None:
        await self._client.aclose()
