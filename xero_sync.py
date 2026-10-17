"""
Xero draft poll for Invoice Hub (SPEC.md section 2.1).

For every connected tenant:
  1. Delta fetch: GET /Invoices?Statuses=DRAFT,SUBMITTED&where=Type=="ACCREC"
     with If-Modified-Since (5-minute overlap buffer), paged so line items
     are included. Upsert into xero_drafts keyed on InvoiceID.
  2. Presence check: ask Xero for the Hub's own PENDING_REVIEW drafts by
     InvoiceID (summaryOnly, 40 per call) and read back each one's current
     status. Any that is no longer DRAFT/SUBMITTED (deleted/authorised
     outside the Hub) is marked EXTERNAL_ACTION with its actual Xero
     status — nothing vanishes silently. Asking by ID costs the same
     whether the org holds ten drafts or a thousand (Oct 2026: paging
     every draft in the org cost six calls a sync and blew Xero's daily
     allowance every afternoon).

Runs two ways:
  - In-app: maybe_sync() is called on approver page load, throttled to
    once per POLL_INTERVAL_MINUTES across all sessions (DB-backed), and
    never two at once (a sync marks itself in progress when it starts).
  - Standalone: `python xero_sync.py` for a Railway cron service.
"""

import datetime as dt
import json
import re
from email.utils import format_datetime

import db
import xero_client

POLL_INTERVAL_MINUTES = 15      # BM -> Xero is hourly anyway
DELTA_OVERLAP_MINUTES = 5
PAGE_SIZE = 100
PRESENCE_BATCH = 40             # InvoiceIDs per presence call (URL length)
PRESENCE_STATUSES = "DRAFT,SUBMITTED,AUTHORISED,PAID,VOIDED,DELETED"
KNOWN_STATUSES = set(PRESENCE_STATUSES.split(","))
LIVE_STATUSES = ("DRAFT", "SUBMITTED")
GONE = "GONE"                   # sentinel: Xero answered 404 for this id
SYNC_LOCK_KEY = "sync_started_at"
SYNC_LOCK_MINUTES = 20          # a lock older than this is a crashed sync
DAILY_LIMIT_LOG_GAP_MINUTES = 60   # log the same daily-limit failure at most hourly
MAX_FALLBACK_FETCHES = 10       # per-invoice look-ups per sync (the rest wait a sync)


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
            if is_fee_protection(inv):
                continue          # FPS Hub owns the Fee Protection fee notes
            yield inv
        if len(invoices) < PAGE_SIZE:
            return
        page += 1


def _upsert_invoice(tenant_id: str, inv: dict) -> None:
    contact = inv.get("Contact") or {}
    db.xero_upsert_draft(
        invoice_id=inv["InvoiceID"],
        tenant_id=tenant_id,
        invoice_number=inv.get("InvoiceNumber") or "",
        reference=inv.get("Reference") or "",
        contact_id=contact.get("ContactID") or "",
        contact_name=contact.get("Name", ""),
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


def _presence_statuses(tenant_id: str, invoice_ids: list) -> tuple:
    """Current Xero status of the given invoices, keyed by InvoiceID, asked
    for in batches of PRESENCE_BATCH. Returns (statuses, inconclusive):
    invoices Xero did not return are absent from `statuses`; a whole batch
    that came back EMPTY is treated as inconclusive (Xero ignoring the
    filter, an outage) and its ids go in `inconclusive` so nobody is
    flagged or individually re-fetched on that evidence."""
    statuses = {}
    inconclusive = set()
    ids = list(invoice_ids)
    for start in range(0, len(ids), PRESENCE_BATCH):
        batch = ids[start:start + PRESENCE_BATCH]
        data = xero_client.api_get(
            tenant_id, "Invoices",
            {"IDs": ",".join(batch), "summaryOnly": "true",
             "Statuses": PRESENCE_STATUSES},
        )
        invoices = (data or {}).get("Invoices", [])
        if not invoices:
            # Nothing back for a whole batch: either every one of them is
            # gone, or Xero ignored the filter / is unwell. One cheap probe
            # decides: a definite answer for the first id (a status, or
            # 404) means the empty result is trustworthy and the batch can
            # go through the per-invoice fallback; anything else is
            # inconclusive and the batch is left alone this sync.
            try:
                probe = xero_client.api_get(tenant_id, f"Invoices/{batch[0]}")
                invs = (probe or {}).get("Invoices", [])
                status = invs[0].get("Status") if invs else None
                if status in KNOWN_STATUSES:
                    statuses[batch[0]] = status
                if status in LIVE_STATUSES:
                    # Xero said "nothing" yet the first one is a live draft:
                    # the batch answer was wrong, so trust none of it.
                    inconclusive.update(batch[1:])
            except xero_client.XeroRateLimited:
                raise
            except xero_client.XeroApiError as exc:
                if exc.status_code == 404:
                    statuses[batch[0]] = GONE       # decided; no second fetch
                else:
                    inconclusive.update(batch)
            continue
        for inv in invoices:
            status = inv.get("Status")
            # Only a status we understand is decisive; anything odd is left
            # out so the per-invoice fallback decides (never mass-flag).
            if inv.get("InvoiceID") and status in KNOWN_STATUSES:
                statuses[inv["InvoiceID"]] = status
    return statuses, inconclusive


def _check_disappeared(tenant_id: str, statuses: dict, pending_ids: list,
                       inconclusive: set = frozenset()) -> int:
    """Mark PENDING_REVIEW drafts that are no longer live in Xero as
    EXTERNAL_ACTION, recording what actually happened to them. A draft the
    batch lookup did not return at all is asked about individually before
    it is written off; ids from an inconclusive batch are left alone."""
    flagged = 0
    fallbacks = 0
    for invoice_id in pending_ids:
        if invoice_id in inconclusive:
            continue
        status = statuses.get(invoice_id)
        if status in LIVE_STATUSES:
            continue
        if status == GONE:
            note = "Removed from Xero (invoice no longer retrievable)."
        elif status is None:
            if fallbacks >= MAX_FALLBACK_FETCHES:
                continue               # bounded cost; the rest next sync
            fallbacks += 1
            note = "No longer visible as a draft in Xero."
            try:
                data = xero_client.api_get(tenant_id, f"Invoices/{invoice_id}")
                invs = (data or {}).get("Invoices", [])
                if invs:
                    status = invs[0].get("Status")
                    if status in LIVE_STATUSES or status not in KNOWN_STATUSES:
                        continue          # still a draft, or odd data: leave it
                    note = f"Actioned outside the Hub — Xero status is now {status}."
            except xero_client.XeroRateLimited:
                raise
            except xero_client.XeroApiError as exc:
                if exc.status_code != 404:
                    continue              # Xero unwell, not "gone": next sync
                note = "Removed from Xero (invoice no longer retrievable)."
        else:
            note = f"Actioned outside the Hub — Xero status is now {status}."
        if db.xero_mark_external_action(invoice_id, note):
            flagged += 1           # False = decided in the Hub meanwhile
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
    calls_before = xero_client.calls_made
    try:
        # 1. Delta fetch with full detail (line items need paging).
        #    Track which drafts are NEW to the Hub so the approver can be
        #    emailed about fresh arrivals (one batched email per sync).
        existing_ids = set(db.xero_ids_for_tenant(tenant_id))
        delta_params = {
            "Statuses": "DRAFT,SUBMITTED",
            "where": 'Type=="ACCREC"',
        }
        fetched = 0
        new_drafts = []
        for inv in _fetch_paged(tenant_id, delta_params, if_modified_since=if_mod):
            if inv["InvoiceID"] not in existing_ids:
                new_drafts.append({
                    "invoice_number": inv.get("InvoiceNumber") or "(no number)",
                    "contact": (inv.get("Contact") or {}).get("Name", ""),
                    "total": inv.get("Total"),
                })
            _upsert_invoice(tenant_id, inv)
            fetched += 1

        # 2. Presence check: the Hub's own pending drafts, by ID.
        pending_ids = db.xero_pending_ids_for_tenant(tenant_id)
        statuses, inconclusive = _presence_statuses(tenant_id, pending_ids)
        if inconclusive:
            _log_once_a_day(
                "presence_inconclusive",
                f"Presence check: Xero returned nothing for {len(inconclusive)} "
                "pending draft(s); left unchanged this sync (will retry).",
                tenant_id)
        db.xero_touch_drafts_seen(
            [i for i in pending_ids if statuses.get(i) in LIVE_STATUSES]
        )
        flagged = _check_disappeared(tenant_id, statuses, pending_ids, inconclusive)

        db.xero_set_last_sync(tenant_id, started.isoformat())
        calls = xero_client.calls_made - calls_before
        db.xero_log_sync(tenant_id, ok=True, fetched=fetched,
                         message=f"{calls} Xero call(s)"
                                 + (f", {flagged} external action(s)" if flagged else ""))
        return {"tenant_id": tenant_id, "ok": True,
                "fetched": fetched, "external_actions": flagged,
                "new_drafts": new_drafts, "calls": calls}
    except xero_client.XeroRateLimited as exc:
        if not (exc.daily and _daily_failure_logged_recently(tenant_id)):
            db.xero_log_sync(tenant_id, ok=False, fetched=0, message=str(exc)[:500])
        return {"tenant_id": tenant_id, "ok": False, "error": str(exc),
                "rate_limited": True, "daily": exc.daily}
    except Exception as exc:
        db.xero_log_sync(tenant_id, ok=False, fetched=0, message=str(exc)[:500])
        return {"tenant_id": tenant_id, "ok": False, "error": str(exc)}


def _daily_failure_logged_recently(tenant_id: str) -> bool:
    """Avoid filling the Exceptions tab with one identical daily-limit line
    per forced 'Sync now' while the lock-out lasts (other failure lines in
    between do not reset the clock)."""
    last = db.xero_latest_sync_failure_like("daily call allowance", tenant_id)
    if not last:
        return False
    try:
        age = dt.datetime.utcnow() - dt.datetime.fromisoformat(last["created_at"])
    except (ValueError, TypeError):
        return False
    return age < dt.timedelta(minutes=DAILY_LIMIT_LOG_GAP_MINUTES)


def _log_once_a_day(key: str, message: str, tenant_id=None) -> None:
    """Write a sync-log failure line at most once per UTC day per key+tenant."""
    today = dt.datetime.utcnow().date().isoformat()
    kv_key = f"logged_{key}:{tenant_id or 'all'}"
    if db.xero_kv_get(kv_key) == today:
        return
    db.xero_kv_set(kv_key, today)
    db.xero_log_sync(tenant_id, ok=False, fetched=0, message=message[:500])


IN_PROGRESS = {"ok": True, "skipped": "in_progress"}


def clear_sync_lock() -> None:
    """For process start: no sync can be running in a container that has
    just booted, so a lock left by the previous container is stale."""
    db.xero_kv_delete(SYNC_LOCK_KEY)


def sync_all(force: bool = False) -> list:
    """Poll every connected tenant. Refreshes the connection list first
    so newly-authorised orgs are picked up. Takes an atomic lock and stamps
    last_poll_at at the START, so page loads and worker ticks that arrive
    mid-sync do not start another one; a forced sync that finds the lock
    held returns [IN_PROGRESS] so the screen can say so. A tenant Xero has
    locked out for the day is skipped (its pause is per org)."""
    if not xero_client.is_connected():
        return []
    now = dt.datetime.utcnow()
    token = now.isoformat()
    stale_before = (now - dt.timedelta(minutes=SYNC_LOCK_MINUTES)).isoformat()
    if not db.xero_kv_acquire(SYNC_LOCK_KEY, token, stale_before):
        return [dict(IN_PROGRESS)] if force else []
    db.xero_kv_set("last_poll_at", token)
    try:
        try:
            xero_client.refresh_connections()
        except Exception as exc:
            db.xero_log_sync(None, ok=False, fetched=0,
                             message=f"Connection refresh failed: {exc}"[:500])
        results = []
        for c in db.xero_list_connections():
            paused = xero_client.rate_limit_status(c["tenant_id"])
            if paused and not force:
                results.append({"tenant_id": c["tenant_id"], "ok": True,
                                "skipped": "paused", "until": paused["until"]})
                continue
            results.append(sync_tenant(c["tenant_id"]))
        _notify_new_drafts(results)
        return results
    finally:
        db.xero_kv_release(SYNC_LOCK_KEY, token)


def _notify_new_drafts(results: list) -> None:
    """One batched email to the approver per sync that brought in new
    drafts. Best-effort: a send failure never breaks the sync (it logs
    via send_email's own error handling)."""
    new = [d for r in results if r.get("ok") for d in r.get("new_drafts", [])]
    if not new:
        return
    try:
        import html
        import xero_watchdog
        rows = "".join(
            f"<tr><td>{html.escape(d['invoice_number'])}</td>"
            f"<td>{html.escape(d['contact'])}</td>"
            f"<td align='right'>£{float(d['total'] or 0):,.2f}</td></tr>"
            for d in new
        )
        body = (
            f"<p>{len(new)} new fee note{'s' if len(new) != 1 else ''} "
            f"arrived from BrightManager and "
            f"{'are' if len(new) != 1 else 'is'} awaiting review.</p>"
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse'>"
            "<tr><th>Invoice</th><th>Client</th><th>Gross</th></tr>"
            f"{rows}</table>"
            f"<p><a href='{xero_watchdog._hub_url()}'>Open the review queue</a></p>"
        )
        xero_watchdog.send_email(
            f"Invoice Hub: {len(new)} new fee note"
            f"{'s' if len(new) != 1 else ''} awaiting approval",
            body,
        )
    except Exception as exc:
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"New-draft notification failed: {exc}"[:500])


_FPS_NUMBER = re.compile(r"^\d{4}C?([-.]\d{1,4})?$")   # 1100, 1100C, 9000, 7040-2025, 7043.1


def is_fee_protection(inv: dict) -> bool:
    """Fee Protection Service fee notes are raised and chased by FPS Hub;
    this app must never show, approve or chase them."""
    ref = (inv.get("Reference") or "").strip().upper()
    num = (inv.get("InvoiceNumber") or "").strip().upper()
    return ref.startswith("FPS-") or bool(_FPS_NUMBER.match(num))


def maybe_sync(force: bool = False) -> list:
    """Sync if the poll interval has elapsed (DB-backed throttle so
    multiple Streamlit sessions don't hammer the API), never while another
    sync is running, and not at all while Xero has locked the Hub out for
    the day (unless forced, which fails fast with the plain message).
    Returns [] when skipped."""
    if not xero_client.is_connected():
        return []
    if not force:
        last = db.xero_kv_get("last_poll_at")
        if last:
            elapsed = dt.datetime.utcnow() - dt.datetime.fromisoformat(last)
            if elapsed < dt.timedelta(minutes=POLL_INTERVAL_MINUTES):
                return []
        connections = db.xero_list_connections()
        if connections and all(xero_client.rate_limit_status(c["tenant_id"])
                               for c in connections):
            return []          # every org is locked out: nothing to ask yet
    return sync_all(force=force)


if __name__ == "__main__":
    db.init_db()
    for result in sync_all():
        print(result)
