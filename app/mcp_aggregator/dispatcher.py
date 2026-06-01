"""ASGI dispatcher that serves a per-container aggregated MCP endpoint.

Mounted at ``/mcp`` on the master.  A request to ``/mcp/{container_id}`` is
routed to a FastMCP proxy built for that container — mounting only its
whitelisted connectors, each namespaced by connector id.  Apps are cached and
rebuilt when the container's whitelist or any connector's token changes.

Key implementation details
--------------------------
* Each per-container FastMCP ``http_app`` is built with ``path="/"``.
  The ``as_asgi()`` dispatcher rewrites ``scope["path"]`` to ``"/"`` before
  handing off to the sub-app, so the sub-app always sees a root-level request.
* ``scope["app"]`` is overwritten to point at the per-container Starlette app
  before delegation.  Without this, the sub-app's internal router sees the
  outer FastAPI app instance and fails to match its own routes.
* The sub-app's lifespan (which initialises FastMCP's session-manager task
  group) is entered in a long-lived background task rather than from within
  a request, preventing "Task group is not initialized" errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

from app.mcp_connectors.registry import MCPConnectorRegistry
from app.mcp_connectors.upstream import build_client

logger = logging.getLogger(__name__)


def _safe_namespace(connector_id: str) -> str:
    """Connector id -> a tool-name-safe namespace prefix."""
    ns = re.sub(r"[^a-zA-Z0-9_]", "_", connector_id).strip("_")
    return ns or "tool"


# ---------------------------------------------------------------------------
# Per-container entry
# ---------------------------------------------------------------------------

class _Entry:
    """A cached per-container ASGI sub-application with managed lifespan."""

    __slots__ = ("app", "signature", "_task", "_stop")

    def __init__(self, app: Any, signature: str) -> None:
        self.app = app
        self.signature = signature
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def start_lifespan(self) -> None:
        """Enter the sub-app's lifespan in a background task.

        FastMCP's StreamableHTTPSessionManager requires its task group to be
        alive for the duration of every request.  Running it in a long-lived
        background task keeps it alive across requests.
        """
        self._stop = asyncio.Event()
        ready = asyncio.Event()
        err: dict[str, BaseException] = {}

        async def _runner() -> None:
            try:
                async with self.app.router.lifespan_context(self.app):
                    ready.set()
                    await self._stop.wait()
            except BaseException as e:  # noqa: BLE001
                err["e"] = e
                ready.set()

        self._task = asyncio.create_task(_runner())
        await ready.wait()
        if "e" in err:
            raise err["e"]

    async def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class ContainerMCPDispatcher:
    """ASGI app: routes ``/mcp/{container_id}`` to a per-container FastMCP proxy.

    Usage::

        dispatcher = ContainerMCPDispatcher(registry)
        app.mount("/mcp", dispatcher.as_asgi())

    The ``as_asgi()`` function extracts the container id from the path,
    builds (or reuses) the per-container FastMCP proxy, rewrites the ASGI
    scope so the sub-app sees ``path="/"``, and delegates.
    """

    def __init__(self, registry: MCPConnectorRegistry) -> None:
        self._registry = registry
        self._cache: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    # --- building --------------------------------------------------------

    @staticmethod
    def _signature(connectors: list) -> str:
        parts = []
        for c in connectors:
            token = (c.oauth or {}).get("access_token", "")
            updated = c.updated_at.isoformat() if c.updated_at else ""
            parts.append(f"{c.connector_id}|{c.transport}|{c.url}|{bool(token)}|{updated}")
        raw = "\n".join(sorted(parts))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _build_proxy(self, container_id: str, connectors: list):
        """Construct (sync) a FastMCP server aggregating the connectors."""
        from fastmcp import FastMCP
        from fastmcp.server import create_proxy

        parent = FastMCP(name=f"belleq-{container_id}")
        for conn in connectors:
            try:
                sub = create_proxy(build_client(conn))
                parent.mount(sub, namespace=_safe_namespace(conn.connector_id))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "aggregator_mount_failed container=%s connector=%s",
                    container_id,
                    conn.connector_id,
                    exc_info=True,
                )
        return parent

    async def _ensure_container(self, container_id: str) -> _Entry:
        """Build (or reuse) the per-container sub-app and return its entry."""
        connectors = self._registry.enabled_connectors_for_container(container_id)
        signature = self._signature(connectors)

        async with self._lock:
            entry = self._cache.get(container_id)
            if entry is not None and entry.signature == signature:
                return entry

            # Tear down the old entry if the whitelist/tokens changed.
            if entry is not None:
                await entry.close()

            proxy = self._build_proxy(container_id, connectors)
            # Serve at "/" — we rewrite scope["path"] to "/" before delegating.
            asgi_app = proxy.http_app(path="/", stateless_http=True)

            new_entry = _Entry(asgi_app, signature)
            await new_entry.start_lifespan()

            self._cache[container_id] = new_entry
            logger.info(
                "aggregator_built container=%s connectors=%s",
                container_id,
                len(connectors),
            )
            return new_entry

    # --- ASGI entry point ------------------------------------------------

    def as_asgi(self):
        """Return the ASGI callable to mount on the FastAPI instance.

        Mounted at ``/mcp``, so Starlette strips that prefix.  This function
        receives ``scope["path"] == "/{container_id}"``, extracts the id,
        rewrites the scope to ``path="/"`` + ``scope["app"] = sub_app``, and
        delegates to the per-container Starlette ASGI app.
        """
        dispatcher = self

        async def _app(scope: dict, receive, send) -> None:  # noqa: ANN001
            if scope["type"] == "lifespan":
                await _handle_lifespan(receive, send)
                return
            if scope["type"] != "http":
                return

            path = scope.get("path", "") or "/"
            segments = [s for s in path.split("/") if s]
            if not segments:
                await _send_plain(send, 404, "Specify a container: /mcp/{container_id}")
                return

            container_id = segments[0]
            try:
                entry = await dispatcher._ensure_container(container_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("aggregator_build_failed container=%s", container_id)
                await _send_plain(send, 502, f"Aggregator error: {exc}")
                return

            # Rewrite the scope so the per-container app (which serves at "/")
            # sees a root-level request.  Also fix scope["app"] to point at the
            # sub-app instead of the outer FastAPI instance.
            child_scope = dict(scope)
            child_scope["path"] = "/"
            child_scope["root_path"] = scope.get("root_path", "") + f"/{container_id}"
            child_scope["app"] = entry.app

            await entry.app(child_scope, receive, send)

        return _app

    async def aclose(self) -> None:
        async with self._lock:
            for entry in self._cache.values():
                await entry.close()
            self._cache.clear()


# --- helpers -----------------------------------------------------------------

async def _handle_lifespan(receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _send_plain(send, status: int, body: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body.encode()})
