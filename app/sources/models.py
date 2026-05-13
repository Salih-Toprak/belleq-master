"""Data source domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DataSourceRecord:
    """External knowledge source (Slack, Notion) configuration."""

    source_id: str
    source_type: str  # slack | notion
    display_name: str
    enabled: bool = True
    added_at: datetime | None = None
    updated_at: datetime | None = None
    credentials: dict = field(default_factory=dict)
    access_control: dict = field(default_factory=dict)
    last_synced_at: datetime | None = None
    sync_status: str = "never"
    total_chunks_indexed: int = 0
    last_error: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SyncLogEntry:
    """One ingestion run for a source."""

    log_id: int | None = None
    source_id: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = ""
    chunks_upserted: int = 0
    chunks_deleted: int = 0
    docs_processed: int = 0
    error_message: str = ""
    metadata: dict = field(default_factory=dict)
