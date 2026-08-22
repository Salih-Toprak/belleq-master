"""Google OAuth 2.0 for the bundled Gmail/Calendar connector.

Unlike the generic MCP connector flow (``oauth.py``), Google is not an MCP server:
there is no protected-resource discovery and no dynamic client registration —
belleq registers ONE OAuth app in Google Cloud and every user consents to it. So
all this module provides is the fixed endpoints, the scope set, and the
authorization-URL builder. The code→token exchange itself is standard and reuses
``oauth.exchange_code`` (Google accepts PKCE + a client secret).

Scope note: gmail.readonly and gmail.modify are *restricted* scopes. While the
OAuth app is in Testing they work for listed test users with no verification, but
refresh tokens then expire after 7 days. Publishing to real customers requires
Google verification plus an annual CASA Tier 2 security assessment.
"""

from __future__ import annotations

from urllib.parse import urlencode

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Each Google app is a SEPARATE connector with its own consent, so it asks only
# for its own scopes — connecting Calendar never grants access to mail. Keep each
# list in sync with that server's tools.
SCOPES_BY_PROVIDER: dict[str, list[str]] = {
    "gmail": [
        "https://www.googleapis.com/auth/gmail.readonly",  # search + read (restricted)
        "https://www.googleapis.com/auth/gmail.modify",    # archive/label/draft (restricted)
        "https://www.googleapis.com/auth/gmail.send",      # send + reply (sensitive)
        "openid",
        "email",
    ],
    "google_calendar": [
        "https://www.googleapis.com/auth/calendar.events",  # read + create + delete events
        "openid",
        "email",
    ],
}

# Connector metadata.provider values handled by this module.
PROVIDERS = frozenset(SCOPES_BY_PROVIDER)


def scopes_for(provider: str) -> list[str]:
    return SCOPES_BY_PROVIDER.get(provider, [])


def build_authorization_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str, scopes: list[str]
) -> str:
    """Google consent URL.

    ``access_type=offline`` + ``prompt=consent`` are load-bearing: without them
    Google returns only an access token (or omits the refresh token on repeat
    grants), and the connector would stop working after an hour with no way to
    refresh.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"
