"""Shared Google auth for belleq's bundled Google connectors.

Each Google app (Gmail, Calendar, …) is its own connector with its own consent
and its own refresh token, so they each run as a separate stdio MCP server. This
module holds the part they all share: turning the connector's refresh token into
a short-lived access token and making authorized calls.

Credentials come from two places, deliberately:
  • GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — belleq's own OAuth app, set on the
    master (inherited from its environment). Platform-wide, never per-user, and
    never sent to the browser.
  • GOOGLE_REFRESH_TOKEN — this connector's per-user grant, stored encrypted on
    the connector record and overlaid onto the subprocess env.

Depends only on stdlib + httpx (already in the master image).
"""

from __future__ import annotations

import os
import time

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 30.0

# Cached access token: (token, expires_at_epoch). Refreshed on demand.
_access: tuple[str, float] = ("", 0.0)


def access_token() -> tuple[str, str]:
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
        return "", "This connector isn't connected yet — reconnect it in the belleq dashboard."
    if not client_id or not client_secret:
        return "", "Google is not configured on this server (missing client credentials)."

    try:
        r = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return "", f"Could not reach Google to refresh access: {exc}"
    if r.status_code >= 400:
        # invalid_grant = the user revoked access, or the token expired (Google
        # expires refresh tokens after 7 days while the OAuth app is in Testing).
        if "invalid_grant" in r.text:
            return "", (
                "Google access has expired or was revoked. Reconnect this "
                "connector in the belleq dashboard."
            )
        return "", f"Google rejected the token refresh ({r.status_code}): {r.text[:200]}"

    data = r.json()
    token = data.get("access_token", "")
    if not token:
        return "", "Google returned no access token."
    _access = (token, time.time() + float(data.get("expires_in", 3600)))
    return token, ""


def request(method: str, url: str, **kwargs):
    """Authorized Google API call. Returns (json, error_message)."""
    token, err = access_token()
    if err:
        return None, err
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    try:
        r = httpx.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not reach Google: {exc}"
    if r.status_code == 403:
        return None, (
            "Google denied this request — this connector may not have been granted "
            f"that permission. Details: {r.text[:200]}"
        )
    if r.status_code >= 400:
        return None, f"Google API error {r.status_code}: {r.text[:250]}"
    if not r.content:
        return {}, ""
    return r.json(), ""
