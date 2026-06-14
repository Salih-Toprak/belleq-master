"""SQLite-backed MCP connector registry (delegates to MasterDB)."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.database import MasterDB
from app.mcp_connectors.models import MCPConnectorRecord

logger = logging.getLogger(__name__)


class ConnectorSink(Protocol):
    """Receives connector changes so they can be mirrored off-instance.

    ``on_upsert`` is given the record plus the secrets blob *verbatim* (still
    encrypted) so plaintext never leaves the master. Implementations must be
    non-blocking and must never raise.
    """

    def on_upsert(self, record: MCPConnectorRecord, secrets_raw: str) -> None: ...

    def on_remove(self, record: MCPConnectorRecord) -> None: ...


class MCPConnectorRegistry:
    """Thin wrapper over MasterDB for upstream MCP connectors + their ACL."""

    def __init__(self, db: MasterDB, sink: ConnectorSink | None = None) -> None:
        self._db = db
        self._sink = sink
        logger.debug("mcp_connector_registry_initialized")

    def set_sink(self, sink: ConnectorSink | None) -> None:
        self._sink = sink

    def _mirror_upsert(self, record: MCPConnectorRecord | None) -> None:
        if self._sink is None or record is None:
            return
        try:
            raw = self._db.get_connector_secrets_raw(record.connector_id)
            self._sink.on_upsert(record, raw)
        except Exception:  # noqa: BLE001 — mirroring must never break local ops
            logger.warning(
                "connector_mirror_upsert_failed connector_id=%s",
                record.connector_id,
                exc_info=True,
            )

    def _mirror_remove(self, record: MCPConnectorRecord | None) -> None:
        if self._sink is None or record is None:
            return
        try:
            self._sink.on_remove(record)
        except Exception:  # noqa: BLE001
            logger.warning(
                "connector_mirror_remove_failed connector_id=%s",
                record.connector_id,
                exc_info=True,
            )

    # --- connectors ---------------------------------------------------

    def list_all(self, enabled_only: bool = False) -> list[MCPConnectorRecord]:
        return self._db.list_connectors(enabled_only=enabled_only)

    def get(self, connector_id: str) -> MCPConnectorRecord | None:
        return self._db.get_connector(connector_id)

    def add(self, record: MCPConnectorRecord) -> None:
        self._db.add_connector(record)
        self._mirror_upsert(self.get(record.connector_id) or record)

    def update(self, connector_id: str, updates: dict[str, Any]) -> MCPConnectorRecord:
        record = self._db.update_connector(connector_id, updates)
        self._mirror_upsert(record)
        return record

    def remove(self, connector_id: str) -> None:
        record = self._db.get_connector(connector_id)
        self._db.remove_connector(connector_id)
        self._mirror_remove(record)

    def import_record(self, record: MCPConnectorRecord, secrets_raw: str) -> bool:
        """Hydrate a connector pushed from the backend, storing secrets verbatim.

        Does NOT fire the sink — this is the backend → master direction, so
        re-mirroring would loop. Returns True if written, False if a newer local
        copy was kept.
        """
        return self._db.import_connector(record, secrets_raw)

    def enable(self, connector_id: str) -> MCPConnectorRecord:
        return self.update(connector_id, {"enabled": True})

    def disable(self, connector_id: str) -> MCPConnectorRecord:
        return self.update(connector_id, {"enabled": False})

    def record_test_result(
        self, connector_id: str, status: str, error: str, tool_count: int
    ) -> MCPConnectorRecord:
        from datetime import datetime, timezone

        return self.update(
            connector_id,
            {
                "last_status": status,
                "last_error": error or "",
                "last_checked_at": datetime.now(timezone.utc),
                "tool_count": int(tool_count),
            },
        )

    # --- per-container whitelist --------------------------------------

    def connectors_for_container(self, container_id: str) -> list[str]:
        return self._db.list_connectors_for_container(container_id)

    def containers_for_connector(self, connector_id: str) -> list[str]:
        return self._db.list_containers_for_connector(connector_id)

    def set_container_connectors(self, container_id: str, connector_ids: list[str]) -> list[str]:
        return self._db.set_container_connectors(container_id, connector_ids)

    def add_container_connector(self, container_id: str, connector_id: str) -> None:
        self._db.add_container_connector(container_id, connector_id)

    def remove_container_connector(self, container_id: str, connector_id: str) -> None:
        self._db.remove_container_connector(container_id, connector_id)

    def refresh_oauth(self, connector_id: str, oauth: dict) -> MCPConnectorRecord:
        """Persist refreshed OAuth tokens for a connector."""
        return self.update(connector_id, {"oauth": oauth, "auth_status": "connected"})

    def enabled_connectors_for_container(self, container_id: str) -> list[MCPConnectorRecord]:
        """Resolve a container's whitelist to the actual enabled connector records."""
        wanted = set(self.connectors_for_container(container_id))
        if not wanted:
            return []
        return [c for c in self.list_all(enabled_only=True) if c.connector_id in wanted]
