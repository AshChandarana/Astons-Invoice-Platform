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
import json
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
    """Non-auth API failure (bad response, retries exhausted). status_code
    carries the HTTP status when there was one (404 = does not exist)."""

    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


calls_made = 0      # accounting-API calls since process start (for cost checks)


class XeroRateLimited(XeroApiError):
    """Xero answered 429. daily=True means this app's daily call allowance
    for the org is used up — nothing to do but wait for it to free up;
    daily=False is the per-minute limit, which passes within a minute."""

    def __init__(self, message: str, daily: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.daily = daily
        self.retry_after = retry_after


DAILY_LIMIT_TEXT = (
    "Xero's daily call allowance for the Invoice Hub is used up, so Xero is "
    "refusing calls until it frees up again (usually early evening)."
)
DAILY_RETRY_FLOOR = 3600              # assume an hour when Xero sends no Retry-After
MAX_MINUTE_WAIT = 90                  # total seconds we will sleep on minute limits
LIMITS_KEY = "xero_limits"            # latest X-DayLimit/X-MinLimit-Remaining seen
RATE_LIMIT_KEY = "xero_rate_limit"    # per-tenant: the last daily 429 + Retry-After


def _retry_after(resp) -> int:
    try:
        return int(resp.headers.get("Retry-After", "0") or 0)
    except (TypeError, ValueError):
        return 0


def _is_daily_limit(resp) -> bool:
    """Xero names the limit hit in X-Rate-Limit-Problem ('minute', 'daily'
    — some docs say 'day' — 'app' = app-wide per-minute, 'concurrent').
    Only the daily one is worth waiting hours for; a Retry-After beyond
    five minutes can only be that one too."""
    problem = (resp.headers.get("X-Rate-Limit-Problem") or "").strip().lower()
    return problem.startswith("da") or _retry_after(resp) > 300


def _rate_limit_key(tenant_id: str) -> str:
    return f"{RATE_LIMIT_KEY}:{tenant_id}"


def _note_limits(resp) -> None:
    """Xero reports the allowance left on every reply; keep the latest so
    the settings tab can show how much room there is."""
    try:
        day = resp.headers.get("X-DayLimit-Remaining")
        minute = resp.headers.get("X-MinLimit-Remaining")
        if day is None and minute is None:
            return
        db.xero_kv_set(LIMITS_KEY, json.dumps({
            "day_remaining": int(day) if day is not None else None,
            "min_remaining": int(minute) if minute is not None else None,
            "at": dt.datetime.utcnow().isoformat(),
        }))
    except Exception:
        pass


def _note_rate_limit(tenant_id: str, resp) -> None:
    """Record a DAILY lock-out for this org (minute limits pass by themselves).
    When Xero gives no Retry-After we assume an hour, but never push an
    existing pause later just because someone pressed 'Sync now' again."""
    if not _is_daily_limit(resp):
        return
    try:
        now = dt.datetime.utcnow()
        retry_after = _retry_after(resp)
        if not retry_after:
            current = rate_limit_status(tenant_id)
            if current:
                return                     # keep the pause we already have
            retry_after = DAILY_RETRY_FLOOR
        db.xero_kv_set(_rate_limit_key(tenant_id), json.dumps({
            "problem": (resp.headers.get("X-Rate-Limit-Problem") or "").strip().lower(),
            "daily": True,
            "retry_after": retry_after,
            "at": now.isoformat(),
        }))
    except Exception:
        pass


def _clear_rate_limit(tenant_id: str) -> None:
    """A successful call proves the lock-out is over (Xero's Retry-After is
    an upper bound), so the banner and the auto-sync pause go away."""
    try:
        db.xero_kv_delete(_rate_limit_key(tenant_id))
    except Exception:
        pass


def rate_limit_status(tenant_id: str = None):
    """The current daily lock-out for a tenant (or, with no tenant, for any
    connected tenant) as {'tenant_id', 'until': iso, 'retry_after', 'at'};
    None once Xero's Retry-After has passed or nothing was recorded."""
    tenants = ([tenant_id] if tenant_id
               else [c["tenant_id"] for c in db.xero_list_connections()])
    for tid in tenants:
        raw = db.xero_kv_get(_rate_limit_key(tid))
        if not raw:
            continue
        try:
            info = json.loads(raw)
            at = dt.datetime.fromisoformat(info["at"])
            until = at + dt.timedelta(seconds=int(info.get("retry_after") or 0))
        except (ValueError, TypeError, KeyError):
            continue
        if dt.datetime.utcnow() >= until:
            continue
        return {"tenant_id": tid, "until": until.isoformat(),
                "retry_after": info.get("retry_after"), "at": info["at"]}
    return None


def limits() -> dict:
    """Latest allowance figures seen, or {} before the first call."""
    raw = db.xero_kv_get(LIMITS_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


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
    global calls_made
    url = f"{API_BASE}/{path.lstrip('/')}"
    attempts_401 = 0
    waited = 0
    for attempt in range(MAX_429_RETRIES + 1):
        calls_made += 1
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

        _note_limits(resp)
        if resp.status_code in (200, 201, 304):
            _clear_rate_limit(tenant_id)
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 304:
            return {}
        if resp.status_code == 401 and attempts_401 == 0:
            attempts_401 += 1
            _refresh_tokens()
            continue
        if resp.status_code == 429:
            _note_rate_limit(tenant_id, resp)
            if _is_daily_limit(resp):
                # Waiting a minute cannot help: fail fast with a plain message
                # instead of hanging the page for five minutes.
                raise XeroRateLimited(DAILY_LIMIT_TEXT, daily=True,
                                      retry_after=_retry_after(resp) or DAILY_RETRY_FLOOR)
            wait = min(_retry_after(resp) or 5, 60)
            if waited + wait > MAX_MINUTE_WAIT:
                break                      # don't hang the page; say so instead
            time.sleep(wait)
            waited += wait
            continue
        raise XeroApiError(
            f"{method} {path} failed ({resp.status_code}): {resp.text[:300]}",
            status_code=resp.status_code,
        )
    raise XeroRateLimited(
        f"{method} {path}: Xero is rate-limiting the Hub (too many calls this "
        "minute). It clears within a minute — try again shortly.",
        daily=False,
    )


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
