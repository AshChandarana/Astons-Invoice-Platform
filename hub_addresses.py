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
BM_CACHE_KEY = "bm_clients_cache_v2"
BM_CACHE_HOURS = 24

# Verified against the live BM API (Jul 2026): addresses are multi-line
# strings, and invoice_address says which one the client's invoices
# should carry ("Registered Address" / "Postal Address" / "Trading
# Address"). BM also stores each client's xero_identifier, so drafts
# match by Xero contact ID first, name second.
BM_ADDRESS_FIELDS = {
    "registered": "registered_address",
    "postal": "postal_address",
    "trading": "trading_address",
}


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


def _split_address(text) -> list:
    """BM addresses are either newline-separated blocks or one
    comma-separated line — normalise to display lines."""
    if not isinstance(text, str) or not text.strip():
        return []
    lines = [l.strip() for l in re.split(r"[\r\n]+", text) if l.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = [p.strip() for p in lines[0].split(",") if p.strip()]
    return lines


def _bm_extract_address(record: dict) -> list:
    """Prefer the address BM says invoices should carry, then fall back
    registered -> postal -> trading."""
    order = ["registered_address", "postal_address", "trading_address"]
    selector = (record.get("invoice_address") or "").lower()
    for label, field in BM_ADDRESS_FIELDS.items():
        if label in selector:
            order.remove(field)
            order.insert(0, field)
            break
    for field in order:
        lines = _split_address(record.get(field))
        if lines:
            return lines
    return []


def _bm_client_index() -> dict:
    """{'by_xero': {xero_contact_id: lines}, 'by_name': {client_key:
    lines}}, cached in the DB for 24h so the full-client-list fetch
    happens at most daily."""
    raw = db.xero_kv_get(BM_CACHE_KEY)
    if raw:
        try:
            cache = json.loads(raw)
            fetched = dt.datetime.fromisoformat(cache["fetched_at"])
            if (dt.datetime.utcnow() - fetched < dt.timedelta(hours=BM_CACHE_HOURS)
                    and "by_xero" in cache["index"]):
                return cache["index"]
        except (ValueError, KeyError, json.JSONDecodeError):
            pass
    if not bm_configured():
        return {}
    try:
        index = {"by_xero": {}, "by_name": {}}
        for record in _bm_fetch_clients():
            lines = _bm_extract_address(record)
            if not lines:
                continue
            xero_id = record.get("xero_identifier")
            if xero_id:
                index["by_xero"][str(xero_id)] = lines
            key = client_key(record.get("name") or "")
            if key:
                index["by_name"][key] = lines
        db.xero_kv_set(BM_CACHE_KEY, json.dumps(
            {"fetched_at": dt.datetime.utcnow().isoformat(), "index": index}))
        return index
    except Exception as exc:
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"BrightManager address lookup failed: {exc}"[:500])
        return {}


# === RESOLUTION ===

def resolve(contact_name: str, xero_contact: dict = None,
            xero_contact_id: str = None) -> dict:
    """Best address for a client. Returns {'lines': [...], 'source': str}
    with lines == [] when nothing is known anywhere. BM matching is by
    Xero contact ID first (BM stores xero_identifier per client), then
    by normalised name."""
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

    bm = _bm_client_index()
    bm_lines = None
    if xero_contact_id:
        bm_lines = bm.get("by_xero", {}).get(str(xero_contact_id))
    if not bm_lines:
        bm_lines = bm.get("by_name", {}).get(key)
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
