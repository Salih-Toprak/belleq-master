"""MCP response capture (4C): observe connector tool results and ingest the
document-like ones into the context's knowledge base.

A FastMCP middleware on the per-context parent proxy sees every tool result that
flows through. When a connector returns a substantial chunk of text (a fetched
Notion page, a GitHub file, a Linear issue body, …), we forward it to that
context's belleq-user container, which queues it for chunk → embed → index.

Hard rules:
- Best-effort and fail-safe: any error here is swallowed; it must NEVER affect
  the tool call the user actually made.
- Skip Belleq's own `belleq_kb_*` tools (don't capture memory reads/writes — that
  would create a feedback loop).
- Skip short results (not document-like) and obvious errors.
- Fire-and-forget: the capture POST never blocks returning the tool result.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

# Tool-name prefixes whose results we never capture (our own memory tools).
_SKIP_PREFIXES = ("belleq_kb_",)


def _text_from_result(result: Any) -> str:
    """Concatenate text content blocks from a ToolResult."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n\n".join(parts)


class CaptureMiddleware(Middleware):
    """Captures document-like connector responses for one context."""

    def __init__(
        self,
        container_id: str,
        capture_url: str,
        admin_key: str,
        *,
        min_chars: int = 800,
        exclude_namespaces: set[str] | None = None,
    ) -> None:
        self._container_id = container_id
        self._url = capture_url
        self._admin_key = admin_key
        self._min_chars = max(1, int(min_chars))
        self._exclude = exclude_namespaces or set()

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # noqa: ANN001
        result = await call_next(context)
        try:
            self._maybe_capture(context, result)
        except Exception:  # noqa: BLE001 — capture must never break a tool call
            logger.debug("capture_middleware_swallowed", exc_info=True)
        return result

    def _maybe_capture(self, context: MiddlewareContext, result: Any) -> None:
        tool_name = getattr(getattr(context, "message", None), "name", "") or ""
        if any(tool_name.startswith(p) for p in _SKIP_PREFIXES):
            return
        # Aggregated tool names are "<namespace>_<tool>"; the namespace is the
        # connector id. Allow per-source opt-out.
        namespace = tool_name.split("_", 1)[0] if "_" in tool_name else tool_name
        if namespace in self._exclude:
            return
        if getattr(result, "is_error", False):
            return

        text = _text_from_result(result)
        if len(text.strip()) < self._min_chars:
            return

        # Fire-and-forget so we never delay returning the result to the client.
        asyncio.create_task(self._send(tool_name, namespace, text))

    async def _send(self, tool_name: str, namespace: str, text: str) -> None:
        payload = {
            "text": text,
            "title": f"{namespace}: {tool_name}",
            "source_label": namespace,
            "tool": tool_name,
        }
        headers = {"X-Master-Key": self._admin_key} if self._admin_key else {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(self._url, json=payload, headers=headers)
            logger.info(
                "mcp_capture_sent container=%s tool=%s chars=%d",
                self._container_id, tool_name, len(text),
            )
        except Exception:  # noqa: BLE001
            logger.debug("mcp_capture_send_failed container=%s tool=%s", self._container_id, tool_name, exc_info=True)
