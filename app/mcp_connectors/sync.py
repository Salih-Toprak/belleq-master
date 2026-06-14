"""Mirror connector changes to the belleq-backend (the only static server).

The master's SQLite lives on an ephemeral EC2 host; when the host is terminated
(e.g. the workspace deleted its last context) the connectors would be lost. To
make connectors durable *per account*, every change is mirrored to the backend,
which persists them in Postgres and re-hydrates a fresh master on the next
provision.

Secrets are forwarded **verbatim** — the already-encrypted ``secrets_json`` blob
from SQLite — so plaintext credentials never leave this process. The backend is
a blind custodian of ciphertext.

Delivery is best-effort and off the request path: changes are queued and a
daemon thread POSTs them, so a slow/unreachable backend never blocks or breaks
local connector operations.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import httpx

from app.mcp_connectors.models import MCPConnectorRecord

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


class BackendConnectorSink:
    """Queue-backed mirror of connector changes to the backend."""

    def __init__(self, base_url: str, internal_token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = internal_token
        self._q: queue.Queue[tuple[str, dict] | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._run, name="connector-sink", daemon=True
        )
        self._worker.start()

    # --- ConnectorSink protocol -------------------------------------------

    def on_upsert(self, record: MCPConnectorRecord, secrets_raw: str) -> None:
        workspace_id = (record.metadata or {}).get("workspace_id", "")
        if not workspace_id:
            # Without a workspace we can't scope it per-account; skip silently.
            logger.debug(
                "connector_mirror_skip_no_workspace connector_id=%s",
                record.connector_id,
            )
            return
        self._q.put(
            (
                "upsert",
                {
                    "workspace_id": workspace_id,
                    "connector_id": record.connector_id,
                    "display_name": record.display_name,
                    "transport": record.transport,
                    "url": record.url or "",
                    "command": record.command or "",
                    "args": record.args or [],
                    "enabled": bool(record.enabled),
                    "auth_status": record.auth_status or "none",
                    "tool_count": int(record.tool_count or 0),
                    "last_status": record.last_status or "unknown",
                    "secrets_encrypted": secrets_raw or "{}",
                    "metadata": record.metadata or {},
                    "added_at": _iso(record.added_at),
                    "updated_at": _iso(record.updated_at),
                },
            )
        )

    def on_remove(self, record: MCPConnectorRecord) -> None:
        workspace_id = (record.metadata or {}).get("workspace_id", "")
        if not workspace_id:
            return
        self._q.put(
            ("remove", {"workspace_id": workspace_id, "connector_id": record.connector_id})
        )

    def close(self) -> None:
        self._q.put(None)

    # --- worker -----------------------------------------------------------

    def _run(self) -> None:
        headers = {"X-Internal-Token": self._token}
        with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
            while True:
                item = self._q.get()
                if item is None:
                    return
                action, payload = item
                try:
                    if action == "upsert":
                        client.post(f"{self._base}/internal/connectors/sync", json=payload)
                    elif action == "remove":
                        client.delete(
                            f"{self._base}/internal/connectors/"
                            f"{payload['workspace_id']}/{payload['connector_id']}"
                        )
                except Exception:  # noqa: BLE001 — best-effort; just log
                    logger.warning(
                        "connector_mirror_post_failed action=%s connector_id=%s",
                        action,
                        payload.get("connector_id"),
                        exc_info=True,
                    )
                finally:
                    self._q.task_done()
