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
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests

import db

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

# Scopes per SPEC.md section 2. The Xero app was created after 2 March
# 2026, so it only has the new granular scopes: accounting.invoices is
# the granular replacement for the broad accounting.transactions.
SCOPES = "offline_access accounting.invoices accounting.attachments accounting.contacts.read"

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

STATE_MAX_AGE_SECONDS = 15 * 60


def _sign_state(timestamp: str) -> str:
    """HMAC the timestamp with the client secret. Signed state means the
    OAuth callback can be validated and completed before anyone logs in
    (the redirect back from Xero always starts a fresh session), while
    still proving the flow originated from this app's config."""
    digest = hmac.new(
        client_secret().encode("utf-8"),
        f"xero-connect:{timestamp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def _validate_state(state: str) -> bool:
    try:
        timestamp, signature = state.split(".", 1)
        age = time.time() - int(timestamp)
    except (ValueError, TypeError):
        return False
    if age < 0 or age > STATE_MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(signature, _sign_state(timestamp))


def build_consent_url() -> str:
    timestamp = str(int(time.time()))
    state = f"{timestamp}.{_sign_state(timestamp)}"
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
    if not _validate_state(state):
        raise XeroAuthError(
            "OAuth state invalid or expired — start the connection again "
            "from Xero settings."
        )

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

def _request(method: str, tenant_id: str, path: str, *, params: dict = None,
             json_payload: dict = None, data: bytes = None,
             content_type: str = None, if_modified_since: str = None) -> dict:
    """One accounting-API call for one tenant, with 429 back-off and one
    automatic re-auth attempt on 401. 304 Not Modified returns {}."""
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
        if content_type:
            headers["Content-Type"] = content_type
        resp = requests.request(
            method, url, headers=headers, params=params or {},
            json=json_payload, data=data, timeout=60,
        )

        if resp.status_code in (200, 201):
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
            f"{method} {path} failed ({resp.status_code}): {resp.text[:300]}"
        )
    raise XeroApiError(f"{method} {path}: rate-limit retries exhausted (429).")


def api_get(tenant_id: str, path: str, params: dict = None,
            if_modified_since: str = None) -> dict:
    return _request("GET", tenant_id, path, params=params,
                    if_modified_since=if_modified_since)


def api_post(tenant_id: str, path: str, payload: dict) -> dict:
    return _request("POST", tenant_id, path, json_payload=payload)


def api_put_attachment(tenant_id: str, invoice_id: str, filename: str,
                       pdf_bytes: bytes, include_online: bool = True) -> dict:
    """Upload a PDF attachment to an invoice. IncludeOnline=true makes it
    visible on the client's online invoice link and Xero emails (SPEC 2.2)."""
    return _request(
        "PUT", tenant_id,
        f"Invoices/{invoice_id}/Attachments/{filename}",
        params={"IncludeOnline": "true"} if include_online else None,
        data=pdf_bytes,
        content_type="application/pdf",
    )
