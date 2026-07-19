# Astons Invoice Hub

Internal Astons Accountants tool with approval workflow. Team members raise fee notes in BrightManager, convert them to branded invoices here, and submit them to Ash for approval before sending to clients.

This is the v2 successor to the simpler `astons-invoice-generator`, which only did the conversion step. The Xero integration build (see `SPEC.md`) is turning the Hub into the single review/approval point for all fee notes.

## Xero integration (SPEC.md — Phase 1 built)

Phase 1 polls Xero for BrightManager-raised ACCREC drafts and shows them in a read-only **Xero queue** for the approver, with an **Exceptions** tab for drafts actioned outside the Hub and failed syncs. Approve/reject actions come in Phases 2–3.

Setup (in addition to the variables below):

1. Create a Xero app at developer.xero.com (auth code flow) with redirect URI set to the app's exact URL, and scopes `accounting.invoices`, `accounting.attachments`, `accounting.contacts.read`, `offline_access` (granular scopes — apps created after 2 Mar 2026 can't use the broad `accounting.transactions`).
2. Set env vars: `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`, `XERO_REDIRECT_URI` (must match the registered redirect URI exactly).
3. Log in as approver → **Xero settings** tab → Connect to Xero → authorise the org(s).
4. Tag each connected org as AA or CW in the same tab.

Polling runs automatically on approver page load, throttled to every 10 minutes (DB-backed, shared across sessions), plus a **Sync now** button. For polling independent of anyone having the app open, add a Railway cron service running `python xero_sync.py` on the same volume/env.

New files: `xero_client.py` (OAuth + API), `xero_sync.py` (poll engine).

## What's different from v1

- Username / password login with roles (team_member, approver)
- Persistent SQLite database storing every submission, approval, rejection and audit entry
- Team member submits for approval → Ash reviews → approves or rejects with a note
- Only approved invoices become downloadable for sending to the client
- "Mark as sent" button to track what's gone out
- Portfolio selector (A / C) with hardcoded bank details — no more parsing bank blocks from the BrightManager PDF

## Local development

```
pip install -r requirements.txt
set INITIAL_APPROVER_USERNAME=ash
set INITIAL_APPROVER_PASSWORD=changeme
set INITIAL_APPROVER_NAME=Ash Chandarana
streamlit run app.py
```

(On Mac/Linux use `export` instead of `set`.)

On first run the DB is created and the initial approver is seeded from the env vars. After first login, change the password via the Users tab.

## Railway deployment

The service needs:

1. **A persistent volume** mounted at `/data` (Railway dashboard → Service → Volumes → Create Volume → mount path `/data`). Without this, the database resets on every deploy.
2. **Environment variables:**
   - `DATABASE_PATH=/data/astons.db`
   - `INITIAL_APPROVER_USERNAME=ash`
   - `INITIAL_APPROVER_PASSWORD=<strong password>`
   - `INITIAL_APPROVER_NAME=Ash Chandarana`

The start command in `railway.json` binds Streamlit to port 8501; the Railway networking setting should route to that port.

## File layout

- `app.py` — Streamlit app, contains team and approver views
- `auth.py` — login / session / role gating
- `db.py` — SQLite access layer (users, invoices, audit log)
- `generate_invoice.py` — PDF parser and branded invoice generator (unchanged from v1 except for portfolio override)
- `astons_logo.png` — brand asset used in generated invoices
- `requirements.txt`, `railway.json`, `.gitignore`
