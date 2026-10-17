"""Tests for the Oct 2026 resilience work: cheap presence check, fail-fast
on Xero's daily allowance, one-sync-at-a-time lock, Microsoft sign-in
status + secret-expiry warning, approval-email resend, hourly backup retry.

Run from the repo root:  py -m pytest -q
Each test gets its own SQLite file (DATABASE_PATH) — the live DB is never touched.
"""
import datetime as dt
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """Fresh DB + imported modules, Xero 'connected', email never sent."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hub.db"))
    for name in ("db", "xero_client", "xero_sync", "xero_watchdog",
                 "xero_backup", "xero_actions", "xero_attrib"):
        sys.modules.pop(name, None)
    import db
    import xero_client
    import xero_sync
    import xero_watchdog
    import xero_backup
    import xero_actions
    db.init_db()
    db.xero_upsert_connection("T1", "Astons")
    monkeypatch.setattr(xero_client, "is_connected", lambda: True)
    monkeypatch.setattr(xero_client, "refresh_connections", lambda: [])
    monkeypatch.setattr(xero_sync, "_notify_new_drafts", lambda results: None)
    monkeypatch.setattr(xero_client.time, "sleep", lambda s: pytest.fail("slept"))
    return types.SimpleNamespace(db=db, client=xero_client, sync=xero_sync,
                                 watchdog=xero_watchdog, backup=xero_backup,
                                 actions=xero_actions)


def _draft(db, invoice_id, number="26100001", status="DRAFT"):
    db.xero_upsert_draft(
        invoice_id=invoice_id, tenant_id="T1", invoice_number=number,
        reference="", contact_id="C", contact_name="Client Ltd",
        line_items_json="[]", sub_total=100, total_tax=20, total=120,
        date="2026-10-15", due_date="2026-10-15", updated_date_utc=None,
        branding_theme_id="", xero_status=status,
    )


def _hub_status(db, invoice_id):
    return db.xero_get_draft(invoice_id)["hub_status"]


class FakeXero:
    """Scripted api_get: records every call, answers the presence lookup
    from `statuses`, the delta fetch with nothing, single-invoice fetches
    from `singles`."""

    def __init__(self, statuses=None, singles=None):
        self.statuses = statuses or {}
        self.singles = singles or {}
        self.calls = []

    def api_get(self, tenant_id, path, params=None, if_modified_since=None):
        self.calls.append((path, dict(params or {})))
        self.tenants = getattr(self, "tenants", []) + [tenant_id]
        if path == "Invoices" and params and "IDs" in params:
            ids = params["IDs"].split(",")
            return {"Invoices": [{"InvoiceID": i, "Status": self.statuses[i]}
                                 for i in ids if i in self.statuses]}
        if path == "Invoices":
            return {"Invoices": []}                      # delta: nothing changed
        if path.startswith("Invoices/"):
            iid = path.split("/", 1)[1]
            if iid in self.singles:
                return {"Invoices": [{"InvoiceID": iid, "Status": self.singles[iid]}]}
            import xero_client
            raise xero_client.XeroApiError(f"GET {path} failed (404): not found",
                                           status_code=404)
        raise AssertionError(path)


# ---------------------------------------------------------------- presence

def test_presence_check_asks_by_id_and_flags_actioned_drafts(hub, monkeypatch):
    for iid in ("A", "B", "C", "D"):
        _draft(hub.db, iid, number=f"2610000{iid}")
    fake = FakeXero(statuses={"A": "DRAFT", "B": "AUTHORISED", "C": "DELETED"},
                    singles={})                           # D: unknown to Xero
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)

    results = hub.sync.sync_all()

    assert results[0]["ok"] and results[0]["external_actions"] == 3
    assert _hub_status(hub.db, "A") == "PENDING_REVIEW"
    assert _hub_status(hub.db, "B") == "EXTERNAL_ACTION"
    assert "AUTHORISED" in hub.db.xero_get_draft("B")["external_action_note"]
    assert "DELETED" in hub.db.xero_get_draft("C")["external_action_note"]
    assert "no longer retrievable" in hub.db.xero_get_draft("D")["external_action_note"]
    # one delta call + one presence call + one single fetch for D = 3 calls,
    # however many drafts the org holds
    paths = [p for p, _ in fake.calls]
    assert paths == ["Invoices", "Invoices", "Invoices/D"]
    presence = fake.calls[1][1]
    assert presence["summaryOnly"] == "true" and "DELETED" in presence["Statuses"]
    assert set(presence["IDs"].split(",")) == {"A", "B", "C", "D"}


def test_presence_check_batches_forty_ids_per_call(hub, monkeypatch):
    ids = [f"ID{i:03d}" for i in range(85)]
    for i in ids:
        _draft(hub.db, i, number=i)
    fake = FakeXero(statuses={i: "DRAFT" for i in ids})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)

    hub.sync.sync_all()

    presence_calls = [p for _, p in fake.calls if "IDs" in p]
    assert [len(p["IDs"].split(",")) for p in presence_calls] == [40, 40, 5]
    assert all(_hub_status(hub.db, i) == "PENDING_REVIEW" for i in ids)


def test_presence_missing_then_single_fetch_says_still_draft_keeps_it(hub, monkeypatch):
    _draft(hub.db, "A")
    fake = FakeXero(statuses={}, singles={"A": "DRAFT"})
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    hub.sync.sync_all()
    assert _hub_status(hub.db, "A") == "PENDING_REVIEW"


# ---------------------------------------------------------------- rate limits

class Resp:
    def __init__(self, status, headers=None, body=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _patch_request(hub, monkeypatch, responses):
    monkeypatch.setattr(hub.client, "get_access_token", lambda: "tok")
    seq = list(responses)
    sent = []

    def fake_request(method, url, **kw):
        sent.append((method, url))
        return seq.pop(0)
    monkeypatch.setattr(hub.client.requests, "request", fake_request)
    return sent


def test_daily_limit_fails_fast_without_sleeping(hub, monkeypatch):
    sent = _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "day", "Retry-After": "12000"}),
    ])
    with pytest.raises(hub.client.XeroRateLimited) as exc:
        hub.client.api_get("T1", "Invoices")
    assert exc.value.daily is True and exc.value.retry_after == 12000
    assert "daily call allowance" in str(exc.value)
    assert len(sent) == 1                                  # no retries
    paused = hub.client.rate_limit_status()
    assert paused and paused["retry_after"] == 12000


def test_long_retry_after_counts_as_daily_even_without_problem_header(hub, monkeypatch):
    _patch_request(hub, monkeypatch, [Resp(429, {"Retry-After": "7200"})])
    with pytest.raises(hub.client.XeroRateLimited) as exc:
        hub.client.api_get("T1", "Invoices")
    assert exc.value.daily is True


def test_minute_limit_sleeps_once_then_succeeds(hub, monkeypatch):
    slept = []
    monkeypatch.setattr(hub.client.time, "sleep", slept.append)
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "minute", "Retry-After": "3"}),
        Resp(200, {"X-DayLimit-Remaining": "4321", "X-MinLimit-Remaining": "59"},
             body={"Invoices": []}),
    ])
    assert hub.client.api_get("T1", "Invoices") == {"Invoices": []}
    assert slept == [3]
    assert hub.client.limits()["day_remaining"] == 4321
    assert hub.client.rate_limit_status() is None          # minute limit is not a lock-out


def test_sync_reports_daily_pause_and_auto_sync_waits_it_out(hub, monkeypatch):
    _draft(hub.db, "A")
    _patch_request(hub, monkeypatch, [
        Resp(429, {"X-Rate-Limit-Problem": "day", "Retry-After": "9000"}),
    ])
    results = hub.sync.maybe_sync(force=True)
    assert results and results[0]["ok"] is False
    assert results[0]["rate_limited"] and results[0]["daily"]
    assert "daily call allowance" in results[0]["error"]
    failures = hub.db.xero_recent_sync_failures()
    assert failures and "daily call allowance" in failures[0]["message"]
    assert _hub_status(hub.db, "A") == "PENDING_REVIEW"     # nothing flagged on failure

    # The lock-out is known: an un-forced sync makes NO call until it passes.
    monkeypatch.setattr(hub.client.requests, "request",
                        lambda *a, **k: pytest.fail("called Xero during lock-out"))
    hub.db.xero_kv_set("last_poll_at", "2000-01-01T00:00:00")
    assert hub.sync.maybe_sync() == []


# ---------------------------------------------------------------- lock + throttle

def test_only_one_sync_runs_at_a_time(hub, monkeypatch):
    fake = FakeXero()
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    hub.db.xero_kv_set(hub.sync.SYNC_LOCK_KEY, dt.datetime.utcnow().isoformat())
    assert hub.sync.maybe_sync(force=True) == [hub.sync.IN_PROGRESS]   # Sync now is told
    hub.db.xero_kv_set("last_poll_at", "2000-01-01T00:00:00")
    assert hub.sync.maybe_sync() == []                     # auto-sync just waits
    assert fake.calls == []

    stale = (dt.datetime.utcnow() - dt.timedelta(minutes=hub.sync.SYNC_LOCK_MINUTES + 1)).isoformat()
    hub.db.xero_kv_set(hub.sync.SYNC_LOCK_KEY, stale)       # crashed sync: ignored
    assert hub.sync.maybe_sync(force=True)
    assert hub.db.xero_kv_get(hub.sync.SYNC_LOCK_KEY) is None   # released afterwards


def test_last_poll_is_stamped_at_start_and_throttles_for_15_minutes(hub, monkeypatch):
    fake = FakeXero()
    monkeypatch.setattr(hub.client, "api_get", fake.api_get)
    assert hub.sync.POLL_INTERVAL_MINUTES == 15
    hub.sync.maybe_sync()
    first_calls = len(fake.calls)
    assert first_calls > 0
    assert hub.sync.maybe_sync() == [] and len(fake.calls) == first_calls
    old = (dt.datetime.utcnow() - dt.timedelta(minutes=16)).isoformat()
    hub.db.xero_kv_set("last_poll_at", old)
    assert hub.sync.maybe_sync()


def test_lock_released_even_when_sync_blows_up(hub, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(hub.client, "api_get", boom)
    results = hub.sync.sync_all()
    assert results[0]["ok"] is False and "kaboom" in results[0]["error"]
    assert hub.db.xero_kv_get(hub.sync.SYNC_LOCK_KEY) is None


# ---------------------------------------------------------------- Microsoft sign-in

def _ms_env(monkeypatch):
    for k in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("MS_SENDER_EMAIL", "ash@astonsaccountants.co.uk")


def test_expired_secret_is_recorded_and_explained(hub, monkeypatch):
    _ms_env(monkeypatch)
    monkeypatch.setattr(hub.watchdog.requests, "post", lambda *a, **k: Resp(
        401, body={"error": "invalid_client",
                   "error_description": "AADSTS7000222: The provided client secret keys "
                                        "for app '034efeac' are expired."}))
    with pytest.raises(RuntimeError) as exc:
        hub.watchdog._graph_token()
    assert "client secret has expired" in str(exc.value)
    status = hub.watchdog.ms_auth_status()
    assert status["state"] == "down" and "AADSTS7000222" in status["error"]
    first_since = status["since"]

    # a second failure keeps the ORIGINAL 'since'
    with pytest.raises(RuntimeError):
        hub.watchdog._graph_token()
    assert hub.watchdog.ms_auth_status()["since"] == first_since

    # send_email reports False and logs plainly, never raises
    assert hub.watchdog.send_email("s", "<p>b</p>") is False
    assert "client secret has expired" in hub.db.xero_recent_sync_failures()[0]["message"]

    # recovery clears the banner state
    monkeypatch.setattr(hub.watchdog.requests, "post",
                        lambda *a, **k: Resp(200, body={"access_token": "t"}))
    assert hub.watchdog._graph_token() == "t"
    assert hub.watchdog.ms_auth_status()["state"] == "ok"


def test_secret_expiry_warning_windows(hub, monkeypatch):
    today = dt.date(2026, 10, 17)
    monkeypatch.delenv("MS_SECRET_EXPIRES", raising=False)
    assert hub.watchdog.secret_expiry_warning(today) == {}
    monkeypatch.setenv("MS_SECRET_EXPIRES", "garbage")
    assert hub.watchdog.secret_expiry_warning(today) == {}
    monkeypatch.setenv("MS_SECRET_EXPIRES", "2026-12-31")
    assert hub.watchdog.secret_expiry_warning(today) == {}        # 75 days: quiet
    monkeypatch.setenv("MS_SECRET_EXPIRES", "2026-11-06")
    w = hub.watchdog.secret_expiry_warning(today)
    assert w["level"] == "warning" and w["days"] == 20 and "6 Nov 2026" in w["text"]
    monkeypatch.setenv("MS_SECRET_EXPIRES", "2026-10-17")
    assert hub.watchdog.secret_expiry_warning(today)["level"] == "error"
    monkeypatch.setenv("MS_SECRET_EXPIRES", "2026-10-15T00:00:00Z")   # datetime form ok
    e = hub.watchdog.secret_expiry_warning(today)
    assert e["level"] == "error" and "expired on 15 Oct 2026" in e["text"]


# ---------------------------------------------------------------- resend

def test_resend_approval_emails_since_a_date(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("BT", "Bradley", "bradley@astonsaccountants.co.uk")
    hub.db.xero_raiser_upsert("ZZ", "Nobody", None)
    for iid, pair in (("A", "BT"), ("B", "ZZ"), ("C", "BT")):
        _draft(hub.db, iid, number=f"2610000{iid}")
        hub.db.xero_mark_approved(iid, uid, "AA", b"%PDF-fake", f"FeeNote_{iid}.pdf",
                                  attachment_ok=True, raiser_pair=pair)
    # C was approved long ago: outside the window
    with hub.db.get_conn() as conn:
        conn.execute("UPDATE xero_drafts SET decided_at = '2026-01-01T00:00:00' "
                     "WHERE invoice_id = 'C'")
    sent = []
    monkeypatch.setattr(hub.watchdog, "send_email",
                        lambda subject, body, to=None, attachments=None:
                        sent.append((subject, tuple(to), attachments[0]["name"],
                                     "Re-sent" in body)) or True)

    result = hub.actions.resend_approval_emails("2026-10-01T00:00:00", uid)

    assert result["sent"] == ["2610000A"]
    assert result["skipped"] == ["2610000B"]               # raiser has no email
    assert result["failed"] == [] and result["considered"] == 2
    assert sent == [("Fee note 2610000A approved — ready to send",
                     ("bradley@astonsaccountants.co.uk",), "FeeNote_A.pdf", True)]


def test_resend_stops_at_first_failure(hub, monkeypatch):
    uid = hub.db.create_user("ash", "pw123456", "Ash", "approver")
    hub.db.xero_raiser_upsert("BT", "Bradley", "bradley@astonsaccountants.co.uk")
    for iid in ("A", "B", "C"):
        _draft(hub.db, iid, number=f"2610000{iid}")
        hub.db.xero_mark_approved(iid, uid, "AA", b"%PDF", f"FeeNote_{iid}.pdf",
                                  attachment_ok=True, raiser_pair="BT")
    monkeypatch.setattr(hub.watchdog, "send_email", lambda *a, **k: False)
    result = hub.actions.resend_approval_emails("2026-10-01T00:00:00", uid)
    assert result["failed"] == ["2610000A"]
    assert result["not_attempted"] == ["2610000B", "2610000C"]
    assert result["sent"] == []


# ---------------------------------------------------------------- backup retry

def test_backup_retries_hourly_after_a_failure(hub, monkeypatch):
    _ms_env(monkeypatch)
    monkeypatch.setattr(hub.backup, "BACKUP_AFTER_HOUR", 0)      # any hour is fine
    attempts = []

    def failing():
        attempts.append(1)
        raise RuntimeError("Microsoft sign-in refused")
    monkeypatch.setattr(hub.backup, "run_backup", failing)

    hub.backup.maybe_backup()
    hub.backup.maybe_backup()                                    # 5-minute tick: skipped
    assert len(attempts) == 1
    assert hub.db.xero_kv_get("backup_status").startswith("FAILED")

    hour_ago = (dt.datetime.utcnow() - dt.timedelta(minutes=61)).isoformat()
    hub.db.xero_kv_set("backup_last_failed_at", hour_ago)
    hub.backup.maybe_backup()
    assert len(attempts) == 2

    hub.db.xero_kv_set("backup_last_failed_at", hour_ago)
    monkeypatch.setattr(hub.backup, "run_backup",
                        lambda: {"filename": "astons_Fri.db", "bytes": 1024 * 1024})
    hub.backup.maybe_backup()
    assert hub.db.xero_kv_get("backup_last_failed_at") is None
    assert hub.db.xero_kv_get("backup_status").startswith("ok")
