"""
Xero API client for Invoice Hub (Phase 1).

OAuth 2.0 authorisation-code flow with rotating refresh tokens, plus the
minimal API helpers the draft poll needs. One Xero app / one token set
covers every connected organisation (tenant), so this works unchanged
whether AA and CW are one Xero org or two.

Configuration (environment variables):
  XERO_CLIENT_ID      - from the Xero developer app
  XERO_CLIENT_SECRET  - from the Xero developer app
  XERO_REDIRECT_URI   - must exactly match the redirect URI registered on
                        the Xero app, e.g. https://<railway-app-url>/

Tokens are stored in the Hub database (xero_tokens table) so they survive
restarts. Refresh tokens rotate on every use — the new one is persisted
immediately after each refresh.
"""

import base64
import datetime as dt
import os
import secrets
import time
from urllib.parse import urlencode

import requests

import db

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

# Scopes per SPEC.md section 2
SCOPES = "offline_access accounting.transactions accounting.attachments accounting.contacts.read"

MAX_429_RETRIES = 4


class XeroNotConnected(Exception):
    """No stored tokens — the consent flow has not been completed."""


class XeroAuthError(Exception):
    """Token exchange / refresh failed. Usually means reconnect needed."""


class XeroApiError(Exception):
    """Non-auth API failure (bad response, retries exhausted)."""


# === CONFIG ===

def client_id() -> str:
    return os.environ.get("XERO_CLIENT_ID", "").strip()


def client_secret() -> str:
    return os.environ.get("XERO_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    return os.environ.get("XERO_REDIRECT_URI", "").strip()


def is_configured() -> bool:
    return bool(client_id() and client_secret() and redirect_uri())


def is_connected() -> bool:
    return db.xero_get_tokens() is not None


# === OAUTH FLOW ===

def build_consent_url() -> str:
    """Build the Xero consent URL. The state value is stored in the DB
    (not session state) because the round-trip to Xero starts a fresh
    Streamlit session."""
    state = secrets.token_urlsafe(24)
    db.xero_kv_set("oauth_state", state)
    db.xero_kv_set(
        "oauth_state_expires",
        (dt.datetime.utcnow() + dt.timedelta(minutes=15)).isoformat(),
    )
    params = {
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth_header() -> dict:
    raw = f"{client_id()}:{client_secret()}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _store_token_response(payload: dict) -> None:
    expires_at = dt.datetime.utcnow() + dt.timedelta(
        seconds=int(payload.get("expires_in", 1800))
    )
    db.xero_save_tokens(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=expires_at.isoformat(),
    )


def exchange_code(code: str, state: str) -> None:
    """Complete the consent flow: swap the auth code for tokens and
    record the connected tenants."""
    saved_state = db.xero_kv_get("oauth_state")
    expires = db.xero_kv_get("oauth_state_expires")
    db.xero_kv_delete("oauth_state")
    db.xero_kv_delete("oauth_state_expires")

    if not saved_state or state != saved_state:
        raise XeroAuthError(
            "OAuth state mismatch — the connect link may have expired. "
            "Start the connection again from Xero settings."
        )
    if expires and dt.datetime.utcnow().isoformat() > expires:
        raise XeroAuthError("Connect link expired — start the connection again.")

    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise XeroAuthError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:300]}"
        )
    _store_token_response(resp.json())
    refresh_connections()


def _refresh_tokens() -> dict:
    tokens = db.xero_get_tokens()
    if not tokens:
        raise XeroNotConnected("Xero is not connected yet.")
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise XeroAuthError(
            f"Token refresh failed ({resp.status_code}): {resp.text[:300]}. "
            "Reconnect from Xero settings."
        )
    _store_token_response(resp.json())
    return db.xero_get_tokens()


def get_access_token() -> str:
    """Return a valid access token, refreshing if it expires within 60s."""
    tokens = db.xero_get_tokens()
    if not tokens:
        raise XeroNotConnected("Xero is not connected yet.")
    expires_at = dt.datetime.fromisoformat(tokens["expires_at"])
    if expires_at - dt.datetime.utcnow() < dt.timedelta(seconds=60):
        tokens = _refresh_tokens()
    return tokens["access_token"]


def disconnect() -> None:
    """Forget stored tokens. (Connections can also be revoked from the
    user's Xero account settings.)"""
    db.xero_delete_tokens()


# === CONNECTIONS (TENANTS) ===

def refresh_connections() -> list:
    """Fetch the org connections for this token and upsert them into the
    Hub DB. Returns the list of connections."""
    token = get_access_token()
    resp = requests.get(
        CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise XeroApiError(
            f"Fetching connections failed ({resp.status_code}): {resp.text[:300]}"
        )
    conns = resp.json()
    for c in conns:
        if c.get("tenantType") == "ORGANISATION":
            db.xero_upsert_connection(c["tenantId"], c.get("tenantName") or "")
    return conns


# === API CALLS ===

def api_get(tenant_id: str, path: str, params: dict = None,
            if_modified_since: str = None) -> dict:
    """GET against the accounting API for one tenant, with 429 back-off
    and one automatic re-auth attempt on 401.

    if_modified_since: HTTP-date string (RFC 7231) or None.
    Returns parsed JSON. 304 Not Modified returns {} (no changes).
    """
    url = f"{API_BASE}/{path.lstrip('/')}"
    attempts_401 = 0
    for attempt in range(MAX_429_RETRIES + 1):
        headers = {
            "Authorization": f"Bearer {get_access_token()}",
            "Xero-tenant-id": tenant_id,
            "Accept": "application/json",
        }
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
        resp = requests.get(url, headers=headers, params=params or {}, timeout=60)

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 304:
            return {}
        if resp.status_code == 401 and attempts_401 == 0:
            attempts_401 += 1
            _refresh_tokens()
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            time.sleep(min(wait, 60))
            continue
        raise XeroApiError(
            f"GET {path} failed ({resp.status_code}): {resp.text[:300]}"
        )
    raise XeroApiError(f"GET {path}: rate-limit retries exhausted (429).")
