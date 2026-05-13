"""Thin SQLite-backed container registry."""

from __future__ import annotations

import logging
from typing import Any

from app.database import MasterDB
from app.registry.models import ContainerRecord

logger = logging.getLogger(__name__)


class ContainerRegistry:
    """Delegates persistence to MasterDB."""

    def __init__(self, db: MasterDB) -> None:
        self._db = db
        logger.debug("registry_initialized")

    def list_all(self) -> list[ContainerRecord]:
        return self._db.list_containers(enabled_only=False)

    def list_enabled(self) -> list[ContainerRecord]:
        return self._db.list_containers(enabled_only=True)

    def get(self, container_id: str) -> ContainerRecord | None:
        return self._db.get_container(container_id)

    def add(self, record: ContainerRecord) -> None:
        self._db.add_container(record)

    def update(self, container_id: str, updates: dict[str, Any]) -> ContainerRecord:
        return self._db.update_container(container_id, updates)

    def remove(self, container_id: str) -> None:
        self._db.remove_container(container_id)

    def enable(self, container_id: str) -> None:
        self._db.enable_container(container_id)

    def disable(self, container_id: str) -> None:
        self._db.disable_container(container_id)
