"""belleq's bundled Google (Gmail + Calendar) MCP server (stdio).

A direct connection to the user's Google account — no Zapier/Pipedream in the
middle. Launched as a stdio subprocess by the master's connector aggregator:

    transport = stdio
    command   = python
    args      = ["-m", "app.mcp_servers.google_server"]
    env       = { GOOGLE_REFRESH_TOKEN: <this user's token> }

Credentials come from two places, deliberately:
  • GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — belleq's own OAuth app, set on the
    master (inherited from its environment). Platform-wide, never per-user, and
    never sent to the browser.
  • GOOGLE_REFRESH_TOKEN — this connector's per-user grant, stored encrypted in
    the connector record and overlaid onto the subprocess env.

Only the short-lived access token is minted here (cached until it expires).

Depends only on stdlib + httpx + fastmcp (already in the master image).
"""

from __future__ import annotations

import base64
import os
import time
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Google")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_CALENDAR = "https://www.googleapis.com/calendar/v3"
_TIMEOUT = 30.0

# Cached access token: (token, expires_at_epoch). Refreshed on demand.
_access: tuple[str, float] = ("", 0.0)


# ── auth ─────────────────────────────────────────────────────────────────────

def _access_token() -> tuple[str, str]:
    """Return (token, error). Mints a fresh access token from the refresh token
    when the cached one is missing or within 60s of expiry."""
    global _access
    tok, exp = _access
    if tok and time.time() < exp - 60:
        return tok, ""

    refresh = (os.environ.get("GOOGLE_REFRESH_TOKEN") or "").strip()
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not refresh:
        return "", "This Google connector isn't connected yet — reconnect it in the belleq dashboard."
    if not client_id or not client_secret:
        return "", "Google is not configured on this server (missing client credentials)."

    try:
        r = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"Could not reach Google to refresh access: {exc}"
    if r.status_code >= 400:
        # invalid_grant = the user revoked access, or the token expired (Google
        # expires refresh tokens after 7 days while the OAuth app is in Testing).
        if "invalid_grant" in r.text:
            return "", (
                "Google access has expired or was revoked. Reconnect the Google "
                "connector in the belleq dashboard."
            )
        return "", f"Google rejected the token refresh ({r.status_code}): {r.text[:200]}"

    data = r.json()
    token = data.get("access_token", "")
    if not token:
        return "", "Google returned no access token."
    _access = (token, time.time() + float(data.get("expires_in", 3600)))
    return token, ""


def _request(method: str, url: str, **kwargs):
    """Authorized Google API call. Returns (json, error_message)."""
    token, err = _access_token()
    if err:
        return None, err
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    try:
        r = httpx.request(method, url, headers=headers, timeout=_TIMEOUT, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not reach Google: {exc}"
    if r.status_code == 403:
        return None, (
            "Google denied this request — the connector may not have been granted "
            f"that permission. Details: {r.text[:200]}"
        )
    if r.status_code >= 400:
        return None, f"Google API error {r.status_code}: {r.text[:250]}"
    if not r.content:
        return {}, ""
    return r.json(), ""


# ── gmail helpers ────────────────────────────────────────────────────────────

def _headers_map(msg: dict) -> dict[str, str]:
    hs = (msg.get("payload") or {}).get("headers") or []
    return {h.get("name", "").lower(): h.get("value", "") for h in hs}


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _plain_body(payload: dict) -> str:
    """Best-effort plain-text body: prefer text/plain, fall back to text/html."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])
    html = ""
    for part in payload.get("parts") or []:
        found = _plain_body(part)
        if found and part.get("mimeType") == "text/plain":
            return found
        if found and not html:
            html = found
    if html:
        return html
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


# ── gmail tools ──────────────────────────────────────────────────────────────

@mcp.tool
def gmail_search(query: str = "", max_results: int = 10) -> str:
    """Search the user's Gmail and return matching messages (id, sender, subject,
    date, snippet). ``query`` uses Gmail search syntax, e.g. "is:unread",
    "from:amy@x.com", "subject:invoice newer_than:7d". Leave empty for the most
    recent mail. Use gmail_read with an id to get the full body."""
    params = {"maxResults": max(1, min(int(max_results or 10), 50))}
    if (query or "").strip():
        params["q"] = query.strip()
    listing, err = _request("GET", f"{_GMAIL}/messages", params=params)
    if err:
        return err
    ids = [m["id"] for m in (listing.get("messages") or [])]
    if not ids:
        return "No messages matched that search."

    out = []
    for mid in ids:
        msg, err = _request(
            "GET",
            f"{_GMAIL}/messages/{mid}",
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date"],
            },
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
    msg, err = _request("GET", f"{_GMAIL}/messages/{message_id.strip()}", params={"format": "full"})
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


def _raw_message(to: str, subject: str, body: str, cc: str = "") -> str:
    em = EmailMessage()
    em["To"] = to
    if cc:
        em["Cc"] = cc
    em["Subject"] = subject
    em.set_content(body)
    return base64.urlsafe_b64encode(em.as_bytes()).decode()


@mcp.tool
def gmail_send(to: str, subject: str, body: str, cc: str = "") -> str:
    """Send an email from the user's Gmail account. ``to``/``cc`` accept
    comma-separated addresses. Confirm the content with the user before sending —
    this delivers immediately. Use gmail_draft instead to save without sending."""
    if not (to or "").strip():
        return "A recipient (to) is required."
    payload = {"raw": _raw_message(to.strip(), subject or "", body or "", (cc or "").strip())}
    _, err = _request("POST", f"{_GMAIL}/messages/send", json=payload)
    if err:
        return err
    return f"Sent to {to}."


@mcp.tool
def gmail_draft(to: str, subject: str, body: str, cc: str = "") -> str:
    """Save an email as a draft in the user's Gmail without sending it. Good for
    letting the user review a reply before it goes out."""
    payload = {"message": {"raw": _raw_message((to or "").strip(), subject or "", body or "", (cc or "").strip())}}
    data, err = _request("POST", f"{_GMAIL}/drafts", json=payload)
    if err:
        return err
    return f"Draft saved (id {data.get('id', '?')}). The user can review and send it from Gmail."


@mcp.tool
def gmail_reply(message_id: str, body: str) -> str:
    """Reply to a message, keeping it in the same Gmail thread. Replies to the
    original sender with a "Re:" subject."""
    if not (message_id or "").strip():
        return "message_id is required (get one from gmail_search)."
    original, err = _request(
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
    _, err = _request("POST", f"{_GMAIL}/messages/send", json=payload)
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
    _, err = _request(
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


# ── calendar tools ───────────────────────────────────────────────────────────

@mcp.tool
def calendar_list_events(days_ahead: int = 7, max_results: int = 20) -> str:
    """List the user's upcoming calendar events for the next ``days_ahead`` days
    (default a week), earliest first."""
    now = time.time()
    params = {
        "timeMin": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "timeMax": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(1, int(days_ahead or 7)) * 86400)
        ),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max(1, min(int(max_results or 20), 50)),
    }
    data, err = _request("GET", f"{_CALENDAR}/calendars/primary/events", params=params)
    if err:
        return err
    items = data.get("items") or []
    if not items:
        return f"No events in the next {days_ahead} day(s)."
    lines = []
    for ev in items:
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date", "?")
        who = ", ".join(a.get("email", "") for a in (ev.get("attendees") or [])[:5])
        lines.append(
            f"[{ev.get('id')}] {start} — {ev.get('summary', '(no title)')}"
            + (f"\n  Location: {ev['location']}" if ev.get("location") else "")
            + (f"\n  With: {who}" if who else "")
        )
    return f"{len(lines)} event(s):\n\n" + "\n".join(lines)


@mcp.tool
def calendar_create_event(summary: str, start: str, end: str, description: str = "",
                          location: str = "", attendees: str = "") -> str:
    """Create a calendar event. ``start``/``end`` are RFC3339 timestamps with an
    offset, e.g. "2026-07-28T14:00:00+03:00". ``attendees`` is a comma-separated
    list of email addresses. Confirm details with the user first — attendees are
    emailed an invitation."""
    if not (summary or "").strip() or not (start or "").strip() or not (end or "").strip():
        return "summary, start and end are all required."
    body: dict = {
        "summary": summary.strip(),
        "start": {"dateTime": start.strip()},
        "end": {"dateTime": end.strip()},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    emails = [a.strip() for a in (attendees or "").split(",") if a.strip()]
    if emails:
        body["attendees"] = [{"email": e} for e in emails]
    data, err = _request(
        "POST",
        f"{_CALENDAR}/calendars/primary/events",
        params={"sendUpdates": "all" if emails else "none"},
        json=body,
    )
    if err:
        return err
    return f"Event created: {data.get('htmlLink') or data.get('id', 'ok')}"


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
