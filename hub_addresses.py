"""
Client address resolution for the branded fee note.

BrightManager pushes invoices into Xero without populating the contact's
postal address, so the "INVOICED TO" block can come up empty. Resolution
order:
  1. Hub address book (client_addresses) — sticky once known
  2. The Xero contact's POBOX/STREET address (when actually filled in)
  3. The BrightManager API's client record (read-only key, same
     BRIGHTMANAGER_API_KEY env var as the onboarding app)
  4. Nothing -> the review screens warn and offer a manual box; a manual
     save lands in the address book and is remembered for that client.

Successful Xero/BM lookups are cached into the address book too, so each
client is resolved at most once.
"""

import datetime as dt
import json
import os
import re

import requests

import db
import xero_pdf

BM_BASE = "https://manager.brightsg.com/api/v2"
BM_CACHE_KEY = "bm_clients_cache"
BM_CACHE_HOURS = 24

# Likely BM address field spellings, tried in order per line slot.
BM_ADDRESS_KEYS = [
    ("address_line_1", "address_line1", "address1", "address_1", "address", "street"),
    ("address_line_2", "address_line2", "address2", "address_2"),
    ("address_line_3", "address_line3", "address3", "address_3"),
    ("town", "city"),
    ("county", "region", "state"),
    ("postcode", "post_code", "postal_code", "zip"),
]


def client_key(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    text = re.sub(r"\b(ltd|limited|llp)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


# === BRIGHTMANAGER ===

def bm_configured() -> bool:
    return bool(os.environ.get("BRIGHTMANAGER_API_KEY", "").strip())


def _bm_fetch_clients() -> list:
    headers = {"X-API-KEY": os.environ["BRIGHTMANAGER_API_KEY"].strip(),
               "Accept": "application/json"}
    clients, page = [], 1
    while True:
        resp = requests.get(f"{BM_BASE}/clients", headers=headers,
                            params={"page": page}, timeout=60)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        batch = data.get("clients", [])
        clients.extend(batch)
        total = data.get("count", len(clients))
        if not batch or len(clients) >= total:
            return clients
        page += 1


def _bm_extract_address(record: dict) -> list:
    lines = []
    lowered = {k.lower(): v for k, v in record.items() if v}
    for slot in BM_ADDRESS_KEYS:
        for key in slot:
            value = lowered.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(value.strip())
                break
    return lines


def _bm_client_index() -> dict:
    """client_key -> address lines, cached in the DB for 24h so the
    full-client-list fetch happens at most daily."""
    raw = db.xero_kv_get(BM_CACHE_KEY)
    if raw:
        try:
            cache = json.loads(raw)
            fetched = dt.datetime.fromisoformat(cache["fetched_at"])
            if dt.datetime.utcnow() - fetched < dt.timedelta(hours=BM_CACHE_HOURS):
                return cache["index"]
        except (ValueError, KeyError, json.JSONDecodeError):
            pass
    if not bm_configured():
        return {}
    try:
        index = {}
        for record in _bm_fetch_clients():
            key = client_key(record.get("name") or "")
            if not key:
                continue
            lines = _bm_extract_address(record)
            if lines:
                index[key] = lines
        db.xero_kv_set(BM_CACHE_KEY, json.dumps(
            {"fetched_at": dt.datetime.utcnow().isoformat(), "index": index}))
        return index
    except Exception as exc:
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"BrightManager address lookup failed: {exc}"[:500])
        return {}


# === RESOLUTION ===

def resolve(contact_name: str, xero_contact: dict = None) -> dict:
    """Best address for a client. Returns {'lines': [...], 'source': str}
    with lines == [] when nothing is known anywhere."""
    key = client_key(contact_name)
    if not key:
        return {"lines": [], "source": None}

    saved = db.client_address_get(key)
    if saved:
        try:
            return {"lines": json.loads(saved["address_json"]),
                    "source": saved["source"]}
        except json.JSONDecodeError:
            pass

    lines = xero_pdf.contact_address_lines(xero_contact or {})
    if lines:
        db.client_address_set(key, lines, "xero")
        return {"lines": lines, "source": "xero"}

    bm_lines = _bm_client_index().get(key)
    if bm_lines:
        db.client_address_set(key, bm_lines, "bm")
        return {"lines": bm_lines, "source": "bm"}

    return {"lines": [], "source": None}


def save_manual(contact_name: str, text: str) -> list:
    """Save a manually-entered address (one line per row) to the book."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    key = client_key(contact_name)
    if key and lines:
        db.client_address_set(key, lines, "manual")
    return lines
