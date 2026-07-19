"""
Standalone background worker, launched alongside Streamlit from the
container's start command. Runs from container BOOT — unlike a thread
started inside app.py, it does not wait for the first browser session,
so syncs, alert emails and the nightly backup happen even on a day
nobody opens the Hub.

Every part throttles itself in the shared SQLite DB (sync: 10 min,
watchdog: daily/weekly, backup: nightly), so the 5-minute tick is cheap
and safe even if a second worker ever runs.
"""

import time

import db


def main() -> None:
    db.init_db()
    print("Hub worker started", flush=True)
    import xero_backup
    import xero_sync
    import xero_watchdog

    while True:
        try:
            xero_sync.maybe_sync()
        except Exception as exc:
            print(f"worker: sync failed: {exc}", flush=True)
        try:
            xero_watchdog.maybe_run()
        except Exception as exc:
            print(f"worker: watchdog failed: {exc}", flush=True)
        try:
            xero_backup.maybe_backup()
        except Exception as exc:
            print(f"worker: backup failed: {exc}", flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
