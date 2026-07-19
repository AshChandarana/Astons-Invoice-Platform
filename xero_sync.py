"""
Xero draft poll for Invoice Hub (SPEC.md section 2.1).

For every connected tenant:
  1. Delta fetch: GET /Invoices?Statuses=DRAFT,SUBMITTED&where=Type=="ACCREC"
     with If-Modified-Since (5-minute overlap buffer), paged so line items
     are included. Upsert into xero_drafts keyed on InvoiceID.
  2. Presence check: lightweight summaryOnly fetch of all live
     DRAFT/SUBMITTED invoice IDs. Any Hub draft still PENDING_REVIEW that
     has disappeared from Xero (deleted/authorised outside the Hub) is
     marked EXTERNAL_ACTION with its actual Xero status — nothing
     vanishes silently.

Runs two ways:
  - In-app: maybe_sync() is called on approver page load, throttled to
    once per POLL_INTERVAL_MINUTES across all sessions (DB-backed).
  - Standalone: `python xero_sync.py` for a Railway cron service.
"""

import datetime as dt
import json
from email.utils import format_datetime

import db
import xero_client

POLL_INTERVAL_MINUTES = 10
DELTA_OVERLAP_MINUTES = 5
PAGE_SIZE = 100


def _http_date(when: dt.datetime) -> str:
    return format_datetime(when.replace(tzinfo=dt.timezone.utc), usegmt=True)


def _parse_dotnet_date(value):
    """Xero JSON returns some dates as /Date(1518685950940+0000)/."""
    if not value:
        return None
    if isinstance(value, str) and "/Date(" in value:
        digits = "".join(
            ch for ch in value.split("(")[1].split("+")[0].split("-")[0]
            if ch.isdigit()
        )
        if digits:
            return dt.datetime.utcfromtimestamp(int(digits) / 1000).isoformat()
        return None
    return value


def _invoice_date(inv: dict, string_key: str, dotnet_key: str):
    return inv.get(string_key) or _parse_dotnet_date(inv.get(dotnet_key))


def _fetch_paged(tenant_id: str, params: dict, if_modified_since=None):
    """Yield invoices across all pages. Paging is required for Xero to
    include line items in the response."""
    page = 1
    while True:
        page_params = dict(params, page=page)
        data = xero_client.api_get(
            tenant_id, "Invoices", page_params, if_modified_since=if_modified_since
        )
        invoices = (data or {}).get("Invoices", [])
        if not invoices:
            return
        for inv in invoices:
            yield inv
        if len(invoices) < PAGE_SIZE:
            return
        page += 1


def _upsert_invoice(tenant_id: str, inv: dict) -> None:
    contact = (inv.get("Contact") or {}).get("Name", "")
    db.xero_upsert_draft(
        invoice_id=inv["InvoiceID"],
        tenant_id=tenant_id,
        invoice_number=inv.get("InvoiceNumber") or "",
        reference=inv.get("Reference") or "",
        contact_name=contact,
        line_items_json=json.dumps(inv.get("LineItems", [])),
        sub_total=inv.get("SubTotal"),
        total_tax=inv.get("TotalTax"),
        total=inv.get("Total"),
        date=_invoice_date(inv, "DateString", "Date"),
        due_date=_invoice_date(inv, "DueDateString", "DueDate"),
        updated_date_utc=_parse_dotnet_date(inv.get("UpdatedDateUTC")),
        branding_theme_id=inv.get("BrandingThemeID") or "",
        xero_status=inv.get("Status") or "",
    )


def _check_disappeared(tenant_id: str, live_ids: set) -> int:
    """Mark PENDING_REVIEW drafts that are no longer live in Xero as
    EXTERNAL_ACTION, recording what actually happened to them."""
    flagged = 0
    for invoice_id in db.xero_pending_ids_for_tenant(tenant_id):
        if invoice_id in live_ids:
            continue
        note = "No longer visible as a draft in Xero."
        try:
            data = xero_client.api_get(tenant_id, f"Invoices/{invoice_id}")
            invs = (data or {}).get("Invoices", [])
            if invs:
                status = invs[0].get("Status", "UNKNOWN")
                note = f"Actioned outside the Hub — Xero status is now {status}."
        except xero_client.XeroApiError:
            note = "Removed from Xero (invoice no longer retrievable)."
        db.xero_mark_external_action(invoice_id, note)
        flagged += 1
    return flagged


def sync_tenant(tenant_id: str) -> dict:
    """Run one poll for one tenant. Returns a summary dict. Failures are
    logged to xero_sync_log (surfaced in the exceptions tab) and re-raised
    only as part of the summary — never silent."""
    conn_row = next(
        (c for c in db.xero_list_connections() if c["tenant_id"] == tenant_id), None
    )
    if_mod = None
    if conn_row and conn_row.get("last_sync_at"):
        last = dt.datetime.fromisoformat(conn_row["last_sync_at"])
        if_mod = _http_date(last - dt.timedelta(minutes=DELTA_OVERLAP_MINUTES))

    started = dt.datetime.utcnow()
    try:
        # 1. Delta fetch with full detail (line items need paging).
        delta_params = {
            "Statuses": "DRAFT,SUBMITTED",
            "where": 'Type=="ACCREC"',
        }
        fetched = 0
        for inv in _fetch_paged(tenant_id, delta_params, if_modified_since=if_mod):
            _upsert_invoice(tenant_id, inv)
            fetched += 1

        # 2. Presence check: all currently-live DRAFT/SUBMITTED IDs.
        #    summaryOnly is lightweight; it does not support `where`, so
        #    this is a superset (may include ACCPAY) — safe for presence.
        live_ids = set()
        page = 1
        while True:
            data = xero_client.api_get(
                tenant_id,
                "Invoices",
                {"Statuses": "DRAFT,SUBMITTED", "summaryOnly": "true", "page": page},
            )
            invoices = (data or {}).get("Invoices", [])
            live_ids.update(i["InvoiceID"] for i in invoices)
            if len(invoices) < PAGE_SIZE:
                break
            page += 1

        db.xero_touch_drafts_seen(
            [i for i in db.xero_pending_ids_for_tenant(tenant_id) if i in live_ids]
        )
        flagged = _check_disappeared(tenant_id, live_ids)

        db.xero_set_last_sync(tenant_id, started.isoformat())
        db.xero_log_sync(tenant_id, ok=True, fetched=fetched,
                         message=f"{flagged} external action(s)" if flagged else None)
        return {"tenant_id": tenant_id, "ok": True,
                "fetched": fetched, "external_actions": flagged}
    except Exception as exc:
        db.xero_log_sync(tenant_id, ok=False, fetched=0, message=str(exc)[:500])
        return {"tenant_id": tenant_id, "ok": False, "error": str(exc)}


def sync_all() -> list:
    """Poll every connected tenant. Refreshes the connection list first
    so newly-authorised orgs are picked up."""
    if not xero_client.is_connected():
        return []
    try:
        xero_client.refresh_connections()
    except Exception as exc:
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"Connection refresh failed: {exc}"[:500])
    results = [sync_tenant(c["tenant_id"]) for c in db.xero_list_connections()]
    db.xero_kv_set("last_poll_at", dt.datetime.utcnow().isoformat())
    return results


def maybe_sync(force: bool = False) -> list:
    """Sync if the poll interval has elapsed (DB-backed throttle so
    multiple Streamlit sessions don't hammer the API). Returns [] when
    skipped."""
    if not xero_client.is_connected():
        return []
    if not force:
        last = db.xero_kv_get("last_poll_at")
        if last:
            elapsed = dt.datetime.utcnow() - dt.datetime.fromisoformat(last)
            if elapsed < dt.timedelta(minutes=POLL_INTERVAL_MINUTES):
                return []
    return sync_all()


if __name__ == "__main__":
    db.init_db()
    for result in sync_all():
        print(result)
