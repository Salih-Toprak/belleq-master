"""OAuth 2.0 (Authorization Code + PKCE + Dynamic Client Registration) for
remote MCP connectors, following the MCP Authorization spec.

Flow, split across two dashboard requests so the *end user's* browser does the
login while the master holds the tokens:

  1. authorize(): probe the MCP URL -> discover the protected-resource metadata
     (RFC 9728) -> the authorization server metadata (RFC 8414) -> dynamically
     register a client (RFC 7591) -> build the authorization URL with PKCE.
  2. user logs in on the provider, which redirects back to the dashboard with
     ?code&state; the dashboard relays it to exchange().
  3. exchange(): swap the code for tokens (PKCE verifier) and store them.

Everything talks raw HTTP via httpx so the split-request flow is explicit.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

CLIENT_NAME = "Belleq"
_TIMEOUT = 15.0


class OAuthError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- PKCE -------------------------------------------------------------


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# --- Discovery --------------------------------------------------------


def _parse_resource_metadata_url(www_authenticate: str) -> str | None:
    """Pull resource_metadata="..." out of a WWW-Authenticate header."""
    for part in www_authenticate.split(","):
        part = part.strip()
        if part.lower().startswith("resource_metadata"):
            _, _, val = part.partition("=")
            return val.strip().strip('"')
    return None


async def _probe_for_auth(client: httpx.AsyncClient, mcp_url: str) -> str | None:
    """Hit the MCP URL; if it requires auth, return the resource-metadata URL.

    Returns None when the server responds without demanding auth (no OAuth
    needed). Raising is reserved for hard failures.
    """
    try:
        # A minimal MCP initialize triggers the 401 + WWW-Authenticate.
        r = await client.post(
            mcp_url,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": "0.1"},
                },
            },
        )
    except httpx.HTTPError as e:
        raise OAuthError(f"Could not reach {mcp_url}: {e}", status_code=502) from e

    if r.status_code != 401:
        return None
    return _parse_resource_metadata_url(r.headers.get("WWW-Authenticate", "")) or "fallback"


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError:
        return None
    return None


async def discover(mcp_url: str) -> dict:
    """Resolve the OAuth endpoints for an MCP server.

    Returns {"auth_required": False} when no auth is needed, or a dict with
    authorization_endpoint / token_endpoint / registration_endpoint /
    scopes_supported / resource when it is.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        rm_url = await _probe_for_auth(client, mcp_url)
        if rm_url is None:
            return {"auth_required": False}

        origin = _origin(mcp_url)
        resource = mcp_url
        as_urls: list[str] = []

        # Protected Resource Metadata (RFC 9728).
        if rm_url and rm_url != "fallback":
            prm = await _fetch_json(client, rm_url)
            if prm:
                resource = prm.get("resource", mcp_url)
                as_urls = list(prm.get("authorization_servers", []) or [])
        if not as_urls:
            # Fallback: probe well-known PRM, then assume the origin is the AS.
            prm = await _fetch_json(
                client, f"{origin}/.well-known/oauth-protected-resource"
            )
            if prm and prm.get("authorization_servers"):
                as_urls = list(prm["authorization_servers"])
                resource = prm.get("resource", mcp_url)
            else:
                as_urls = [origin]

        # Authorization Server Metadata (RFC 8414) — try both well-known paths.
        meta: dict | None = None
        for as_url in as_urls:
            as_origin = as_url.rstrip("/")
            for path in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ):
                meta = await _fetch_json(client, f"{as_origin}{path}")
                if meta and meta.get("authorization_endpoint"):
                    break
            if meta and meta.get("authorization_endpoint"):
                break

        if not meta or not meta.get("authorization_endpoint"):
            raise OAuthError(
                "Could not discover the authorization server for this MCP "
                "server. It may not support OAuth, or uses a non-standard flow.",
                status_code=422,
            )

        return {
            "auth_required": True,
            "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta.get("token_endpoint"),
            "registration_endpoint": meta.get("registration_endpoint"),
            "scopes_supported": meta.get("scopes_supported", []),
            "resource": resource,
        }


async def register_client(registration_endpoint: str, redirect_uri: str) -> dict:
    """Dynamic Client Registration (RFC 7591). Returns {client_id, client_secret?}."""
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client + PKCE
        "application_type": "web",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.post(registration_endpoint, json=body)
        except httpx.HTTPError as e:
            raise OAuthError(f"Client registration failed: {e}", status_code=502) from e
    if r.status_code not in (200, 201):
        raise OAuthError(
            f"Client registration rejected ({r.status_code}): {r.text[:300]}",
            status_code=422,
        )
    data = r.json()
    client_id = data.get("client_id")
    if not client_id:
        raise OAuthError("Registration response missing client_id", status_code=422)
    return {"client_id": client_id, "client_secret": data.get("client_secret", "")}


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: list[str],
    resource: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if resource:
        params["resource"] = resource  # RFC 8707 resource indicator
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


async def exchange_code(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str = "",
) -> dict:
    """Exchange an authorization code for tokens. Returns the token dict."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if resource:
        data["resource"] = resource
    return await _token_request(token_endpoint, data)


async def refresh_tokens(
    *, token_endpoint: str, client_id: str, client_secret: str, refresh_token: str
) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    return await _token_request(token_endpoint, data)


async def _token_request(token_endpoint: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.post(
                token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise OAuthError(f"Token request failed: {e}", status_code=502) from e
    if r.status_code != 200:
        raise OAuthError(
            f"Token endpoint rejected the request ({r.status_code}): {r.text[:300]}",
            status_code=422,
        )
    tok = r.json()
    expires_in = tok.get("expires_in")
    return {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": (time.time() + float(expires_in)) if expires_in else 0.0,
        "scope": tok.get("scope", ""),
    }
