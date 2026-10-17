"""Tests for the second review round: inconclusive presence batches, note
clearing on decisions, log-flood limits, Microsoft re-check, pause stamping."""
import datetime as dt
import json

import pytest

from tests.test_sync_resilience import (FakeXero, Resp, _draft, _hub_status,
                                        _patch_request, hub)  # noqa: F401 (fixture)


def _ms_env(monkeypatch, secret="s1"):
    for k in ("MS_CLIENT_ID", "MS_TENANT_ID"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("MS_CLIENT_SECRET", secret)
    monkeypatch.setenv("MS_SENDER_EMAIL", "ash@astonsaccountants.co.uk")


# ---------------------------------------------------------------- presence

def test_empty_presence_batch_is_inconclusive_not_a_mass_fallback(hub, monkeypatch):
    for iid in ("A", "B", "C"):
        _draft(hub.db, iid, number=f"2610000{iid}")

    class Unwell(FakeXero):
        """Xero returns nothing for the batch AND cannot answer the probe."""
        def api_get(self, tenant_id, path, params=None, if_modified_since=None):
            self.calls.append((path, dict(params or {})))
            if path.startswith("Invoices/"):
                raise hub.client.XeroApiError("GET failed (500): oops", status_code=500)
            return {"Invoices": []}
    fake = Unwell()
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)

    result = hub.sync.sync_all()[0]
    assert result["ok"] and result["external_actions"] == 0
    assert all(_hub_status(hub.db, i) == "PENDING_REVIEW" for i in ("A", "B", "C"))
    paths = [p for p, _ in fake.calls]
    assert [p for p in paths if p.startswith("Invoices/")] == ["Invoices/A"]   # one probe
    msgs = [f["message"] for f in hub.db.xero_recent_sync_failures()]
    assert any("Presence check" in m for m in msgs)
    hub.sync.sync_all()                                          # second sync same day
    msgs = [f["message"] for f in hub.db.xero_recent_sync_failures()]
    assert len([m for m in msgs if "Presence check" in m]) == 1  # logged once a day


def test_external_action_flag_reports_false_when_already_decided(hub):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    _draft(hub.db, "X")
    assert hub.db.xero_mark_external_action("X", "gone") is True
    _draft(hub.db, "Y")
    hub.db.xero_mark_approved("Y", uid, "AA", b"%PDF", "f.pdf", attachment_ok=True)
    assert hub.db.xero_mark_external_action("Y", "gone") is False


def test_approving_clears_a_stale_external_note(hub):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    _draft(hub.db, "X")
    hub.db.xero_mark_external_action("X", "Actioned outside the Hub — AUTHORISED.")
    hub.db.xero_mark_approved("X", uid, "AA", b"%PDF", "f.pdf", attachment_ok=True)
    row = hub.db.xero_get_draft("X")
    assert row["hub_status"] == "APPROVED" and row["external_action_note"] is None


# ---------------------------------------------------------------- log flooding

def test_daily_limit_dedupe_survives_interleaved_failures(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "daily", "Retry-After": "9000"})] * 2)
    hub.sync.maybe_sync(force=True)
    hub.db.xero_log_sync(None, ok=False, fetched=0, message="Watchdog email failed: x")
    hub.sync.maybe_sync(force=True)
    msgs = [f["message"] for f in hub.db.xero_recent_sync_failures()]
    assert len([m for m in msgs if "daily call allowance" in m]) == 1
    assert not any(m.startswith("GET ") for m in msgs)        # plain text, no path prefix


def test_backup_failure_logged_once_per_day_but_status_hourly(hub, monkeypatch):
    _ms_env(monkeypatch)
    monkeypatch.setattr(hub.backup, "BACKUP_AFTER_HOUR", 0)
    monkeypatch.setattr(hub.backup, "run_backup",
                        lambda: (_ for _ in ()).throw(RuntimeError("refused")))
    hub.backup.maybe_backup()
    hour_ago = (dt.datetime.utcnow() - dt.timedelta(minutes=61)).isoformat()
    hub.db.xero_kv_set("backup_last_failed_at", hour_ago)
    hub.backup.maybe_backup()
    msgs = [f["message"] for f in hub.db.xero_recent_sync_failures()]
    assert len([m for m in msgs if "backup failed" in m]) == 1
    assert hub.db.xero_kv_get("backup_status").startswith("FAILED")


# ---------------------------------------------------------------- Microsoft re-check

def test_recheck_probes_at_once_when_secret_changes(hub, monkeypatch):
    _ms_env(monkeypatch, secret="old")
    monkeypatch.setattr(hub.watchdog.requests, "post", lambda *a, **k: Resp(
        401, body={"error": "invalid_client", "error_description": "AADSTS7000222"}))
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    status = hub.watchdog.ms_auth_status()
    assert status["state"] == "down" and status["hard"] is True

    # same secret, failure 1 minute old: no probe yet
    assert hub.watchdog.maybe_recheck_ms_auth() is False

    # renewed secret: probe immediately, and it succeeds -> ok, outage remembered
    monkeypatch.setenv("MS_CLIENT_SECRET", "new")
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(200, body={"access_token": "t"}))
    assert hub.watchdog.maybe_recheck_ms_auth() is True
    after = hub.watchdog.ms_auth_status()
    assert after["state"] == "ok" and after["last_down_since"] == status["since"]
    assert hub.watchdog.maybe_recheck_ms_auth() is False       # nothing to do when ok


def test_recheck_probes_every_15_minutes_while_down(hub, monkeypatch):
    _ms_env(monkeypatch)
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(503, text="unavailable"))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            hub.watchdog._graph_token()
    assert hub.watchdog.ms_auth_status()["state"] == "down"
    assert hub.watchdog.ms_auth_status()["hard"] is False
    assert hub.watchdog.maybe_recheck_ms_auth() is False
    rec = hub.watchdog.ms_auth_status()
    rec["at"] = (dt.datetime.utcnow() - dt.timedelta(minutes=16)).isoformat()
    hub.db.xero_kv_set(hub.watchdog.MS_AUTH_KEY, json.dumps(rec))
    assert hub.watchdog.maybe_recheck_ms_auth() is True


def test_check_ms_auth_button_path_records_outcome(hub, monkeypatch):
    _ms_env(monkeypatch)
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(200, body={"access_token": "t"}))
    assert hub.watchdog.check_ms_auth()["state"] == "ok"


# ---------------------------------------------------------------- pause stamping

def test_sync_now_does_not_extend_a_pause_when_xero_gives_no_retry_after(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [Resp(429, {"X-Rate-Limit-Problem": "daily"})] * 2)
    with pytest.raises(hub.client.XeroRateLimited):
        hub.client.api_get("T1", "Invoices")
    first = hub.client.rate_limit_status("T1")
    with pytest.raises(hub.client.XeroRateLimited):
        hub.client.api_get("T1", "Invoices")
    assert hub.client.rate_limit_status("T1")["until"] == first["until"]


def test_worker_start_clears_stale_lock(hub):
    hub.db.xero_kv_set(hub.sync.SYNC_LOCK_KEY, dt.datetime.utcnow().isoformat())
    hub.sync.clear_sync_lock()
    assert hub.db.xero_kv_get(hub.sync.SYNC_LOCK_KEY) is None
