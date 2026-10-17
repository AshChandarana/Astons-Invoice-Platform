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


MS_AUTH_KEY = "ms_auth_status"
SECRET_EXPIRED_TEXT = (
    "Microsoft sign-in refused: the Hub's client secret has expired or is "
    "invalid. Create a new client secret for the app in Microsoft Entra and "
    "update MS_CLIENT_SECRET on Railway (Invoice Hub, Launchpad and MyAstons "
    "share it)."
)


MS_RECHECK_MINUTES = 15
MS_RECHECK_HARD_MINUTES = 60


def _secret_fingerprint() -> str:
    """Short, non-reversible marker of the configured secret, so a renewed
    secret is recognised and re-tried at once."""
    import hashlib
    return hashlib.sha256(os.environ.get("MS_CLIENT_SECRET", "").encode()).hexdigest()[:8]


def _set_ms_auth_status(state: str, error: str = None, hard: bool = False) -> None:
    """Remember whether Microsoft is accepting our sign-in, so every page
    can show a banner the moment it stops (and clear it when it recovers).
    A credential failure (hard=True) is 'down' at once; a transient failure
    only counts as 'down' on the second consecutive one, so a single blip
    does not light the banner for everyone. 'since' keeps the time of the
    FIRST failure of the current outage; the ok record keeps the last
    outage's start as 'last_down_since' (for the resend default date)."""
    import json
    now = dt.datetime.utcnow().isoformat()
    previous = ms_auth_status()
    fingerprint = _secret_fingerprint()
    if state == "down":
        failures = int(previous.get("failures") or 0) + 1
        was_down = previous.get("state") == "down"
        since = previous.get("since") if previous.get("state") in ("down", "flaky") else now
        new_state = "down" if (hard or was_down or failures >= 2) else "flaky"
        # A credential problem stays 'hard' for the outage — unless the
        # secret has since been changed, which is fresh evidence.
        same_secret = previous.get("secret") in (None, fingerprint)
        sticky_hard = bool(previous.get("hard")) and same_secret
        db.xero_kv_set(MS_AUTH_KEY, json.dumps(
            {"state": new_state, "since": since or now, "at": now,
             "failures": failures, "hard": bool(hard or sticky_hard),
             "error": (error or "")[:300], "secret": fingerprint}))
    else:
        if previous.get("state") == "ok" and previous.get("at"):
            return                       # already fine; skip the write
        record = {"state": "ok", "at": now, "since": now}
        last_down = previous.get("since") if previous.get("state") in ("down", "flaky") else None
        last_down = last_down or previous.get("last_down_since")
        if last_down:
            record["last_down_since"] = last_down
        db.xero_kv_set(MS_AUTH_KEY, json.dumps(record))
        # Recovery: let the nightly backup retry at the next tick rather
        # than waiting out its hourly gate.
        db.xero_kv_delete("backup_last_failed_at")


def check_ms_auth() -> dict:
    """Ask Microsoft for a token (no email) and record the outcome. Returns
    the new status dict (with 'checked_at'). Safe to call from a button or
    the worker."""
    if not email_configured():
        return {"state": "unconfigured",
                "error": "MS_CLIENT_ID / MS_CLIENT_SECRET / MS_TENANT_ID / "
                         "MS_SENDER_EMAIL are not all set"}
    try:
        _graph_token()
    except Exception:
        pass                             # _graph_token already recorded it
    status = ms_auth_status()
    status["checked_at"] = dt.datetime.utcnow().isoformat()
    return status


def maybe_recheck_ms_auth() -> bool:
    """Worker tick: while Microsoft is marked down/flaky, re-probe when the
    secret has been changed (new fingerprint) or every MS_RECHECK_MINUTES,
    so the banner clears itself soon after a renewal. Returns True if a
    probe was made."""
    status = ms_auth_status()
    if status.get("state") not in ("down", "flaky") or not email_configured():
        return False
    changed = status.get("secret") and status["secret"] != _secret_fingerprint()
    try:
        age = dt.datetime.utcnow() - dt.datetime.fromisoformat(status.get("at") or "")
    except ValueError:
        age = dt.timedelta(days=1)
    # A credential failure cannot fix itself without a new secret, so probe
    # only hourly in case it was fixed in Entra some other way; anything
    # else is re-tried every MS_RECHECK_MINUTES.
    wait = dt.timedelta(minutes=MS_RECHECK_HARD_MINUTES if status.get("hard")
                        else MS_RECHECK_MINUTES)
    if not changed and age < wait:
        return False
    check_ms_auth()
    return True


def ms_auth_status() -> dict:
    """{'state': 'ok'|'down', 'since', 'at', 'error'} or {} before any call."""
    import json
    raw = db.xero_kv_get(MS_AUTH_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def secret_expiry_warning(today: dt.date = None) -> dict:
    """Warning about the Microsoft client secret's expiry, from the
    MS_SECRET_EXPIRES env var (ISO date, as shown in Entra). Returns
    {'level': 'warning'|'error', 'days': n, 'text': ...} from 30 days out,
    else {}."""
    raw = os.environ.get("MS_SECRET_EXPIRES", "").strip()
    if not raw:
        return {}
    try:
        expires = dt.date.fromisoformat(raw[:10])
    except ValueError:
        return {}
    today = today or dt.datetime.now(ZoneInfo("Europe/London")).date()
    days = (expires - today).days
    when = expires.strftime("%d %b %Y").lstrip("0")
    fix = ("Create a new client secret for the app in Microsoft Entra and update "
           "MS_CLIENT_SECRET (and MS_SECRET_EXPIRES) on Railway for the Invoice Hub, "
           "Launchpad and MyAstons.")
    if days < 0:
        return {"level": "error", "days": days,
                "text": f"The Microsoft client secret expired on {when}. "
                        f"Emails and backups will fail until it is renewed. {fix}"}
    if days == 0:
        return {"level": "error", "days": 0,
                "text": f"The Microsoft client secret expires TODAY ({when}). {fix}"}
    if days <= 30:
        return {"level": "warning", "days": days,
                "text": f"The Microsoft client secret expires in {days} day"
                        f"{'s' if days != 1 else ''}, on {when}. {fix}"}
    return {}


def _graph_token() -> str:
    try:
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
    except requests.RequestException as exc:
        _set_ms_auth_status("down", f"network: {exc}"[:160])
        raise RuntimeError(f"Microsoft sign-in unreachable ({exc}).") from exc
    if resp.status_code != 200:
        try:
            err = resp.json()
        except ValueError:
            err = {}
        if not isinstance(err, dict):
            err = {}
        code = (err.get("error") or f"HTTP {resp.status_code}").strip()
        desc = (err.get("error_description") or resp.text or "").strip()
        short = f"{code}: {desc[:160]}"
        credential_problem = (code in ("invalid_client", "unauthorized_client")
                              or "AADSTS7000222" in desc)
        _set_ms_auth_status("down", short, hard=credential_problem)
        if credential_problem:
            raise RuntimeError(SECRET_EXPIRED_TEXT)
        raise RuntimeError(f"Microsoft sign-in failed ({short}).")
    _set_ms_auth_status("ok")
    return resp.json()["access_token"]


def send_email(subject: str, html_body: str, to: list = None,
               attachments: list = None) -> bool:
    """Send via Graph to `to` (defaults to the watchdog alert recipient).
    attachments: [{'name': str, 'bytes': bytes}] — e.g. the branded fee
    note PDF. Returns True on success; failures are logged to the sync
    log (never silent) and False is returned."""
    if not email_configured():
        db.xero_kv_set("watchdog_email_status",
                       "Email not configured — MS_CLIENT_ID/SECRET/TENANT_ID/SENDER_EMAIL missing.")
        return False
    recipients = [a for a in (to or [alert_recipient()]) if a and a.strip()]
    if not recipients:
        return False
    try:
        import base64
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [
                {"emailAddress": {"address": a.strip()}} for a in recipients
            ],
        }
        if attachments:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": a["name"],
                    "contentType": "application/pdf",
                    "contentBytes": base64.b64encode(a["bytes"]).decode("ascii"),
                }
                for a in attachments
            ]
        token = _graph_token()
        resp = requests.post(
            GRAPH_SENDMAIL_URL.format(sender=os.environ["MS_SENDER_EMAIL"].strip()),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"message": message, "saveToSentItems": "true"},
            timeout=60,
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
        f"({draft.get('contact_name') or ''})",  # subjects are plain text
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
