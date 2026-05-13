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

    def __init__(self, url: str = "sqlite:///./data/master.db") -> None:
        _ensure_sqlite_parent_dir(url)
        self._url = url
        self._engine: Engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        metadata.create_all(self._engine)
        logger.info("master_db_initialized url=%s", url)

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
