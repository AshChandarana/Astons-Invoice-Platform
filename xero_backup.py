"""
Nightly database backup to SharePoint.

The Hub's SQLite database is the firm's billing record — one file on
one Railway volume. Each night (first background tick after 02:00 UK) a
consistent snapshot is uploaded to the Aston-FirmOperations SharePoint
site under "InvoiceHub Backups", named by weekday (astons_Mon.db ...
astons_Sun.db) so seven rolling copies exist with zero cleanup.

Uses the same Graph app-only credentials as email (Sites.ReadWrite.All
is already granted). Status is stored in xero_kv for the settings
screen; failures also log to xero_sync_log (Exceptions tab).
"""

import datetime as dt
import os
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

import requests

import db

UK_TZ = ZoneInfo("Europe/London")
GRAPH = "https://graph.microsoft.com/v1.0"
SP_HOST = os.environ.get("SP_HOSTNAME", "astonsaccountants.sharepoint.com")
SP_SITE_PATH = os.environ.get("SP_BACKUP_SITE_PATH", "/sites/Aston-FirmOperations")
SP_FOLDER = os.environ.get("SP_BACKUP_FOLDER", "InvoiceHub Backups")
BACKUP_AFTER_HOUR = 2  # first tick after 02:00 UK
CHUNK = 5 * 1024 * 1024


def configured() -> bool:
    import xero_watchdog
    return xero_watchdog.email_configured()  # same Graph credentials


def _token() -> str:
    import xero_watchdog
    return xero_watchdog._graph_token()


def _snapshot_db(dest_path: str) -> None:
    """Consistent SQLite snapshot even while the app is writing."""
    src = sqlite3.connect(db.db_path())
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def run_backup() -> dict:
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}

    site = requests.get(
        f"{GRAPH}/sites/{SP_HOST}:{SP_SITE_PATH}", headers=headers, timeout=30)
    site.raise_for_status()
    site_id = site.json()["id"]

    weekday = dt.datetime.now(UK_TZ).strftime("%a")
    filename = f"astons_{weekday}.db"

    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, filename)
        _snapshot_db(snap)
        size = os.path.getsize(snap)

        session = requests.post(
            f"{GRAPH}/sites/{site_id}/drive/root:/{SP_FOLDER}/{filename}:"
            "/createUploadSession",
            headers=headers,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=30,
        )
        session.raise_for_status()
        upload_url = session.json()["uploadUrl"]

        with open(snap, "rb") as fh:
            offset = 0
            while offset < size:
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                resp = requests.put(
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    data=chunk,
                    timeout=120,
                )
                resp.raise_for_status()
                offset += len(chunk)

    return {"filename": filename, "bytes": size}


def maybe_backup() -> None:
    """Once per UK day, first call after 02:00. Cheap no-op otherwise."""
    if not configured():
        return
    now = dt.datetime.now(UK_TZ)
    if now.hour < BACKUP_AFTER_HOUR:
        return
    today = now.date().isoformat()
    if db.xero_kv_get("backup_last_date") == today:
        return
    try:
        result = run_backup()
        db.xero_kv_set("backup_last_date", today)
        db.xero_kv_set(
            "backup_status",
            f"ok — {result['filename']} ({result['bytes'] / 1024 / 1024:.1f} MB) "
            f"uploaded {now.strftime('%d %b %Y %H:%M')}",
        )
        db.record_xero_event(None, "db_backup", result["filename"])
    except Exception as exc:
        db.xero_kv_set("backup_status", f"FAILED {now.strftime('%d %b %H:%M')}: {exc}"[:300])
        db.xero_log_sync(None, ok=False, fetched=0,
                         message=f"SharePoint backup failed: {exc}"[:500])
