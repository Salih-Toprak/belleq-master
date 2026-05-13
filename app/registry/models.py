"""Registry domain models (dataclasses, no persistence logic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ContainerRecord:
    """Registered user/chatbot container endpoint and credentials."""

    container_id: str
    display_name: str
    container_type: str  # chatbot | user | agent
    base_url: str
    api_key: str = ""
    enabled: bool = True
    added_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ContainerStatus:
    """Aggregated view of a container after a health or stats probe."""

    container_id: str
    display_name: str
    container_type: str
    base_url: str
    enabled: bool
    reachable: bool
    checked_at: str
    response_ms: int | None
    stats: dict | None
