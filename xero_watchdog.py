"""
Orphaned-draft watchdog + weekly digest (SPEC 2.4).

Sweeps the Hub's pending Xero drafts:
  - >= 3 days with no decision -> amber, daily alert email to Ash
  - >= 7 days                  -> red escalation flag
Weekly digest (Mondays): pending count, average time-to-approval over the
last 30 days, oldest outstanding draft.

Alert emails go via Microsoft Graph using the same app-only credentials
pattern (and env var names) as the client-onboarding app:
  MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, MS_SENDER_EMAIL
  WATCHDOG_ALERT_EMAIL (defaults to ash@astonsaccountants.co.uk)

If email isn't configured the sweep still runs — flags show in the Hub
and the would-be email is stored for in-app viewing. Send failures are
logged to xero_sync_log so they surface in the Exceptions tab.

Runs two ways: throttled on approver page load (once per UK day / ISO
week), or standalone via `python xero_watchdog.py` for a Railway cron.
"""

import datetime as dt
import html
import os
from zoneinfo import ZoneInfo

import requests

import db

UK_TZ = ZoneInfo("Europe/London")
WARN_DAYS = 3
ESCALATE_DAYS = 7

GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SENDMAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"


# === EMAIL (Microsoft Graph, app-only) ===

def email_configured() -> bool:
    return all(os.environ.get(k, "").strip() for k in
               ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID", "MS_SENDER_EMAIL"))


def alert_recipient() -> str:
    return os.environ.get("WATCHDOG_ALERT_EMAIL", "ash@astonsaccountants.co.uk").strip()


def _graph_token() -> str:
    resp = requests.post(
        GRAPH_TOKEN_URL.format(tenant=os.environ["MS_TENANT_ID"].strip()),
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["MS_CLIENT_ID"].strip(),
            "client_secret": os.environ["MS_CLIENT_SECRET"].strip(),
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_email(subject: str, html_body: str, to: list = None) -> bool:
    """Send via Graph to `to` (defaults to the watchdog alert recipient).
    Returns True on success; failures are logged to the sync log (never
    silent) and False is returned."""
    if not email_configured():
        db.xero_kv_set("watchdog_email_status",
                       "Email not configured — MS_CLIENT_ID/SECRET/TENANT_ID/SENDER_EMAIL missing.")
        return False
    recipients = [a for a in (to or [alert_recipient()]) if a and a.strip()]
    if not recipients:
        return False
    try:
        token = _graph_token()
        resp = requests.post(
            GRAPH_SENDMAIL_URL.format(sender=os.environ["MS_SENDER_EMAIL"].strip()),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [
                        {"emailAddress": {"address": a.strip()}} for a in recipients
                    ],
                },
                "saveToSentItems": "true",
            },
            timeout=30,
        )
        if resp.status_code == 202:
            db.xero_kv_set("watchdog_email_status", "ok")
            return True
        raise RuntimeError(f"sendMail returned {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"Watchdog email failed: {exc}"[:500])
        db.xero_kv_set("watchdog_email_status", f"Last send failed: {exc}"[:300])
        return False


def notify_prepped(draft: dict, prepped_by: str) -> bool:
    """Email the approver when a team member sends a fee note for
    approval — the moment it actually needs Ash's attention."""
    import html
    total = f"£{float(draft.get('total') or 0):,.2f}"
    body = (
        f"<p><b>{html.escape(prepped_by)}</b> has prepared fee note "
        f"<b>{html.escape(draft.get('invoice_number') or '(no number)')}</b> "
        f"for <b>{html.escape(draft.get('contact_name') or '')}</b> "
        f"({total}) — it is ready for your approval.</p>"
        f"<p><a href='{_hub_url()}'>Open the approvals queue</a></p>"
    )
    return send_email(
        f"Fee note {draft.get('invoice_number') or ''} ready for approval "
        f"({html.escape(draft.get('contact_name') or '')})",
        body,
    )


# === SWEEP ===

def draft_age_days(draft: dict) -> int:
    try:
        first_seen = dt.datetime.fromisoformat(draft["first_seen_at"])
    except (ValueError, TypeError, KeyError):
        return 0
    return max(0, (dt.datetime.utcnow() - first_seen).days)


def overdue_report() -> dict:
    """Pending drafts grouped by severity. Used by the sweep AND the UI
    (queue badges / sidebar / exceptions), so flags always match."""
    pending = db.xero_list_drafts("PENDING_REVIEW", limit=1000)
    warn, escalate = [], []
    for d in pending:
        age = draft_age_days(d)
        d["age_days"] = age
        if age >= ESCALATE_DAYS:
            escalate.append(d)
        elif age >= WARN_DAYS:
            warn.append(d)
    return {"pending": pending, "warn": warn, "escalate": escalate}


def _fmt_money(value) -> str:
    try:
        return f"£{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return ""


def _drafts_table(drafts: list) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(d.get('invoice_number') or '(no number)')}</td>"
        f"<td>{html.escape(d.get('contact_name') or '')}</td>"
        f"<td align='right'>{_fmt_money(d.get('total'))}</td>"
        f"<td align='right'>{d['age_days']} days</td></tr>"
        for d in drafts
    )
    return (
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse'>"
        "<tr><th>Invoice</th><th>Client</th><th>Gross</th><th>Waiting</th></tr>"
        f"{rows}</table>"
    )


def _hub_url() -> str:
    return os.environ.get("XERO_REDIRECT_URI",
                          "https://astons-invoice-platform-production.up.railway.app/").strip()


def run_daily_sweep(force: bool = False) -> dict:
    """Once per UK calendar day: alert on 3-day-old undecided drafts,
    escalation flags at 7 days. (Raiser notification lands with Phase 5
    attribution — alerts go to Ash for now.)"""
    today = dt.datetime.now(UK_TZ).date().isoformat()
    if not force and db.xero_kv_get("watchdog_last_daily") == today:
        return {"ran": False}

    report = overdue_report()
    overdue = report["escalate"] + report["warn"]
    result = {"ran": True, "overdue": len(overdue),
              "escalated": len(report["escalate"]), "emailed": False}

    if overdue:
        subject = (
            f"Invoice Hub: {len(overdue)} draft"
            f"{'s' if len(overdue) != 1 else ''} awaiting review "
            f"({len(report['escalate'])} escalated)"
        )
        body = ""
        if report["escalate"]:
            body += (f"<h3 style='color:#b00020'>Escalated — waiting "
                     f"{ESCALATE_DAYS}+ days</h3>" + _drafts_table(report["escalate"]))
        if report["warn"]:
            body += (f"<h3 style='color:#b58900'>Waiting {WARN_DAYS}+ days</h3>"
                     + _drafts_table(report["warn"]))
        body += f"<p><a href='{_hub_url()}'>Open the Invoice Hub review queue</a></p>"
        db.xero_kv_set("watchdog_last_alert_html", body)
        result["emailed"] = send_email(subject, body)

    db.xero_kv_set("watchdog_last_daily", today)
    db.record_xero_event(
        None, "watchdog_daily",
        f"overdue={len(overdue)} escalated={len(report['escalate'])} "
        f"emailed={result['emailed']}",
    )
    return result


def approval_stats(days: int = 30) -> dict:
    """Average time-to-approval and volume over the trailing window."""
    since = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()
    actioned = [a for a in db.xero_recent_actioned(limit=500)
                if a.get("decided_at") and a["decided_at"] >= since]
    approved = [a for a in actioned if a["hub_status"].startswith("APPROVED")]
    hours = []
    for a in approved:
        full = db.xero_get_draft(a["invoice_id"])
        try:
            delta = (dt.datetime.fromisoformat(a["decided_at"])
                     - dt.datetime.fromisoformat(full["first_seen_at"]))
            hours.append(delta.total_seconds() / 3600)
        except (ValueError, TypeError, KeyError):
            continue
    return {
        "approved": len(approved),
        "rejected": len([a for a in actioned if a["hub_status"] == "REJECTED"]),
        "avg_hours": (sum(hours) / len(hours)) if hours else None,
    }


def run_weekly_digest(force: bool = False) -> dict:
    """Mondays, once per ISO week: pending count, average
    time-to-approval, oldest outstanding draft."""
    now_uk = dt.datetime.now(UK_TZ)
    week_key = f"{now_uk.isocalendar().year}-W{now_uk.isocalendar().week}"
    if not force:
        if now_uk.weekday() != 0 or db.xero_kv_get("watchdog_last_weekly") == week_key:
            return {"ran": False}

    report = overdue_report()
    stats = approval_stats(30)
    oldest = max(report["pending"], key=lambda d: d["age_days"], default=None)

    if stats["avg_hours"] is None:
        avg_text = "n/a (nothing approved in the last 30 days)"
    elif stats["avg_hours"] < 48:
        avg_text = f"{stats['avg_hours']:.1f} hours"
    else:
        avg_text = f"{stats['avg_hours'] / 24:.1f} days"

    body = (
        "<h3>Invoice Hub — weekly digest</h3><ul>"
        f"<li><b>{len(report['pending'])}</b> draft(s) currently awaiting review "
        f"({len(report['escalate'])} escalated, {len(report['warn'])} overdue)</li>"
        f"<li>Last 30 days: <b>{stats['approved']}</b> approved, "
        f"<b>{stats['rejected']}</b> rejected</li>"
        f"<li>Average time-to-approval: <b>{avg_text}</b></li>"
    )
    if oldest:
        body += (
            f"<li>Oldest outstanding: <b>{html.escape(oldest.get('invoice_number') or '(no number)')}</b> "
            f"({html.escape(oldest.get('contact_name') or '')}, "
            f"{_fmt_money(oldest.get('total'))}) — {oldest['age_days']} days</li>"
        )
    body += f"</ul><p><a href='{_hub_url()}'>Open the Invoice Hub</a></p>"

    db.xero_kv_set("watchdog_last_digest_html", body)
    emailed = send_email("Invoice Hub: weekly digest", body)
    db.xero_kv_set("watchdog_last_weekly", week_key)
    db.record_xero_event(None, "watchdog_weekly", f"emailed={emailed}")
    return {"ran": True, "emailed": emailed}


def maybe_run(force: bool = False) -> None:
    """Cheap to call on every approver page load — each part throttles
    itself in the DB."""
    try:
        run_daily_sweep(force=force)
        run_weekly_digest(force=force)
    except Exception as exc:
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"Watchdog sweep failed: {exc}"[:500])


if __name__ == "__main__":
    db.init_db()
    import xero_sync
    for r in xero_sync.sync_all():
        print(r)
    print(run_daily_sweep(force=True))
    print(run_weekly_digest())
