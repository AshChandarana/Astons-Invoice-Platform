"""Tests for the third review round: empty-batch probe, bounded fallbacks,
backup gate cleared on recovery, hard re-check skip, network errors recorded,
no-email approvals excluded from resends."""
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


# ---------------------------------------------------------------- presence

def test_empty_batch_with_decisive_probe_flags_the_deleted_drafts(hub, monkeypatch):
    """Every pending draft deleted in Xero and the IDs lookup returns []:
    the probe (404) proves the emptiness is real, so they are flagged via
    the per-invoice fallback."""
    for iid in ("A", "B", "C"):
        _draft(hub.db, iid, number=f"2610000{iid}")
    fake = FakeXero(statuses={}, singles={})        # singles 404 for everything
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    result = hub.sync.sync_all()[0]
    assert result["external_actions"] == 3
    for iid in ("A", "B", "C"):
        assert _hub_status(hub.db, iid) == "EXTERNAL_ACTION"
    assert not [f for f in hub.db.xero_recent_sync_failures() if "Presence" in f["message"]]


def test_empty_batch_with_probe_status_uses_it(hub, monkeypatch):
    _draft(hub.db, "A")
    _draft(hub.db, "B")
    fake = FakeXero(statuses={}, singles={"A": "DELETED", "B": "DRAFT"})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    hub.sync.sync_all()
    assert _hub_status(hub.db, "A") == "EXTERNAL_ACTION"
    assert _hub_status(hub.db, "B") == "PENDING_REVIEW"
    paths = [p for p, _ in fake.calls]
    assert paths.count("Invoices/A") == 1 and paths.count("Invoices/B") == 1


def test_empty_batch_with_server_error_probe_is_inconclusive(hub, monkeypatch):
    _draft(hub.db, "A")
    _draft(hub.db, "B")

    class Broken(FakeXero):
        def api_get(self, tenant_id, path, params=None, if_modified_since=None):
            self.calls.append((path, dict(params or {})))
            if path.startswith("Invoices/"):
                raise hub.client.XeroApiError("GET failed (503): upstream", status_code=503)
            return {"Invoices": []}
    fake = Broken()
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    result = hub.sync.sync_all()[0]
    assert result["ok"] and result["external_actions"] == 0
    assert _hub_status(hub.db, "A") == "PENDING_REVIEW"
    assert [p for p, _ in fake.calls].count("Invoices/A") == 1      # one probe only
    assert any("Presence check" in f["message"] for f in hub.db.xero_recent_sync_failures())


def test_per_invoice_fallbacks_are_bounded_per_sync(hub, monkeypatch):
    ids = [f"ID{i:02d}" for i in range(15)]
    for i in ids:
        _draft(hub.db, i, number=i)
    # batch returns one known draft so it is not "empty"; the other 14 are missing
    fake = FakeXero(statuses={"ID00": "DRAFT"}, singles={})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    result = hub.sync.sync_all()[0]
    singles = [p for p, _ in fake.calls if p.startswith("Invoices/")]
    assert len(singles) == hub.sync.MAX_FALLBACK_FETCHES
    assert result["external_actions"] == hub.sync.MAX_FALLBACK_FETCHES
    flagged = sum(1 for i in ids if _hub_status(hub.db, i) == "EXTERNAL_ACTION")
    assert flagged == hub.sync.MAX_FALLBACK_FETCHES            # the rest wait


# ---------------------------------------------------------------- Microsoft

def test_recovery_clears_backup_gate_and_hard_recheck_is_skipped(hub, monkeypatch):
    _ms_env(monkeypatch, secret="old")
    monkeypatch.setattr(hub.watchdog.requests, "post", lambda *a, **k: Resp(
        401, body={"error": "invalid_client", "error_description": "AADSTS7000222"}))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    hub.db.xero_kv_set("backup_last_failed_at", dt.datetime.utcnow().isoformat())

    # hard + same secret: only an hourly probe, so 30 minutes on nothing fires
    rec = hub.watchdog.ms_auth_status()
    rec["at"] = (dt.datetime.utcnow() - dt.timedelta(minutes=30)).isoformat()
    hub.db.xero_kv_set(hub.watchdog.MS_AUTH_KEY, json.dumps(rec))
    assert hub.watchdog.maybe_recheck_ms_auth() is False

    # new secret -> probe -> ok -> backup may retry immediately
    monkeypatch.setenv("MS_CLIENT_SECRET", "new")
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(200, body={"access_token": "t"}))
    assert hub.watchdog.maybe_recheck_ms_auth() is True
    assert hub.watchdog.ms_auth_status()["state"] == "ok"
    assert hub.db.xero_kv_get("backup_last_failed_at") is None


def test_new_secret_failing_transiently_is_not_called_hard(hub, monkeypatch):
    _ms_env(monkeypatch, secret="old")
    monkeypatch.setattr(hub.watchdog.requests, "post", lambda *a, **k: Resp(
        401, body={"error": "invalid_client", "error_description": "AADSTS7000222"}))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    assert hub.watchdog.ms_auth_status()["hard"] is True
    monkeypatch.setenv("MS_CLIENT_SECRET", "new")
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(503, text="unavailable"))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    status = hub.watchdog.ms_auth_status()
    assert status["state"] == "down" and status["hard"] is False


def test_network_error_is_recorded_as_down(hub, monkeypatch):
    _ms_env(monkeypatch)

    def boom(*a, **k):
        raise hub.watchdog.requests.ConnectionError("dns")
    monkeypatch.setattr(hub.watchdog.requests, "post", boom)
    with pytest.raises(RuntimeError) as exc:
        hub.watchdog._graph_token()
    assert "unreachable" in str(exc.value)
    status = hub.watchdog.ms_auth_status()
    assert status["state"] == "flaky" and status["error"].startswith("network")
    assert hub.watchdog.check_ms_auth()["state"] == "down"     # second failure


# ---------------------------------------------------------------- resend targeting

def test_approval_with_no_raiser_email_is_not_a_resend_candidate(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("ZZ", "Nobody", None)
    _draft(hub.db, "A")
    hub.db.xero_mark_approved("A", uid, "AA", b"%PDF", "f.pdf",
                              attachment_ok=True, raiser_pair="ZZ")
    hub.db.record_xero_event(uid, "xero_approve_notify",
                             "xero_invoice_id=A notified=no-email")
    assert hub.actions.unnotified_approvals("2026-01-01T00:00:00") == []


def test_preview_can_send_matches_the_sender_predicate(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("BT", "Bradley", "bradley@astonsaccountants.co.uk")
    _draft(hub.db, "A")
    hub.db.xero_mark_approved("A", uid, "AA", b"%PDF", "f.pdf",
                              attachment_ok=True, raiser_pair="BT")
    with hub.db.get_conn() as conn:
        conn.execute("UPDATE xero_drafts SET branded_pdf = NULL WHERE invoice_id = 'A'")
    preview = hub.actions.unnotified_approvals("2026-01-01T00:00:00")
    assert len(preview) == 1 and preview[0]["can_send"] is False
    result = hub.actions.resend_approval_emails("2026-01-01T00:00:00", uid)
    assert result["skipped"] == ["26100001"] and result["sent"] == []
