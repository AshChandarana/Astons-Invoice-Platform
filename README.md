# Astons Invoice Platform

Internal Astons Accountants tool with approval workflow. Team members raise fee notes in BrightManager, convert them to branded invoices here, and submit them to Ash for approval before sending to clients.

This is the v2 successor to the simpler `astons-invoice-generator`, which only did the conversion step.

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
