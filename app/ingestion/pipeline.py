"""Orchestrates fetch → chunk → embed → vector upsert."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.ingestion.chunker import build_chunk_metadata, chunk_text, make_point_id
from app.sources.models import DataSourceRecord, SyncLogEntry
from app.sources.notion_source import NotionSource
from app.sources.registry import DataSourceRegistry
from app.sources.slack_source import SlackSource
from app.vectordb.base import VectorDBAdapter

if TYPE_CHECKING:
    from app.embeddings.base import EmbeddingAdapter

logger = logging.getLogger(__name__)


def _notion_doc_id(page_id: str) -> str:
    return hashlib.sha256(f"notion::{page_id}".encode("utf-8")).hexdigest()[:16]


def _slack_doc_id(channel_id: str, window_start_ts: str) -> str:
    return hashlib.sha256(f"slack::{channel_id}::{window_start_ts}".encode("utf-8")).hexdigest()[:16]


class IngestionPipeline:
    """Runs ingestion for configured data sources."""

    def __init__(
        self,
        vectordb: VectorDBAdapter | None,
        embedder: "EmbeddingAdapter | None",
        source_registry: DataSourceRegistry,
        collection_name: str,
    ) -> None:
        self._vectordb = vectordb
        self._embedder = embedder
        self._registry = source_registry
        self._collection = collection_name
        logger.info("ingestion_pipeline_init collection=%s", collection_name)

    async def ingest_source(self, source_id: str, full_resync: bool = False) -> SyncLogEntry:
        rec = self._registry.get(source_id)
        if rec is None or not rec.enabled:
            raise ValueError(f"source not found or disabled: {source_id}")
        if self._vectordb is None:
            raise RuntimeError("Vector DB unavailable")
        if self._embedder is None:
            raise RuntimeError("Embedding adapter unavailable")

        now = datetime.now(timezone.utc)
        self._registry.update_sync_state(source_id, "running", None, rec.total_chunks_indexed, "")
        log_entry = SyncLogEntry(
            source_id=source_id,
            started_at=now,
            status="running",
        )
        try:
            if rec.source_type == "notion":
                await self._ingest_notion(rec, log_entry, full_resync)
            elif rec.source_type == "slack":
                await self._ingest_slack(rec, log_entry, full_resync)
            else:
                raise ValueError(f"unsupported source_type: {rec.source_type}")

            log_entry.status = "success"
            log_entry.finished_at = datetime.now(timezone.utc)
            self._registry.update_sync_state(
                source_id,
                "success",
                log_entry.finished_at,
                rec.total_chunks_indexed + log_entry.chunks_upserted,
                "",
            )
            self._registry.log_sync(log_entry)
            return log_entry
        except Exception as e:  # noqa: BLE001
            err = str(e)
            logger.error("ingest_source_failed source_id=%s error=%s", source_id, err, exc_info=True)
            log_entry.status = "error"
            log_entry.error_message = err
            log_entry.finished_at = datetime.now(timezone.utc)
            self._registry.update_sync_state(
                source_id,
                "error",
                rec.last_synced_at,
                rec.total_chunks_indexed,
                err,
            )
            self._registry.log_sync(log_entry)
            raise

    async def _ingest_notion(
        self,
        record: DataSourceRecord,
        log_entry: SyncLogEntry,
        full_resync: bool,
    ) -> None:
        src = NotionSource(record)
        try:
            pages = await src.fetch_pages()
            log_entry.docs_processed = len(pages)
            for page in pages:
                page_id = page["page_id"]
                doc_id = _notion_doc_id(page_id)
                last_edited = page.get("last_edited_time") or ""

                if not full_resync and record.last_synced_at:
                    existing = await self._vectordb.get_by_doc_id(self._collection, doc_id)
                    if existing:
                        prev = (existing[0].get("payload") or {}).get("notion_last_edited_time")
                        if prev == last_edited:
                            continue

                deleted = await self._vectordb.delete_by_doc_id(self._collection, doc_id)
                log_entry.chunks_deleted += int(deleted)

                chunks = chunk_text(page.get("text") or "")
                if not chunks or (len(chunks) == 1 and not chunks[0].strip()):
                    continue
                total = len(chunks)
                metas: list[dict[str, Any]] = []
                for i, ch in enumerate(chunks):
                    meta = build_chunk_metadata(
                        doc_id=doc_id,
                        doc_title=page.get("title") or "",
                        doc_path=page.get("url") or page_id,
                        source_type="notion",
                        chunk_index=i,
                        total_chunks=total,
                        department="general",
                        access_control=record.access_control,
                        extra={
                            "notion_last_edited_time": last_edited,
                            "page_id": page_id,
                        },
                        source_id=record.source_id,
                    )
                    metas.append(meta)
                vectors = await self._embedder.embed_batch(chunks)
                points = []
                for i, vec in enumerate(vectors):
                    pid = make_point_id(doc_id, i)
                    points.append({"id": pid, "vector": vec, "payload": metas[i]})
                n = await self._vectordb.upsert(self._collection, points)
                log_entry.chunks_upserted += int(n)
        finally:
            await src.close()

    async def _ingest_slack(
        self,
        record: DataSourceRecord,
        log_entry: SyncLogEntry,
        full_resync: bool,
    ) -> None:
        src = SlackSource(record)
        try:
            if full_resync:
                flt = {"must": [{"field": "ac_source_id", "value": record.source_id}]}
                deleted = await self._vectordb.delete_by_normalized_filter(self._collection, flt)
                log_entry.chunks_deleted += int(deleted)

            since = None if full_resync else record.last_synced_at
            if since is not None and since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            messages = await src.fetch_messages(since=since)

            from collections import defaultdict

            by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for m in messages:
                by_channel[m["channel_id"]].append(m)

            for channel_id, msgs in by_channel.items():
                msgs.sort(key=lambda x: float(x["ts"]))
                windows: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for m in msgs:
                    tsf = float(m["ts"])
                    dt = datetime.fromtimestamp(tsf, tz=timezone.utc)
                    win_start = dt.replace(minute=0, second=0, microsecond=0)
                    wkey = str(int(win_start.timestamp()))
                    windows[wkey].append(m)

                for wkey, wmsgs in windows.items():
                    doc_id = _slack_doc_id(channel_id, wkey)
                    ch_name = wmsgs[0]["channel_name"]
                    body = "\n\n".join(x["text"] for x in wmsgs if x.get("text"))
                    chunks = chunk_text(body)
                    if not chunks or not any(c.strip() for c in chunks):
                        continue
                    total = len(chunks)
                    deleted = await self._vectordb.delete_by_doc_id(self._collection, doc_id)
                    log_entry.chunks_deleted += int(deleted)
                    metas = []
                    for i, ch in enumerate(chunks):
                        meta = build_chunk_metadata(
                            doc_id=doc_id,
                            doc_title=f"{ch_name} {wkey}",
                            doc_path=f"slack:{channel_id}:{wkey}",
                            source_type="slack",
                            chunk_index=i,
                            total_chunks=total,
                            department="general",
                            access_control=record.access_control,
                            extra={
                                "channel": ch_name,
                                "window_start": wkey,
                            },
                            source_id=record.source_id,
                        )
                        metas.append(meta)
                    vectors = await self._embedder.embed_batch(chunks)
                    points = []
                    for i, vec in enumerate(vectors):
                        pid = make_point_id(doc_id, i)
                        points.append({"id": pid, "vector": vec, "payload": metas[i]})
                    n = await self._vectordb.upsert(self._collection, points)
                    log_entry.chunks_upserted += int(n)
                log_entry.docs_processed += len(windows)
        finally:
            await src.close()

    async def ingest_all(self, full_resync: bool = False) -> list[SyncLogEntry]:
        results: list[SyncLogEntry] = []
        for rec in self._registry.list_all(enabled_only=True):
            try:
                results.append(await self.ingest_source(rec.source_id, full_resync=full_resync))
            except Exception as e:  # noqa: BLE001
                logger.warning("ingest_all_skip_source source_id=%s error=%s", rec.source_id, e)
                results.append(
                    SyncLogEntry(
                        source_id=rec.source_id,
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc),
                        status="error",
                        error_message=str(e),
                    )
                )
        return results
