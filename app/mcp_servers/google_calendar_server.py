"""belleq's bundled Google Calendar MCP server (stdio).

A direct connection to the user's Google Calendar — no Zapier/Pipedream in the
middle. Launched as a stdio subprocess by the master's connector aggregator:

    transport = stdio
    command   = python
    args      = ["-m", "app.mcp_servers.google_calendar_server"]
    env       = { GOOGLE_REFRESH_TOKEN: <this connector's grant> }

Gmail is a separate connector (app.mcp_servers.gmail_server) with its own
consent, so connecting Calendar never grants access to the user's mail.

Auth/token handling is shared — see google_common.
"""

from __future__ import annotations

import time

from fastmcp import FastMCP

from app.mcp_servers.google_common import request

mcp = FastMCP("Google Calendar")

_CALENDAR = "https://www.googleapis.com/calendar/v3"


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
    data, err = request("GET", f"{_CALENDAR}/calendars/primary/events", params=params)
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
    data, err = request(
        "POST",
        f"{_CALENDAR}/calendars/primary/events",
        params={"sendUpdates": "all" if emails else "none"},
        json=body,
    )
    if err:
        return err
    return f"Event created: {data.get('htmlLink') or data.get('id', 'ok')}"


@mcp.tool
def calendar_delete_event(event_id: str) -> str:
    """Cancel/delete an event from the user's primary calendar. Get the event_id
    from calendar_list_events. Confirm with the user first — attendees are
    notified that the event was cancelled."""
    if not (event_id or "").strip():
        return "event_id is required (get one from calendar_list_events)."
    _, err = request(
        "DELETE",
        f"{_CALENDAR}/calendars/primary/events/{event_id.strip()}",
        params={"sendUpdates": "all"},
    )
    if err:
        return err
    return "Event deleted."


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
