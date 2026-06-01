"""Master SQLite database: container registry and health check log (SQLAlchemy Core)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, make_url

from app.registry.models import ContainerRecord
from app.credential_crypto import pack_credentials, unpack_credentials
from app.sources.models import DataSourceRecord, SyncLogEntry
from app.mcp_connectors.models import ContainerConnectorLink, MCPConnectorRecord

logger = logging.getLogger(__name__)

metadata = MetaData()

container_registry = Table(
    "container_registry",
    metadata,
    Column("container_id", String, primary_key=True),
    Column("display_name", String, nullable=False, server_default=""),
    Column("container_type", String, nullable=False, server_default="user"),
    Column("base_url", String, nullable=False),
    Column("api_key", String, nullable=False, server_default=""),
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("added_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("metadata_json", Text, nullable=False, server_default="{}"),
)

health_check_log = Table(
    "health_check_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("container_id", String, nullable=False),
    Column("checked_at", DateTime, nullable=False),
    Column("reachable", Boolean, nullable=False),
    Column("response_ms", Integer, nullable=True),
    Column("error_message", Text, nullable=False, server_default=""),
)

data_sources = Table(
    "data_sources",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("source_type", String, nullable=False),
    Column("display_name", String, nullable=False, server_default=""),
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("credentials_json", Text, nullable=False, server_default="{}"),
    Column("access_control_json", Text, nullable=False, server_default="{}"),
    Column("last_synced_at", DateTime, nullable=True),
    Column("sync_status", String, nullable=False, server_default="never"),
    Column("total_chunks_indexed", Integer, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=False, server_default=""),
    Column("added_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("metadata_json", Text, nullable=False, server_default="{}"),
)

sync_log = Table(
    "sync_log",
    metadata,
    Column("log_id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String, nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime, nullable=True),
    Column("status", String, nullable=False, server_default=""),
    Column("chunks_upserted", Integer, nullable=False, server_default="0"),
    Column("chunks_deleted", Integer, nullable=False, server_default="0"),
    Column("docs_processed", Integer, nullable=False, server_default="0"),
    Column("error_message", Text, nullable=False, server_default=""),
    Column("metadata_json", Text, nullable=False, server_default="{}"),
)

embedding_config = Table(
    "embedding_config",
    metadata,
    Column("config_id", String, primary_key=True),
    Column("backend", String, nullable=False, server_default="ollama"),
    Column("model_name", String, nullable=False, server_default="nomic-embed-text"),
    Column("vector_size", Integer, nullable=False, server_default="768"),
    Column("config_json", Text, nullable=False, server_default="{}"),
    Column("updated_at", DateTime, nullable=False),
)

ingestion_scheduler = Table(
    "ingestion_scheduler",
    metadata,
    Column("id", String, primary_key=True),
    Column("interval_minutes", Integer, nullable=False, server_default="60"),
    Column("updated_at", DateTime, nullable=False),
)

# Upstream MCP servers registered by the user (Notion MCP, GitHub MCP, ...).
mcp_connectors = Table(
    "mcp_connectors",
    metadata,
    Column("connector_id", String, primary_key=True),
    Column("display_name", String, nullable=False, server_default=""),
    Column("transport", String, nullable=False, server_default="streamable_http"),
    Column("url", String, nullable=False, server_default=""),
    Column("command", String, nullable=False, server_default=""),
    Column("args_json", Text, nullable=False, server_default="[]"),
    # headers + env (secret material) packed together, encrypted at rest.
    Column("secrets_json", Text, nullable=False, server_default="{}"),
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("last_status", String, nullable=False, server_default="unknown"),
    Column("last_error", Text, nullable=False, server_default=""),
    Column("last_checked_at", DateTime, nullable=True),
    Column("tool_count", Integer, nullable=False, server_default="0"),
    Column("auth_status", String, nullable=False, server_default="none"),
    Column("added_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("metadata_json", Text, nullable=False, server_default="{}"),
)

# In-flight OAuth authorization-code exchanges, keyed by the `state` param.
mcp_oauth_state = Table(
    "mcp_oauth_state",
    metadata,
    Column("state", String, primary_key=True),
    Column("connector_id", String, nullable=False),
    # code_verifier + client registration + endpoints (secret), encrypted.
    Column("payload_json", Text, nullable=False, server_default="{}"),
    Column("created_at", DateTime, nullable=False),
)

# Per-container connector whitelist (which connectors a container may see).
container_mcp_acl = Table(
    "container_mcp_acl",
    metadata,
    Column("container_id", String, primary_key=True),
    Column("connector_id", String, primary_key=True),
    Column("added_at", DateTime, nullable=False),
)


def _ensure_sqlite_parent_dir(url: str) -> None:
    """Create parent directory for SQLite file if needed."""
    try:
        u = make_url(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("could_not_parse_db_url url=%s error=%s", url, e)
        return
    if u.drivername != "sqlite":
        return
    if not u.database or u.database in (":memory:",):
        return
    # database may be relative path after sqlite:///
    db_path = u.database
    if db_path.startswith("/"):
        parent = os.path.dirname(db_path)
    else:
        parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
        logger.debug("sqlite_parent_ready path=%s", parent)


class MasterDB:
    """SQLite-backed master state using SQLAlchemy Core (no ORM)."""

    def __init__(
        self,
        url: str = "sqlite:///./data/master.db",
        credential_encryption_key: str = "",
    ) -> None:
        _ensure_sqlite_parent_dir(url)
        self._url = url
        self._credential_encryption_key = credential_encryption_key or ""
        self._engine: Engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        metadata.create_all(self._engine)
        self._run_lightweight_migrations()
        logger.info("master_db_initialized url=%s", url)

    def _run_lightweight_migrations(self) -> None:
        """Add columns introduced after a table was first created (SQLite).

        ``metadata.create_all`` never alters an existing table, so new columns
        on already-created tables must be added explicitly. Idempotent.
        """
        from sqlalchemy import text

        wanted = {
            "mcp_connectors": [("auth_status", "TEXT NOT NULL DEFAULT 'none'")],
        }
        with self._engine.begin() as conn:
            for table, cols in wanted.items():
                existing = {
                    row[1]
                    for row in conn.execute(text(f"PRAGMA table_info({table})"))
                }
                if not existing:
                    continue  # table not created yet (fresh DB handles via create_all)
                for col_name, col_ddl in cols:
                    if col_name not in existing:
                        conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")
                        )
                        logger.info("migrated_add_column table=%s col=%s", table, col_name)

    @property
    def engine(self) -> Engine:
        return self._engine

    def _row_to_record(self, row: Any) -> ContainerRecord:
        meta_raw = row.metadata_json or "{}"
        try:
            meta = json.loads(meta_raw)
            if not isinstance(meta, dict):
                meta = {}
        except json.JSONDecodeError:
            logger.warning(
                "metadata_json_invalid container_id=%s using_empty_dict",
                row.container_id,
            )
            meta = {}
        return ContainerRecord(
            container_id=row.container_id,
            display_name=row.display_name or "",
            container_type=row.container_type or "user",
            base_url=row.base_url,
            api_key=row.api_key or "",
            enabled=bool(row.enabled),
            added_at=row.added_at,
            updated_at=row.updated_at,
            metadata=meta,
        )

    def _record_to_dict(self, record: ContainerRecord) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        added = record.added_at or now
        updated = record.updated_at or now
        return {
            "container_id": record.container_id,
            "display_name": record.display_name,
            "container_type": record.container_type,
            "base_url": record.base_url,
            "api_key": record.api_key,
            "enabled": record.enabled,
            "added_at": added,
            "updated_at": updated,
            "metadata_json": json.dumps(record.metadata or {}),
        }

    def get_container(self, container_id: str) -> ContainerRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(container_registry).where(container_registry.c.container_id == container_id)
            ).mappings().first()
            if row is None:
                return None
            return self._row_to_record(row)

    def list_containers(self, enabled_only: bool = False) -> list[ContainerRecord]:
        stmt = select(container_registry).order_by(container_registry.c.added_at)
        if enabled_only:
            stmt = stmt.where(container_registry.c.enabled.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_record(r) for r in rows]

    def add_container(self, record: ContainerRecord) -> None:
        if self.get_container(record.container_id) is not None:
            raise ValueError(f"container_id already exists: {record.container_id}")
        data = self._record_to_dict(record)
        with self._engine.begin() as conn:
            conn.execute(insert(container_registry).values(**data))
        logger.info("container_added container_id=%s", record.container_id)

    def update_container(self, container_id: str, updates: dict[str, Any]) -> ContainerRecord:
        current = self.get_container(container_id)
        if current is None:
            raise KeyError(container_id)
        forbidden = {"container_id", "added_at"}
        for k in updates:
            if k in forbidden:
                raise ValueError(f"cannot update field: {k}")
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {"updated_at": now}
        if "metadata" in updates:
            payload["metadata_json"] = json.dumps(updates["metadata"] or {})
        for field in (
            "display_name",
            "container_type",
            "base_url",
            "api_key",
            "enabled",
        ):
            if field in updates:
                payload[field] = updates[field]
        with self._engine.begin() as conn:
            conn.execute(
                update(container_registry)
                .where(container_registry.c.container_id == container_id)
                .values(**payload)
            )
        updated = self.get_container(container_id)
        assert updated is not None
        logger.info("container_updated container_id=%s", container_id)
        return updated

    def remove_container(self, container_id: str) -> None:
        if self.get_container(container_id) is None:
            raise KeyError(container_id)
        with self._engine.begin() as conn:
            conn.execute(
                delete(container_registry).where(container_registry.c.container_id == container_id)
            )
        logger.info("container_removed container_id=%s", container_id)

    def enable_container(self, container_id: str) -> None:
        self.update_container(container_id, {"enabled": True})

    def disable_container(self, container_id: str) -> None:
        self.update_container(container_id, {"enabled": False})

    def log_health_check(
        self,
        container_id: str,
        reachable: bool,
        response_ms: int | None,
        error: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                insert(health_check_log).values(
                    container_id=container_id,
                    checked_at=now,
                    reachable=reachable,
                    response_ms=response_ms,
                    error_message=error or "",
                )
            )
        logger.debug(
            "health_check_logged container_id=%s reachable=%s response_ms=%s",
            container_id,
            reachable,
            response_ms,
        )

    def get_health_history(self, container_id: str, limit: int = 20) -> list[dict[str, Any]]:
        stmt = (
            select(health_check_log)
            .where(health_check_log.c.container_id == container_id)
            .order_by(health_check_log.c.checked_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        out: list[dict[str, Any]] = []
        for r in rows:
            checked = r["checked_at"]
            if hasattr(checked, "isoformat"):
                checked_s = checked.isoformat()
            else:
                checked_s = str(checked)
            out.append(
                {
                    "id": r["id"],
                    "container_id": r["container_id"],
                    "checked_at": checked_s,
                    "reachable": bool(r["reachable"]),
                    "response_ms": r["response_ms"],
                    "error_message": r["error_message"] or "",
                }
            )
        return out

    # --- Data sources -------------------------------------------------

    def _row_to_source(self, row: Any) -> DataSourceRecord:
        ac_raw = row.access_control_json or "{}"
        meta_raw = row.metadata_json or "{}"
        try:
            ac = json.loads(ac_raw)
            if not isinstance(ac, dict):
                ac = {}
        except json.JSONDecodeError:
            ac = {}
        try:
            meta = json.loads(meta_raw)
            if not isinstance(meta, dict):
                meta = {}
        except json.JSONDecodeError:
            meta = {}
        creds = unpack_credentials(row.credentials_json or "{}", self._credential_encryption_key)
        return DataSourceRecord(
            source_id=row.source_id,
            source_type=row.source_type or "slack",
            display_name=row.display_name or "",
            enabled=bool(row.enabled),
            added_at=row.added_at,
            updated_at=row.updated_at,
            credentials=creds,
            access_control=ac,
            last_synced_at=row.last_synced_at,
            sync_status=row.sync_status or "never",
            total_chunks_indexed=int(row.total_chunks_indexed or 0),
            last_error=row.last_error or "",
            metadata=meta,
        )

    def _source_to_dict(self, record: DataSourceRecord) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        added = record.added_at or now
        updated = record.updated_at or now
        creds_stored = pack_credentials(record.credentials or {}, self._credential_encryption_key)
        return {
            "source_id": record.source_id,
            "source_type": record.source_type,
            "display_name": record.display_name,
            "enabled": record.enabled,
            "credentials_json": creds_stored,
            "access_control_json": json.dumps(record.access_control or {}),
            "last_synced_at": record.last_synced_at,
            "sync_status": record.sync_status or "never",
            "total_chunks_indexed": int(record.total_chunks_indexed or 0),
            "last_error": record.last_error or "",
            "added_at": added,
            "updated_at": updated,
            "metadata_json": json.dumps(record.metadata or {}),
        }

    def get_source(self, source_id: str) -> DataSourceRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(data_sources).where(data_sources.c.source_id == source_id)
            ).mappings().first()
            if row is None:
                return None
            return self._row_to_source(row)

    def list_sources(
        self,
        enabled_only: bool = False,
        source_type: str | None = None,
    ) -> list[DataSourceRecord]:
        stmt = select(data_sources).order_by(data_sources.c.added_at)
        if enabled_only:
            stmt = stmt.where(data_sources.c.enabled.is_(True))
        if source_type:
            stmt = stmt.where(data_sources.c.source_type == source_type)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_source(r) for r in rows]

    def add_source(self, record: DataSourceRecord) -> None:
        if self.get_source(record.source_id) is not None:
            raise ValueError(f"source_id already exists: {record.source_id}")
        data = self._source_to_dict(record)
        with self._engine.begin() as conn:
            conn.execute(insert(data_sources).values(**data))
        logger.info("data_source_added source_id=%s", record.source_id)

    def update_source(self, source_id: str, updates: dict[str, Any]) -> DataSourceRecord:
        current = self.get_source(source_id)
        if current is None:
            raise KeyError(source_id)
        forbidden = {"source_id", "added_at", "source_type", "credentials"}
        for k in updates:
            if k in forbidden:
                raise ValueError(f"cannot update field via update_source: {k}")
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {"updated_at": now}
        if "display_name" in updates:
            payload["display_name"] = updates["display_name"]
        if "enabled" in updates:
            payload["enabled"] = updates["enabled"]
        if "access_control" in updates:
            payload["access_control_json"] = json.dumps(updates["access_control"] or {})
        if "metadata" in updates:
            payload["metadata_json"] = json.dumps(updates["metadata"] or {})
        if "sync_status" in updates:
            payload["sync_status"] = updates["sync_status"]
        if "last_synced_at" in updates:
            payload["last_synced_at"] = updates["last_synced_at"]
        if "total_chunks_indexed" in updates:
            payload["total_chunks_indexed"] = int(updates["total_chunks_indexed"])
        if "last_error" in updates:
            payload["last_error"] = updates["last_error"]
        with self._engine.begin() as conn:
            conn.execute(
                update(data_sources).where(data_sources.c.source_id == source_id).values(**payload)
            )
        updated = self.get_source(source_id)
        assert updated is not None
        logger.info("data_source_updated source_id=%s", source_id)
        return updated

    def remove_source(self, source_id: str) -> None:
        if self.get_source(source_id) is None:
            raise KeyError(source_id)
        with self._engine.begin() as conn:
            conn.execute(delete(sync_log).where(sync_log.c.source_id == source_id))
            conn.execute(delete(data_sources).where(data_sources.c.source_id == source_id))
        logger.info("data_source_removed source_id=%s", source_id)

    def update_source_sync_state(
        self,
        source_id: str,
        status: str,
        last_synced_at: datetime | None,
        total_chunks_indexed: int,
        last_error: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                update(data_sources)
                .where(data_sources.c.source_id == source_id)
                .values(
                    updated_at=now,
                    sync_status=status,
                    last_synced_at=last_synced_at,
                    total_chunks_indexed=int(total_chunks_indexed),
                    last_error=last_error or "",
                )
            )

    def log_sync_entry(self, entry: SyncLogEntry) -> int:
        now = datetime.now(timezone.utc)
        started = entry.started_at or now
        finished = entry.finished_at
        with self._engine.begin() as conn:
            res = conn.execute(
                insert(sync_log).values(
                    source_id=entry.source_id,
                    started_at=started,
                    finished_at=finished,
                    status=entry.status or "",
                    chunks_upserted=int(entry.chunks_upserted or 0),
                    chunks_deleted=int(entry.chunks_deleted or 0),
                    docs_processed=int(entry.docs_processed or 0),
                    error_message=entry.error_message or "",
                    metadata_json=json.dumps(entry.metadata or {}),
                )
            )
            lid = int(res.lastrowid) if res.lastrowid is not None else 0
        logger.debug("sync_log_written source_id=%s log_id=%s", entry.source_id, lid)
        return int(lid)

    def get_sync_log(self, source_id: str, limit: int = 20) -> list[SyncLogEntry]:
        stmt = (
            select(sync_log)
            .where(sync_log.c.source_id == source_id)
            .order_by(sync_log.c.started_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        out: list[SyncLogEntry] = []
        for r in rows:
            meta_raw = r["metadata_json"] or "{}"
            try:
                meta = json.loads(meta_raw)
                if not isinstance(meta, dict):
                    meta = {}
            except json.JSONDecodeError:
                meta = {}
            out.append(
                SyncLogEntry(
                    log_id=int(r["log_id"]),
                    source_id=r["source_id"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    status=r["status"] or "",
                    chunks_upserted=int(r["chunks_upserted"] or 0),
                    chunks_deleted=int(r["chunks_deleted"] or 0),
                    docs_processed=int(r["docs_processed"] or 0),
                    error_message=r["error_message"] or "",
                    metadata=meta,
                )
            )
        return out

    def get_embedding_config(self) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(embedding_config).where(embedding_config.c.config_id == "default")
            ).mappings().first()
        if row is None:
            return None
        try:
            cfg = json.loads(row["config_json"] or "{}")
            if not isinstance(cfg, dict):
                cfg = {}
        except json.JSONDecodeError:
            cfg = {}
        return {
            "config_id": row["config_id"],
            "backend": row["backend"],
            "model_name": row["model_name"],
            "vector_size": int(row["vector_size"] or 0),
            "config": cfg,
            "updated_at": row["updated_at"],
        }

    def set_embedding_config(
        self,
        backend: str,
        model_name: str,
        vector_size: int,
        config: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc)
        payload = {
            "config_id": "default",
            "backend": backend,
            "model_name": model_name,
            "vector_size": int(vector_size),
            "config_json": json.dumps(config or {}),
            "updated_at": now,
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(embedding_config.c.config_id).where(embedding_config.c.config_id == "default")
            ).first()
            if existing:
                conn.execute(
                    update(embedding_config)
                    .where(embedding_config.c.config_id == "default")
                    .values(**{k: v for k, v in payload.items() if k != "config_id"})
                )
            else:
                conn.execute(insert(embedding_config).values(**payload))
        logger.info("embedding_config_saved backend=%s model=%s", backend, model_name)

    def get_scheduler_interval(self) -> int | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(ingestion_scheduler).where(ingestion_scheduler.c.id == "default")
            ).mappings().first()
        if row is None:
            return None
        return int(row["interval_minutes"] or 0)

    def set_scheduler_interval(self, interval_minutes: int) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(ingestion_scheduler.c.id).where(ingestion_scheduler.c.id == "default")
            ).first()
            if existing:
                conn.execute(
                    update(ingestion_scheduler)
                    .where(ingestion_scheduler.c.id == "default")
                    .values(interval_minutes=int(interval_minutes), updated_at=now)
                )
            else:
                conn.execute(
                    insert(ingestion_scheduler).values(
                        id="default",
                        interval_minutes=int(interval_minutes),
                        updated_at=now,
                    )
                )
        logger.info("scheduler_interval_saved minutes=%s", interval_minutes)

    # --- MCP connectors -----------------------------------------------

    def _row_to_connector(self, row: Any) -> MCPConnectorRecord:
        try:
            args = json.loads(row.args_json or "[]")
            if not isinstance(args, list):
                args = []
        except json.JSONDecodeError:
            args = []
        try:
            meta = json.loads(row.metadata_json or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except json.JSONDecodeError:
            meta = {}
        secrets = unpack_credentials(row.secrets_json or "{}", self._credential_encryption_key)
        headers = secrets.get("headers") if isinstance(secrets.get("headers"), dict) else {}
        env = secrets.get("env") if isinstance(secrets.get("env"), dict) else {}
        oauth = secrets.get("oauth") if isinstance(secrets.get("oauth"), dict) else {}
        return MCPConnectorRecord(
            connector_id=row.connector_id,
            display_name=row.display_name or "",
            transport=row.transport or "streamable_http",
            url=row.url or "",
            command=row.command or "",
            args=args,
            headers=headers,
            env=env,
            enabled=bool(row.enabled),
            added_at=row.added_at,
            updated_at=row.updated_at,
            last_status=row.last_status or "unknown",
            last_error=row.last_error or "",
            last_checked_at=row.last_checked_at,
            tool_count=int(row.tool_count or 0),
            auth_status=(getattr(row, "auth_status", None) or "none"),
            oauth=oauth,
            metadata=meta,
        )

    def _connector_to_dict(self, record: MCPConnectorRecord) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        added = record.added_at or now
        updated = record.updated_at or now
        secrets_stored = pack_credentials(
            {
                "headers": record.headers or {},
                "env": record.env or {},
                "oauth": record.oauth or {},
            },
            self._credential_encryption_key,
        )
        return {
            "connector_id": record.connector_id,
            "display_name": record.display_name,
            "transport": record.transport,
            "url": record.url or "",
            "command": record.command or "",
            "args_json": json.dumps(record.args or []),
            "secrets_json": secrets_stored,
            "enabled": record.enabled,
            "last_status": record.last_status or "unknown",
            "last_error": record.last_error or "",
            "last_checked_at": record.last_checked_at,
            "tool_count": int(record.tool_count or 0),
            "auth_status": record.auth_status or "none",
            "added_at": added,
            "updated_at": updated,
            "metadata_json": json.dumps(record.metadata or {}),
        }

    def get_connector(self, connector_id: str) -> MCPConnectorRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(mcp_connectors).where(mcp_connectors.c.connector_id == connector_id)
            ).mappings().first()
            if row is None:
                return None
            return self._row_to_connector(row)

    def list_connectors(self, enabled_only: bool = False) -> list[MCPConnectorRecord]:
        stmt = select(mcp_connectors).order_by(mcp_connectors.c.added_at)
        if enabled_only:
            stmt = stmt.where(mcp_connectors.c.enabled.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
            return [self._row_to_connector(r) for r in rows]

    def add_connector(self, record: MCPConnectorRecord) -> None:
        if self.get_connector(record.connector_id) is not None:
            raise ValueError(f"connector_id already exists: {record.connector_id}")
        data = self._connector_to_dict(record)
        with self._engine.begin() as conn:
            conn.execute(insert(mcp_connectors).values(**data))
        logger.info("mcp_connector_added connector_id=%s", record.connector_id)

    def update_connector(self, connector_id: str, updates: dict[str, Any]) -> MCPConnectorRecord:
        current = self.get_connector(connector_id)
        if current is None:
            raise KeyError(connector_id)
        forbidden = {"connector_id", "added_at"}
        for k in updates:
            if k in forbidden:
                raise ValueError(f"cannot update field: {k}")
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {"updated_at": now}
        # Secrets travel together; re-pack from the merged view.
        if "headers" in updates or "env" in updates or "oauth" in updates:
            headers = updates.get("headers", current.headers) or {}
            env = updates.get("env", current.env) or {}
            oauth = updates.get("oauth", current.oauth) or {}
            payload["secrets_json"] = pack_credentials(
                {"headers": headers, "env": env, "oauth": oauth},
                self._credential_encryption_key,
            )
        if "args" in updates:
            payload["args_json"] = json.dumps(updates["args"] or [])
        if "metadata" in updates:
            payload["metadata_json"] = json.dumps(updates["metadata"] or {})
        for fld in (
            "display_name",
            "transport",
            "url",
            "command",
            "enabled",
            "last_status",
            "last_error",
            "last_checked_at",
            "tool_count",
            "auth_status",
        ):
            if fld in updates:
                payload[fld] = updates[fld]
        with self._engine.begin() as conn:
            conn.execute(
                update(mcp_connectors)
                .where(mcp_connectors.c.connector_id == connector_id)
                .values(**payload)
            )
        updated = self.get_connector(connector_id)
        assert updated is not None
        logger.info("mcp_connector_updated connector_id=%s", connector_id)
        return updated

    def remove_connector(self, connector_id: str) -> None:
        if self.get_connector(connector_id) is None:
            raise KeyError(connector_id)
        with self._engine.begin() as conn:
            conn.execute(
                delete(container_mcp_acl).where(container_mcp_acl.c.connector_id == connector_id)
            )
            conn.execute(
                delete(mcp_connectors).where(mcp_connectors.c.connector_id == connector_id)
            )
        logger.info("mcp_connector_removed connector_id=%s", connector_id)

    # --- OAuth in-flight state ----------------------------------------

    def save_oauth_state(self, state: str, connector_id: str, payload: dict[str, Any]) -> None:
        """Persist a pending OAuth exchange (code_verifier, client info, endpoints)."""
        now = datetime.now(timezone.utc)
        stored = pack_credentials(payload or {}, self._credential_encryption_key)
        with self._engine.begin() as conn:
            conn.execute(delete(mcp_oauth_state).where(mcp_oauth_state.c.state == state))
            conn.execute(
                insert(mcp_oauth_state).values(
                    state=state,
                    connector_id=connector_id,
                    payload_json=stored,
                    created_at=now,
                )
            )

    def get_oauth_state(self, state: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(mcp_oauth_state).where(mcp_oauth_state.c.state == state)
            ).mappings().first()
        if row is None:
            return None
        payload = unpack_credentials(row["payload_json"] or "{}", self._credential_encryption_key)
        return {"connector_id": row["connector_id"], "payload": payload, "created_at": row["created_at"]}

    def delete_oauth_state(self, state: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(mcp_oauth_state).where(mcp_oauth_state.c.state == state))

    # --- Per-container connector whitelist ----------------------------

    def list_connectors_for_container(self, container_id: str) -> list[str]:
        """Return the connector ids whitelisted for a container."""
        stmt = (
            select(container_mcp_acl.c.connector_id)
            .where(container_mcp_acl.c.container_id == container_id)
            .order_by(container_mcp_acl.c.added_at)
        )
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

    def list_containers_for_connector(self, connector_id: str) -> list[str]:
        stmt = (
            select(container_mcp_acl.c.container_id)
            .where(container_mcp_acl.c.connector_id == connector_id)
            .order_by(container_mcp_acl.c.added_at)
        )
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

    def set_container_connectors(self, container_id: str, connector_ids: list[str]) -> list[str]:
        """Replace a container's whitelist with exactly ``connector_ids``."""
        now = datetime.now(timezone.utc)
        wanted = [c for c in dict.fromkeys(connector_ids)]  # de-dupe, keep order
        with self._engine.begin() as conn:
            conn.execute(
                delete(container_mcp_acl).where(container_mcp_acl.c.container_id == container_id)
            )
            for cid in wanted:
                conn.execute(
                    insert(container_mcp_acl).values(
                        container_id=container_id, connector_id=cid, added_at=now
                    )
                )
        logger.info(
            "container_mcp_acl_set container_id=%s count=%s", container_id, len(wanted)
        )
        return wanted

    def add_container_connector(self, container_id: str, connector_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(container_mcp_acl.c.connector_id)
                .where(container_mcp_acl.c.container_id == container_id)
                .where(container_mcp_acl.c.connector_id == connector_id)
            ).first()
            if exists:
                return
            conn.execute(
                insert(container_mcp_acl).values(
                    container_id=container_id, connector_id=connector_id, added_at=now
                )
            )

    def remove_container_connector(self, container_id: str, connector_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                delete(container_mcp_acl)
                .where(container_mcp_acl.c.container_id == container_id)
                .where(container_mcp_acl.c.connector_id == connector_id)
            )
