"""Tests for the fourth review round: only a real 404 writes a draft off,
probe results are reused, reject/approve record why nobody was emailed,
skipped resends clear from the preview, boot/hourly Microsoft probes."""
import datetime as dt
import json

import pytest

from tests.test_sync_resilience import (FakeXero, Resp, _draft, _hub_status,
                                        hub)  # noqa: F401 (fixture)


def _ms_env(monkeypatch, secret="s1"):
    for k in ("MS_CLIENT_ID", "MS_TENANT_ID"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("MS_CLIENT_SECRET", secret)
    monkeypatch.setenv("MS_SENDER_EMAIL", "ash@astonsaccountants.co.uk")


# ---------------------------------------------------------------- 404 vs 5xx

def test_fallback_server_error_leaves_draft_pending(hub, monkeypatch):
    _draft(hub.db, "LIVE")
    _draft(hub.db, "MISSING")

    class Wobbly(FakeXero):
        def api_get(self, tenant_id, path, params=None, if_modified_since=None):
            if path == "Invoices/MISSING":
                self.calls.append((path, {}))
                raise hub.client.XeroApiError("GET Invoices/MISSING failed (503): x",
                                              status_code=503)
            return super().api_get(tenant_id, path, params, if_modified_since)
    fake = Wobbly(statuses={"LIVE": "DRAFT"})       # batch non-empty, MISSING absent
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)

    result = hub.sync.sync_all()[0]
    assert result["ok"] and result["external_actions"] == 0
    assert _hub_status(hub.db, "MISSING") == "PENDING_REVIEW"


def test_fallback_404_still_writes_off(hub, monkeypatch):
    _draft(hub.db, "LIVE")
    _draft(hub.db, "GONE1")
    fake = FakeXero(statuses={"LIVE": "DRAFT"}, singles={})    # GONE1 -> 404
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    hub.sync.sync_all()
    assert _hub_status(hub.db, "GONE1") == "EXTERNAL_ACTION"
    assert "no longer retrievable" in hub.db.xero_get_draft("GONE1")["external_action_note"]


def test_probe_404_is_not_fetched_twice(hub, monkeypatch):
    _draft(hub.db, "A")
    fake = FakeXero(statuses={}, singles={})                   # empty batch, probe 404
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    hub.sync.sync_all()
    assert _hub_status(hub.db, "A") == "EXTERNAL_ACTION"
    assert [p for p, _ in fake.calls].count("Invoices/A") == 1


def test_live_probe_after_empty_batch_distrusts_the_batch(hub, monkeypatch):
    for iid in ("A", "B", "C"):
        _draft(hub.db, iid, number=f"2610000{iid}")
    fake = FakeXero(statuses={}, singles={"A": "DRAFT"})       # B, C would 404
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    result = hub.sync.sync_all()[0]
    assert result["external_actions"] == 0
    assert all(_hub_status(hub.db, i) == "PENDING_REVIEW" for i in ("A", "B", "C"))
    singles = [p for p, _ in fake.calls if p.startswith("Invoices/")]
    assert singles == ["Invoices/A"]                           # no fetches for B, C


def test_sync_log_records_call_count(hub, monkeypatch):
    _draft(hub.db, "A")
    fake = FakeXero(statuses={"A": "DRAFT"})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    monkeypatch.setattr(hub.client, "calls_made", 0)

    def counting(tenant_id, path, params=None, if_modified_since=None):
        hub.client.calls_made += 1
        return fake.api_get(tenant_id, path, params, if_modified_since)
    monkeypatch.setattr(hub.client, "api_get", counting)
    result = hub.sync.sync_all()[0]
    assert result["calls"] == 2                                # delta + presence
    with hub.db.get_conn() as conn:
        row = conn.execute("SELECT message FROM xero_sync_log WHERE ok = 1 "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["message"] == "2 Xero call(s)"


# ---------------------------------------------------------------- notify bookkeeping

def test_skipped_resend_is_recorded_and_drops_out_of_preview(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("ZZ", "Nobody", None)
    _draft(hub.db, "A")
    hub.db.xero_mark_approved("A", uid, "AA", b"%PDF", "f.pdf",
                              attachment_ok=True, raiser_pair="ZZ")
    hub.db.record_xero_event(uid, "xero_approve_notify", "xero_invoice_id=A notified=none")
    assert len(hub.actions.unnotified_approvals("2026-01-01T00:00:00")) == 1
    result = hub.actions.resend_approval_emails("2026-01-01T00:00:00", uid)
    assert result["skipped"] == ["26100001"]
    assert hub.actions.unnotified_approvals("2026-01-01T00:00:00") == []


def test_reject_reports_whether_an_email_was_due(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("BT", "Bradley", "bradley@astonsaccountants.co.uk")
    _draft(hub.db, "A")
    with hub.db.get_conn() as conn:
        conn.execute("UPDATE xero_drafts SET raiser_pair = 'BT' WHERE invoice_id = 'A'")
    monkeypatch.setattr(hub.actions, "_fetch_live_invoice",
                        lambda t, i: {"InvoiceID": i, "Status": "DRAFT"})
    monkeypatch.setattr(hub.client, "api_post", lambda *a, **k: {})
    monkeypatch.setattr(hub.watchdog, "send_email", lambda *a, **k: False)   # outage
    result = hub.actions.reject_draft("A", uid, "Wrong amount")
    assert result["notified"] == [] and result["emails_expected"] is True
    with hub.db.get_conn() as conn:
        note = conn.execute("SELECT note FROM audit_log WHERE action = 'xero_reject_notify' "
                            "ORDER BY id DESC LIMIT 1").fetchone()["note"]
    assert note.endswith("notified=none")


# ---------------------------------------------------------------- Microsoft probes

def test_check_ms_auth_when_unconfigured(hub, monkeypatch):
    for k in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID", "MS_SENDER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    status = hub.watchdog.check_ms_auth()
    assert status["state"] == "unconfigured" and "not all set" in status["error"]


def test_hard_failure_is_still_probed_hourly(hub, monkeypatch):
    _ms_env(monkeypatch)
    monkeypatch.setattr(hub.watchdog.requests, "post", lambda *a, **k: Resp(
        401, body={"error": "invalid_client", "error_description": "AADSTS7000222"}))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    assert hub.watchdog.maybe_recheck_ms_auth() is False          # fresh failure
    rec = hub.watchdog.ms_auth_status()
    rec["at"] = (dt.datetime.utcnow() - dt.timedelta(minutes=30)).isoformat()
    hub.db.xero_kv_set(hub.watchdog.MS_AUTH_KEY, json.dumps(rec))
    assert hub.watchdog.maybe_recheck_ms_auth() is False          # under an hour
    rec["at"] = (dt.datetime.utcnow() - dt.timedelta(minutes=61)).isoformat()
    hub.db.xero_kv_set(hub.watchdog.MS_AUTH_KEY, json.dumps(rec))
    assert hub.watchdog.maybe_recheck_ms_auth() is True           # hourly probe
