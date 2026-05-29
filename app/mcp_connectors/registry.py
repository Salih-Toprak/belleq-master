"""SQLite-backed MCP connector registry (delegates to MasterDB)."""

from __future__ import annotations

import logging
from typing import Any

from app.database import MasterDB
from app.mcp_connectors.models import MCPConnectorRecord

logger = logging.getLogger(__name__)


class MCPConnectorRegistry:
    """Thin wrapper over MasterDB for upstream MCP connectors + their ACL."""

    def __init__(self, db: MasterDB) -> None:
        self._db = db
        logger.debug("mcp_connector_registry_initialized")

    # --- connectors ---------------------------------------------------

    def list_all(self, enabled_only: bool = False) -> list[MCPConnectorRecord]:
        return self._db.list_connectors(enabled_only=enabled_only)

    def get(self, connector_id: str) -> MCPConnectorRecord | None:
        return self._db.get_connector(connector_id)

    def add(self, record: MCPConnectorRecord) -> None:
        self._db.add_connector(record)

    def update(self, connector_id: str, updates: dict[str, Any]) -> MCPConnectorRecord:
        return self._db.update_connector(connector_id, updates)

    def remove(self, connector_id: str) -> None:
        self._db.remove_connector(connector_id)

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

    def enabled_connectors_for_container(self, container_id: str) -> list[MCPConnectorRecord]:
        """Resolve a container's whitelist to the actual enabled connector records."""
        wanted = set(self.connectors_for_container(container_id))
        if not wanted:
            return []
        return [c for c in self.list_all(enabled_only=True) if c.connector_id in wanted]
