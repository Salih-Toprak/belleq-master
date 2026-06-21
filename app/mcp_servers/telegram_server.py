"""A tiny, self-contained Telegram MCP server (stdio).

This is belleq's own bundled connector for Telegram — a direct connection to the
Telegram Bot API using the user's own bot token, with no third-party aggregator
(Zapier/Pipedream) and no Node/npx. It's launched as a stdio subprocess by the
master's connector aggregator:

    transport = stdio
    command   = python
    args      = ["-m", "app.mcp_servers.telegram_server"]
    env       = { TELEGRAM_BOT_TOKEN: <bot token>, TELEGRAM_CHAT_ID: <default chat> }

Create a bot and get the token from @BotFather. To find a chat id, message the
bot (or add it to a group) and call the ``telegram_get_chat_id`` tool.

Deliberately depends only on the stdlib + httpx + fastmcp (all already in the
master image) so it runs without installing anything at call time.
"""

from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Telegram")

_API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def _call(method: str, **payload):
    token = _token()
    if not token:
        return None, "TELEGRAM_BOT_TOKEN is not set on this connector."
    try:
        r = httpx.post(_API.format(token=token, method=method), json=payload, timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not reach Telegram: {exc}"
    if r.status_code >= 400:
        return None, f"Telegram API error {r.status_code}: {r.text[:300]}"
    return r.json(), None


@mcp.tool
def send_message(text: str, chat_id: str | None = None) -> str:
    """Send a Telegram message. Use this to notify the user. ``chat_id`` defaults
    to the connector's configured chat when omitted."""
    cid = (chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not cid:
        return (
            "No chat id. Set a default Chat ID on the Telegram connector, or pass "
            "chat_id. Use telegram_get_chat_id to find it (message the bot first)."
        )
    if not (text or "").strip():
        return "Nothing to send: text is empty."
    data, err = _call("sendMessage", chat_id=cid, text=text)
    if err:
        return err
    return "Message sent."


@mcp.tool
def get_chat_id() -> str:
    """List recent chats that have messaged this bot, with their chat ids — so you
    can find the id to send to. Message the bot (or add it to a group) first."""
    data, err = _call_get("getUpdates")
    if err:
        return err
    seen: dict[str, str] = {}
    for upd in (data or {}).get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        name = chat.get("title") or " ".join(
            x for x in [chat.get("first_name"), chat.get("last_name")] if x
        ) or chat.get("username") or "chat"
        seen[str(cid)] = name
    if not seen:
        return "No recent chats. Send a message to the bot, then try again."
    return "\n".join(f"{name}: {cid}" for cid, name in seen.items())


def _call_get(method: str):
    token = _token()
    if not token:
        return None, "TELEGRAM_BOT_TOKEN is not set on this connector."
    try:
        r = httpx.get(_API.format(token=token, method=method), timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not reach Telegram: {exc}"
    if r.status_code >= 400:
        return None, f"Telegram API error {r.status_code}: {r.text[:300]}"
    return r.json(), None


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
