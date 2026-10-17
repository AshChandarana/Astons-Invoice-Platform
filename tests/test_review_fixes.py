"""Tests added from the independent review of the Oct 2026 resilience work."""
import datetime as dt

import pytest

from tests.test_sync_resilience import (FakeXero, Resp, _draft, _hub_status,
                                        _patch_request, hub)  # noqa: F401 (fixture)


# ---------------------------------------------------------------- presence safety

def test_unknown_status_in_batch_falls_back_instead_of_flagging(hub, monkeypatch):
    """If Xero ever omits or renames Status, nothing may be mass-flagged."""
    _draft(hub.db, "A")
    _draft(hub.db, "B")

    class Odd(FakeXero):
        def api_get(self, tenant_id, path, params=None, if_modified_since=None):
            if path == "Invoices" and params and "IDs" in params:
                self.calls.append((path, dict(params)))
                return {"Invoices": [{"InvoiceID": "A"},               # no Status
                                     {"InvoiceID": "B", "Status": "WEIRD"}]}
            return super().api_get(tenant_id, path, params, if_modified_since)
    fake = Odd(singles={"A": "DRAFT", "B": "DRAFT"})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)

    hub.sync.sync_all()
    assert _hub_status(hub.db, "A") == "PENDING_REVIEW"
    assert _hub_status(hub.db, "B") == "PENDING_REVIEW"
    assert [p for p, _ in fake.calls].count("Invoices/A") == 1   # fallback asked


def test_approval_landing_mid_sync_is_never_overwritten(hub, monkeypatch):
    """Worker reads pending ids, approver approves X meanwhile, Xero then
    reports X AUTHORISED: X must stay APPROVED, not become EXTERNAL_ACTION."""
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    _draft(hub.db, "X")
    hub.db.xero_mark_approved("X", uid, "AA", b"%PDF", "FeeNote_X.pdf",
                              attachment_ok=True, raiser_pair="BT")
    hub.db.xero_mark_external_action("X", "Actioned outside the Hub — AUTHORISED.")
    assert _hub_status(hub.db, "X") == "APPROVED"
    assert hub.db.xero_get_draft("X")["external_action_note"] is None


# ---------------------------------------------------------------- rate-limit semantics

def test_daily_header_spelled_daily_is_daily_even_with_short_retry(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "daily", "Retry-After": "120"})])
    with pytest.raises(hub.client.XeroRateLimited) as exc:
        hub.client.api_get("T1", "Invoices")
    assert exc.value.daily is True and exc.value.retry_after == 120


def test_app_minute_limit_is_not_treated_as_daily(hub, monkeypatch):
    slept = []
    monkeypatch.setattr(hub.client.time, "sleep", slept.append)
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "app", "Retry-After": "2"}),
        Resp(200, body={"Invoices": []}),
    ])
    assert hub.client.api_get("T1", "Invoices") == {"Invoices": []}
    assert slept == [2]
    assert hub.client.rate_limit_status() is None


def test_daily_429_without_retry_after_still_pauses_for_an_hour(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [Resp(429, {"X-Rate-Limit-Problem": "daily"})])
    with pytest.raises(hub.client.XeroRateLimited) as exc:
        hub.client.api_get("T1", "Invoices")
    assert exc.value.retry_after == hub.client.DAILY_RETRY_FLOOR
    paused = hub.client.rate_limit_status("T1")
    assert paused and paused["retry_after"] == 3600


def test_successful_call_clears_the_daily_pause(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "daily", "Retry-After": "12000"}),
        Resp(200, body={"Invoices": []}),
    ])
    with pytest.raises(hub.client.XeroRateLimited):
        hub.client.api_get("T1", "Invoices")
    assert hub.client.rate_limit_status("T1")
    hub.client.api_get("T1", "Invoices")                  # Xero serving again
    assert hub.client.rate_limit_status("T1") is None


def test_minute_limit_total_wait_is_bounded(hub, monkeypatch):
    slept = []
    monkeypatch.setattr(hub.client.time, "sleep", slept.append)
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "minute", "Retry-After": "60"})] * 5)
    with pytest.raises(hub.client.XeroRateLimited) as exc:
        hub.client.api_get("T1", "Invoices")
    assert exc.value.daily is False
    assert sum(slept) <= hub.client.MAX_MINUTE_WAIT       # 60 then stop, not 4 x 60
    assert "try again shortly" in str(exc.value)


def test_paused_tenant_is_skipped_but_others_still_sync(hub, monkeypatch):
    hub.db.xero_upsert_connection("T2", "Second Org")
    _draft(hub.db, "A")
    fake = FakeXero(statuses={"A": "DRAFT"})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    hub.db.xero_kv_set(hub.client._rate_limit_key("T1"), __import__("json").dumps(
        {"daily": True, "retry_after": 9000, "at": dt.datetime.utcnow().isoformat()}))
    hub.db.xero_kv_set("last_poll_at", "2000-01-01T00:00:00")

    results = hub.sync.maybe_sync()
    by_tenant = {r["tenant_id"]: r for r in results}
    assert by_tenant["T1"]["skipped"] == "paused"
    assert by_tenant["T2"]["ok"] and "skipped" not in by_tenant["T2"]
    assert fake.tenants and set(fake.tenants) == {"T2"}      # T1 never called


def test_daily_failure_logged_at_most_hourly(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "daily", "Retry-After": "9000"})] * 3)
    for _ in range(3):
        hub.sync.maybe_sync(force=True)                   # three "Sync now" presses
    msgs = [f["message"] for f in hub.db.xero_recent_sync_failures()]
    assert len([m for m in msgs if "daily call allowance" in m]) == 1


# ---------------------------------------------------------------- resend targeting

def test_resend_only_targets_fee_notes_whose_email_failed(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("BT", "Bradley", "bradley@astonsaccountants.co.uk")
    for iid in ("OK", "FAILED", "NOEVENT"):
        _draft(hub.db, iid, number=f"261{iid}")
        hub.db.xero_mark_approved(iid, uid, "AA", b"%PDF", f"FeeNote_{iid}.pdf",
                                  attachment_ok=True, raiser_pair="BT")
    hub.db.record_xero_event(uid, "xero_approve_notify",
                             "xero_invoice_id=OK notified=bradley@astonsaccountants.co.uk")
    hub.db.record_xero_event(uid, "xero_approve_notify",
                             "xero_invoice_id=FAILED notified=none")

    preview = hub.actions.unnotified_approvals("2026-01-01T00:00:00")
    assert sorted(c["invoice_id"] for c in preview) == ["FAILED", "NOEVENT"]
    assert all(c["can_send"] for c in preview)

    sent = []
    monkeypatch.setattr(hub.watchdog, "send_email",
                        lambda subject, body, to=None, attachments=None:
                        sent.append(subject) or True)
    result = hub.actions.resend_approval_emails("2026-01-01T00:00:00", uid)
    assert sorted(result["sent"]) == ["261FAILED", "261NOEVENT"]
    assert len(sent) == 2

    # pressing the button again sends nothing: the resend was recorded
    sent.clear()
    assert hub.actions.unnotified_approvals("2026-01-01T00:00:00") == []
    again = hub.actions.resend_approval_emails("2026-01-01T00:00:00", uid)
    assert again["sent"] == [] and again["considered"] == 0 and sent == []


# ---------------------------------------------------------------- Microsoft status

def test_single_transient_token_failure_does_not_light_the_banner(hub, monkeypatch):
    for k in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(503, text="Service Unavailable"))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    assert hub.watchdog.ms_auth_status()["state"] == "flaky"
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()                       # second in a row
    assert hub.watchdog.ms_auth_status()["state"] == "down"

    # a credential error is 'down' immediately, even as a first failure
    hub.db.xero_kv_delete(hub.watchdog.MS_AUTH_KEY)
    monkeypatch.setattr(hub.watchdog.requests, "post", lambda *a, **k: Resp(
        401, body={"error": "invalid_client", "error_description": "AADSTS7000222 expired"}))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    assert hub.watchdog.ms_auth_status()["state"] == "down"


def test_non_dict_error_body_is_handled(hub, monkeypatch):
    for k in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(400, body=["not", "a", "dict"], text="x"))
    with pytest.raises(RuntimeError) as exc:
        hub.watchdog._graph_token()
    assert "Microsoft sign-in failed" in str(exc.value)
