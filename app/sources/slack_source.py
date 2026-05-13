"""Slack API reader for ingestion."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from app.sources.models import DataSourceRecord

logger = logging.getLogger(__name__)


_MENTION_RE = re.compile(r"<@[^>]+>")
_CHANNEL_RE = re.compile(r"<#[^|>]+\|([^>]+)>")
_CHANNEL_RAW_RE = re.compile(r"<#[A-Z0-9]+>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")


class SlackSource:
    """Fetch channel messages for ingestion."""

    def __init__(self, record: DataSourceRecord) -> None:
        self.record = record
        creds = record.credentials or {}
        self.bot_token = str(creds.get("bot_token", ""))
        self.access_control = dict(record.access_control or {})
        self.allowed_channels = list(self.access_control.get("allowed_channels") or [])
        self.excluded_channels = set(self.access_control.get("excluded_channels") or [])
        self.include_threads = bool(self.access_control.get("include_threads", True))
        self.max_history_days = int(self.access_control.get("max_history_days", 90))
        self._client = AsyncWebClient(token=self.bot_token)
        self._channel_cache: dict[str, str] | None = None
        self._workspace: str = ""

    async def validate_credentials(self) -> dict[str, Any]:
        try:
            r = await self._client.auth_test()
            if r.get("ok"):
                self._workspace = str(r.get("team", "") or r.get("url", "") or "")
                return {"valid": True, "workspace": self._workspace, "error": ""}
            return {"valid": False, "workspace": "", "error": str(r.get("error", "unknown"))}
        except Exception as e:  # noqa: BLE001
            return {"valid": False, "workspace": "", "error": str(e)}

    async def _resolve_channels(self) -> dict[str, str]:
        if self._channel_cache is not None:
            return self._channel_cache
        name_to_id: dict[str, str] = {}
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            r = await self._client.conversations_list(**kwargs)
            if not r.get("ok"):
                logger.warning("slack_conversations_list_failed error=%s", r.get("error"))
                break
            for ch in r.get("channels") or []:
                cid = ch.get("id")
                name = ch.get("name")
                if cid and name:
                    name_to_id[str(name)] = str(cid)
            cursor = r.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
        self._channel_cache = name_to_id
        return name_to_id

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        s = _MENTION_RE.sub("", text)
        s = _CHANNEL_RE.sub(r"\1", s)
        s = _CHANNEL_RAW_RE.sub("", s)
        s = _LINK_RE.sub(r"\2", s)
        s = re.sub(r":[a-z0-9_+-]+:", "", s, flags=re.I)
        return s.strip()

    def _ts_to_url(self, channel_id: str, ts: str) -> str:
        ts_clean = ts.replace(".", "")
        if self._workspace and self._workspace.startswith("http"):
            return f"{self._workspace}/archives/{channel_id}/p{ts_clean}"
        return ""

    async def fetch_messages(self, since: datetime | None = None) -> list[dict[str, Any]]:
        if not self.bot_token:
            return []
        auth = await self.validate_credentials()
        if not auth.get("valid"):
            logger.warning("slack_fetch_skipped_invalid_auth error=%s", auth.get("error"))
            return []

        name_map = await self._resolve_channels()
        now = datetime.now(timezone.utc)
        if since is None:
            since = now - timedelta(days=self.max_history_days)
        oldest = str(since.timestamp())

        out: list[dict[str, Any]] = []
        for ch_name in self.allowed_channels:
            if ch_name in self.excluded_channels:
                continue
            channel_id = name_map.get(ch_name)
            if not channel_id:
                logger.warning("slack_channel_not_found name=%s", ch_name)
                continue
            cursor = None
            while True:
                kwargs: dict[str, Any] = {
                    "channel": channel_id,
                    "limit": 200,
                    "oldest": oldest,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                hist = await self._client.conversations_history(**kwargs)
                if not hist.get("ok"):
                    logger.warning("slack_history_failed channel=%s err=%s", ch_name, hist.get("error"))
                    break
                for msg in hist.get("messages") or []:
                    if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                        continue
                    text_raw = str(msg.get("text", ""))
                    text = self._clean_text(text_raw)
                    if len(text.strip()) <= 10:
                        continue
                    ts = str(msg.get("ts", ""))
                    uid = str(msg.get("user", ""))
                    thread_ts = msg.get("thread_ts")
                    is_reply = thread_ts is not None and thread_ts != ts
                    out.append(
                        {
                            "channel_name": ch_name,
                            "channel_id": channel_id,
                            "ts": ts,
                            "text": text,
                            "user": uid,
                            "thread_ts": str(thread_ts) if thread_ts else None,
                            "is_thread_reply": bool(is_reply),
                            "message_url": self._ts_to_url(channel_id, ts),
                        }
                    )
                    if self.include_threads and int(msg.get("reply_count", 0) or 0) > 0 and msg.get("ts"):
                        rep = await self._client.conversations_replies(
                            channel=channel_id,
                            ts=msg["ts"],
                            limit=200,
                        )
                        if rep.get("ok"):
                            for rmsg in (rep.get("messages") or [])[1:]:
                                rt = self._clean_text(str(rmsg.get("text", "")))
                                if len(rt.strip()) <= 10:
                                    continue
                                rts = str(rmsg.get("ts", ""))
                                out.append(
                                    {
                                        "channel_name": ch_name,
                                        "channel_id": channel_id,
                                        "ts": rts,
                                        "text": rt,
                                        "user": str(rmsg.get("user", "")),
                                        "thread_ts": str(msg.get("ts")),
                                        "is_thread_reply": True,
                                        "message_url": self._ts_to_url(channel_id, rts),
                                    }
                                )
                meta = hist.get("response_metadata") or {}
                cursor = meta.get("next_cursor") or None
                if not hist.get("has_more") or not cursor:
                    break
        out.sort(key=lambda m: (m["channel_id"], float(m["ts"] or 0)))
        return out

    async def close(self) -> None:
        await self._client.close()
