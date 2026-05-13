"""SQLite-backed data source registry (delegates to MasterDB)."""

from __future__ import annotations

import logging
from typing import Any

from app.database import MasterDB
from app.sources.models import DataSourceRecord, SyncLogEntry

logger = logging.getLogger(__name__)


class DataSourceRegistry:
    """Thin wrapper over MasterDB for configured knowledge sources."""

    def __init__(self, db: MasterDB) -> None:
        self._db = db
        logger.debug("data_source_registry_initialized")

    def list_all(self, enabled_only: bool = False, source_type: str | None = None) -> list[DataSourceRecord]:
        return self._db.list_sources(enabled_only=enabled_only, source_type=source_type)

    def get(self, source_id: str) -> DataSourceRecord | None:
        return self._db.get_source(source_id)

    def add(self, record: DataSourceRecord) -> None:
        self._db.add_source(record)

    def update(self, source_id: str, updates: dict[str, Any]) -> DataSourceRecord:
        return self._db.update_source(source_id, updates)

    def remove(self, source_id: str) -> None:
        self._db.remove_source(source_id)

    def enable(self, source_id: str) -> None:
        self.update(source_id, {"enabled": True})

    def disable(self, source_id: str) -> None:
        self.update(source_id, {"enabled": False})

    def update_sync_state(
        self,
        source_id: str,
        status: str,
        last_synced_at: Any,
        total_chunks_indexed: int,
        last_error: str = "",
    ) -> None:
        self._db.update_source_sync_state(
            source_id,
            status,
            last_synced_at,
            total_chunks_indexed,
            last_error,
        )

    def log_sync(self, entry: SyncLogEntry) -> int:
        return self._db.log_sync_entry(entry)

    def get_sync_history(self, source_id: str, limit: int = 20) -> list[SyncLogEntry]:
        return self._db.get_sync_log(source_id, limit=limit)
