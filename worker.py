"""
Standalone background worker, launched alongside Streamlit from the
container's start command. Runs from container BOOT — unlike a thread
started inside app.py, it does not wait for the first browser session,
so syncs, alert emails and the nightly backup happen even on a day
nobody opens the Hub.

Every part throttles itself in the shared SQLite DB (sync: 15 min,
watchdog: daily/weekly, backup: nightly), so the 5-minute tick is cheap
and safe even if a second worker ever runs.
"""

import time

import db


def main() -> None:
    for attempt in range(6):                 # the DB may be busy at boot
        try:
            db.init_db()
            break
        except Exception as exc:
            if attempt == 5:
                print("worker: init_db never succeeded; carrying on — Streamlit "
                      "creates the schema and each tick reports its own errors", flush=True)
                break
            print(f"worker: init_db failed ({exc}); retrying", flush=True)
            time.sleep(10)
    print("Hub worker started", flush=True)
    import xero_backup
    import xero_sync
    import xero_watchdog

    try:
        xero_sync.clear_sync_lock()      # a fresh container has no sync in flight
    except Exception as exc:
        print(f"worker: could not clear sync lock: {exc}", flush=True)
    try:
        # Probe Microsoft once so an expired/renewed secret shows (or clears)
        # on every page straight away instead of at the next email.
        state = xero_watchdog.check_ms_auth().get("state")
        print(f"worker: Microsoft sign-in at boot: {state}", flush=True)
    except Exception as exc:
        print(f"worker: Microsoft boot check failed: {exc}", flush=True)

    while True:
        try:
            xero_sync.maybe_sync()
        except Exception as exc:
            print(f"worker: sync failed: {exc}", flush=True)
        try:
            if xero_watchdog.maybe_recheck_ms_auth():
                state = xero_watchdog.ms_auth_status().get("state")
                print(f"worker: Microsoft sign-in re-checked: {state}", flush=True)
        except Exception as exc:
            print(f"worker: Microsoft re-check failed: {exc}", flush=True)
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
