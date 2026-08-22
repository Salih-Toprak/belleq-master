"""belleq's bundled Gmail MCP server (stdio).

A direct connection to the user's Gmail — no Zapier/Pipedream in the middle.
Launched as a stdio subprocess by the master's connector aggregator:

    transport = stdio
    command   = python
    args      = ["-m", "app.mcp_servers.gmail_server"]
    env       = { GOOGLE_REFRESH_TOKEN: <this connector's grant> }

Google Calendar is a separate connector (app.mcp_servers.google_calendar_server)
with its own consent, so granting Gmail never grants calendar access.

Auth/token handling is shared — see google_common.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from fastmcp import FastMCP

from app.mcp_servers.google_common import request

mcp = FastMCP("Gmail")

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


# ── helpers ──────────────────────────────────────────────────────────────────

def _headers_map(msg: dict) -> dict[str, str]:
    hs = (msg.get("payload") or {}).get("headers") or []
    return {h.get("name", "").lower(): h.get("value", "") for h in hs}


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _plain_body(payload: dict) -> str:
    """Best-effort plain-text body: prefer text/plain, fall back to anything."""
    if not payload:
        return ""
    body = payload.get("body") or {}
    if payload.get("mimeType") == "text/plain" and body.get("data"):
        return _decode(body["data"])
    fallback = ""
    for part in payload.get("parts") or []:
        found = _plain_body(part)
        if found and part.get("mimeType") == "text/plain":
            return found
        if found and not fallback:
            fallback = found
    if fallback:
        return fallback
    if body.get("data"):
        return _decode(body["data"])
    return ""


def _summarize(msg: dict) -> str:
    h = _headers_map(msg)
    date = h.get("date", "")
    try:
        date = parsedate_to_datetime(date).strftime("%Y-%m-%d %H:%M") if date else ""
    except Exception:  # noqa: BLE001
        pass
    unread = "UNREAD" in (msg.get("labelIds") or [])
    return (
        f"[{msg.get('id')}]{' (unread)' if unread else ''} {date}\n"
        f"  From:    {h.get('from', '?')}\n"
        f"  Subject: {h.get('subject', '(no subject)')}\n"
        f"  {(msg.get('snippet') or '').strip()[:200]}"
    )


def _raw_message(to: str, subject: str, body: str, cc: str = "") -> str:
    em = EmailMessage()
    em["To"] = to
    if cc:
        em["Cc"] = cc
    em["Subject"] = subject
    em.set_content(body)
    return base64.urlsafe_b64encode(em.as_bytes()).decode()


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool
def gmail_search(query: str = "", max_results: int = 10) -> str:
    """Search the user's Gmail and return matching messages (id, sender, subject,
    date, snippet). ``query`` uses Gmail search syntax, e.g. "is:unread",
    "from:amy@x.com", "subject:invoice newer_than:7d". Leave empty for the most
    recent mail. Use gmail_read with an id to get the full body."""
    params = {"maxResults": max(1, min(int(max_results or 10), 50))}
    if (query or "").strip():
        params["q"] = query.strip()
    listing, err = request("GET", f"{_GMAIL}/messages", params=params)
    if err:
        return err
    ids = [m["id"] for m in (listing.get("messages") or [])]
    if not ids:
        return "No messages matched that search."

    out = []
    for mid in ids:
        msg, err = request(
            "GET",
            f"{_GMAIL}/messages/{mid}",
            params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        )
        if err:
            return err
        out.append(_summarize(msg))
    return f"{len(out)} message(s):\n\n" + "\n\n".join(out)


@mcp.tool
def gmail_read(message_id: str) -> str:
    """Read one Gmail message in full — headers plus the message body. Get the
    message_id from gmail_search."""
    if not (message_id or "").strip():
        return "message_id is required (get one from gmail_search)."
    msg, err = request("GET", f"{_GMAIL}/messages/{message_id.strip()}", params={"format": "full"})
    if err:
        return err
    h = _headers_map(msg)
    body = _plain_body(msg.get("payload") or {}).strip()
    return (
        f"From:    {h.get('from', '?')}\n"
        f"To:      {h.get('to', '?')}\n"
        f"Date:    {h.get('date', '?')}\n"
        f"Subject: {h.get('subject', '(no subject)')}\n"
        f"Thread:  {msg.get('threadId', '')}\n\n"
        f"{body or '(no readable text body)'}"
    )


@mcp.tool
def gmail_send(to: str, subject: str, body: str, cc: str = "") -> str:
    """Send an email from the user's Gmail account. ``to``/``cc`` accept
    comma-separated addresses. Confirm the content with the user before sending —
    this delivers immediately. Use gmail_draft instead to save without sending."""
    if not (to or "").strip():
        return "A recipient (to) is required."
    payload = {"raw": _raw_message(to.strip(), subject or "", body or "", (cc or "").strip())}
    _, err = request("POST", f"{_GMAIL}/messages/send", json=payload)
    if err:
        return err
    return f"Sent to {to}."


@mcp.tool
def gmail_draft(to: str, subject: str, body: str, cc: str = "") -> str:
    """Save an email as a draft in the user's Gmail without sending it. Good for
    letting the user review a reply before it goes out."""
    payload = {
        "message": {"raw": _raw_message((to or "").strip(), subject or "", body or "", (cc or "").strip())}
    }
    data, err = request("POST", f"{_GMAIL}/drafts", json=payload)
    if err:
        return err
    return f"Draft saved (id {data.get('id', '?')}). The user can review and send it from Gmail."


@mcp.tool
def gmail_reply(message_id: str, body: str) -> str:
    """Reply to a message, keeping it in the same Gmail thread. Replies to the
    original sender with a "Re:" subject."""
    if not (message_id or "").strip():
        return "message_id is required (get one from gmail_search)."
    original, err = request(
        "GET",
        f"{_GMAIL}/messages/{message_id.strip()}",
        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Message-ID"]},
    )
    if err:
        return err
    h = _headers_map(original)
    subject = h.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    em = EmailMessage()
    em["To"] = h.get("from", "")
    em["Subject"] = subject
    if h.get("message-id"):
        em["In-Reply-To"] = h["message-id"]
        em["References"] = h["message-id"]
    em.set_content(body or "")
    payload = {
        "raw": base64.urlsafe_b64encode(em.as_bytes()).decode(),
        "threadId": original.get("threadId", ""),
    }
    _, err = request("POST", f"{_GMAIL}/messages/send", json=payload)
    if err:
        return err
    return f"Replied to {h.get('from', 'sender')}."


@mcp.tool
def gmail_modify(message_id: str, archive: bool = False, mark_read: bool = False,
                 mark_unread: bool = False, star: bool = False) -> str:
    """Triage a message: archive it (removes it from the inbox), mark it read or
    unread, and/or star it. Set the flags you want; others are left untouched."""
    if not (message_id or "").strip():
        return "message_id is required (get one from gmail_search)."
    add: list[str] = []
    remove: list[str] = []
    if archive:
        remove.append("INBOX")
    if mark_read:
        remove.append("UNREAD")
    if mark_unread:
        add.append("UNREAD")
    if star:
        add.append("STARRED")
    if not add and not remove:
        return "Nothing to change — set archive, mark_read, mark_unread, or star."
    _, err = request(
        "POST",
        f"{_GMAIL}/messages/{message_id.strip()}/modify",
        json={"addLabelIds": add, "removeLabelIds": remove},
    )
    if err:
        return err
    done = []
    if archive:
        done.append("archived")
    if mark_read:
        done.append("marked read")
    if mark_unread:
        done.append("marked unread")
    if star:
        done.append("starred")
    return "Message " + ", ".join(done) + "."


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
