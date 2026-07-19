# Invoice Hub — Xero Integration & Billing Dashboard Spec

## Instructions to Claude Code (read first)

You are extending the existing Astons **Invoice Hub** app. This document is the single source of truth for the build — keep it in the repo root as `SPEC.md` and refer back to it every session.

**Build order — do NOT build everything at once. Complete, test, and get Ash's sign-off on each phase before starting the next:**

| Phase | Sections | Deliverable |
|---|---|---|
| 1 | §2.1 | Xero draft polling + review queue in the Hub |
| 2 | §2.2 | Approve action (AUTHORISED + branded PDF attached + IncludeOnline) — test on one real draft |
| 3 | §2.3 | Reject action (DELETED + raiser notification) — Ash runs the BM wrinkle test before sign-off |
| 4 | §2.4 | Orphaned-draft watchdog + exceptions list |
| 5 | §3 | Raiser attribution + shared-credit splits |
| 6 | §4 | Billing dashboard + targets + historical import (§6.2) |
| 7 | §7–8 | WIP vs billed, anomaly flags |

**Working rules:**
- At the start of every session: re-read this spec and state which phase we're on and what remains.
- Never invent behaviour not in the spec — if something is ambiguous, ask Ash before coding.
- Every Xero write action (approve, reject, attach) must be logged with timestamp, actor, and before/after state, and must fail loudly into the exceptions list — never silently.
- All amounts come from Xero. The Hub never edits invoice amounts — it reviews, brands, approves, rejects, and reports.
- Use sandbox/test data until Ash explicitly approves go-live per phase.
- British spelling throughout the UI. Currency £, format #,##0.00.

**Ash will provide:** Xero app client ID/secret (scopes in §2), charge-out rates per person (§7.1), monthly billing targets per person (§4.1), the historical Bill Number List workbook (§6.2), and answers to the BrightManager API questions (§7.1).

---

**Goal:** Invoice Hub becomes the single review/approval point for all fee notes. BrightManager raises → Xero draft → Hub reviews, brands, approves/rejects, attaches, tracks billing by team member vs target. Retires the manual Bill Number List spreadsheet entirely.

---

## 1. Pipeline overview

```
BrightManager (raise + time)
        │  pushes draft
        ▼
Xero (single source of truth for amounts)
        │  Hub polls drafts via API
        ▼
Invoice Hub review queue
        ├─ APPROVE → set AUTHORISED in Xero + generate branded PDF + attach to Xero invoice (include with online invoice) + log to billing tracker
        └─ REJECT  → set DELETED in Xero + notify raiser with reason → team amends & re-raises in BM
```

No manual entry point into the Hub. Everything arrives automatically via the Xero poll.

---

## 2. Xero API integration

**App scopes:** `accounting.transactions`, `accounting.attachments`, `accounting.contacts.read`, `offline_access`. OAuth 2.0 with refresh token; store tenant ID for the org (and second tenant if AA and CW are separate Xero orgs — see §5).

### 2.1 Draft ingestion (poll)
- `GET /Invoices?Statuses=DRAFT,SUBMITTED&where=Type=="ACCREC"` every 10–15 minutes (use `If-Modified-Since` to fetch deltas only).
- Upsert into Hub DB keyed on `InvoiceID`. Store: InvoiceNumber, Reference, Contact, LineItems, SubTotal, TotalTax, Total, Date, DueDate, UpdatedDateUTC, BrandingThemeID.
- New drafts land in the **Review Queue** with status `PENDING_REVIEW`.
- If a draft disappears from Xero between polls (deleted/authorised outside the Hub), mark it `EXTERNAL_ACTION` and surface in an exceptions list — nothing vanishes silently.

### 2.2 Approve
- Single action in Hub UI. Sequence:
  1. `POST /Invoices` — update status `DRAFT → AUTHORISED`.
  2. Generate branded PDF (existing ReportLab/HTML renderer).
  3. `PUT /Invoices/{InvoiceID}/Attachments/{FeeNote_<InvoiceNumber>.pdf}` then set `IncludeOnline=true` (`POST` to the attachment with the flag) so the client's online invoice link and Xero emails carry the branded PDF.
  4. Log approval: approver, timestamp, before/after snapshot.
- All-or-nothing: if any step fails, roll back UI state, retry queue with exponential backoff, and flag in exceptions. Never leave "authorised but no attachment" without a visible flag.

### 2.3 Reject
- Requires a reason (short free text / picklist: wrong amount, wrong client, wrong narrative, duplicate, other).
- `POST /Invoices` — update status `DRAFT → DELETED`. (Only drafts/submitted can be deleted; the Hub must never let an authorised invoice be "rejected" — offer VOID as a separate, partner-only action with confirmation.)
- Notify the raiser (email/Teams) with invoice details + reason. Status in Hub: `REJECTED — awaiting re-raise`.
- **BM wrinkle test (do before go-live):** delete a test draft via API and check whether BrightManager still shows it as pending / errors on sync. If BM holds a stale link, document the manual BM-side tidy step in the reject notification.

### 2.4 Orphaned-draft watchdog
- Daily sweep: any Xero draft with no Hub decision after **3 days** → alert to Ash + raiser. After **7 days** → escalation flag on dashboard.
- Weekly digest email: count of pending, average time-to-approval, oldest outstanding draft.

---

## 3. Raiser identification & billing attribution

Attribution priority order:
1. **BM record match** — match Xero draft to BrightManager invoice (via invoice number/reference) and read the raiser from BM. (If BM's API/export allows; else →)
2. **Xero Reference / Tracking Category** — team enters their initials (or "AB/CD" for shared credit) in the invoice Reference when raising in BM. Hub parses it. Mirrors current spreadsheet convention (e.g. `LG/BT`, `DP/RD`).
3. **Fallback** — unattributed drafts appear in the review screen with a mandatory "raised by" picker; approval is blocked until set. Guarantees 100% attribution with zero extra admin in the happy path.

**Shared credit rule (configurable):** `AB/CD` splits net fee 50/50 by default; per-pair overrides allowed. Store both the split amounts and the raw pair so reporting can show either view.

**Data captured per invoice:** raiser(s) + split, net fee, VAT, gross, entity (AA/CW), Monthly vs Non-Monthly flag, client code, issue date, approval date, approver, days-to-approve.

**Monthly vs Non-Monthly flag:** derive from BM (recurring vs one-off) if available; else from branding theme/reference convention; else a one-click toggle at review. Preserves the current spreadsheet's key split.

---

## 4. Billing dashboard

Replaces the Bill Number List spreadsheet (80+ manual monthly tabs).

### 4.1 Per-person view (monthly)
- Billed-to-date (net) vs **monthly target** per team member — targets set/edited by Ash in a settings screen, with effective-from dates so history is preserved.
- RAG status: green ≥ on-pace, amber within 15% of pace, red behind.
- **Run-rate projection:** "at current pace, finishes month at £X (Y% of target)."
- Split view: sole credit vs shared credit contribution.
- Drill-down: click a person → list of their fee notes for the month.

### 4.2 Firm view
- Firm total vs firm monthly target; Monthly vs Non-Monthly split; AA vs CW split.
- 12-month trend chart (net billed per month, vs same month prior year).
- Cumulative in-month curve vs prior months' average curve (spot a slow month by day 10, not day 31).

### 4.3 Registers & exports
- Fee note register per month (replicates spreadsheet columns: number, entity, client code, net, VAT, gross, issued by, issued on, Xero status) — filterable, CSV export.
- Exceptions tab: orphaned drafts, unattributed invoices, failed API actions, external actions.
- **Reconciliation check (automated):** sum of Hub-logged approved invoices vs Xero authorised invoices for the month. Any variance flagged — the "Check ≠ 0" problem becomes an automatic alert instead of a manual hunt.

### 4.4 Access
- Ash: everything incl. targets and reject/void.
- Managers (optional later): own numbers only, or team view — configurable.

---

## 5. Entity handling (AA / CW)

- If AA and CW are **two Xero orgs**: two tenant connections, unified queue with an entity badge; dual bank-detail routing already in the PDF renderer keys off entity.
- If **one org** with two branding themes/bank accounts: derive entity from branding theme or account code; tag accordingly.
- All reporting filterable by entity; targets can be per-entity or combined.

---

## 6. Migration & go-live

1. Team rule from day one of build: **all fee notes originate in BrightManager** — Word/Dext leg stops immediately.
2. Import historical spreadsheet (at minimum FY-to-date; ideally all tabs) into the Hub DB so trend charts and prior-year comparisons work from launch. One-off Python import script; the workbook structure is consistent enough to parse (fee note no., entity, client code, net, issued by, issued on).
3. Parallel-run the spreadsheet for one month against the dashboard; reconcile; then retire the spreadsheet.
4. Test plan before production:
   - Approve happy path (status change + attachment + online-invoice flag visible to a test client contact).
   - Reject/delete path + BM behaviour check (§2.3).
   - Attribution parsing for all initials-pair formats in current use.
   - Rate limits: Xero allows 60 calls/min, 5,000/day per tenant — the poll + batch actions fit comfortably; implement 429 back-off anyway.

---

## 7. Phase 7 — WIP vs billed & anomaly flags

### 7.1 WIP vs billed (recovery tracking)
**Concept:** per fee note and per person, compare *time cost* (hours in BrightManager × charge-out rate) with *fee billed*. Recovery rate = billed ÷ WIP. Surfaces write-offs and margin leakage.

**Time data source — build in this priority order:**
1. **BrightManager API** — if time entries per client/job are exposed on Astons' plan, pull on a schedule and match to fee notes by client code. (Ash to confirm API coverage with Bright support before this phase starts.)
2. **Scheduled CSV export** — Hub watches a folder / mailbox for BM time-report exports (weekly) and parses them. Robust parser: tolerate column reordering, flag unmatched client codes into exceptions.
3. If neither is viable, defer this feature — do not build a manual keying screen.

**Config:** charge-out rate per person (settings screen, effective-from dates). If BM holds rates and the API exposes them, prefer BM as source.

**Reporting:** recovery % per fee note (shown at review), per person per month, per client rolling 12 months. Flag recovery <85% amber, <70% red (thresholds configurable).

### 7.2 Anomaly flags at review
Checks run against the **Hub's own invoice database** (populated by the pipeline + the historical import in §6.2) — no live Xero queries needed. Badges appear on each draft in the review queue before Ash opens it.

| Flag | Rule | Severity |
|---|---|---|
| Fee changed | Monthly client's net fee differs from their previous monthly fee note | Amber, show delta |
| Possible duplicate | Same client code + same net amount within 30 days | Red |
| Below prior year | Non-monthly fee note lower than the comparable fee note ~12 months prior (same client, similar narrative/service) | Amber, show both figures |
| First bill | No history for this client code | Info |
| Round-number drift | Net fee not matching BM's recorded recurring fee for the client (if BM data available) | Amber |

Clean invoices show no badges — review should take seconds. All flag events logged so Ash can see how many errors were caught per month per raiser.

## 7b. Other nice-to-haves (build only when asked)
- **One-tap approve from phone:** review queue is mobile-friendly; approve on the train.
- **Auto-chase hooks:** approved invoices unpaid at +30 days feed a chase list (Xero invoice reminders or GoCardless status, which the onboarding app already monitors).
- **Monthly board pack export:** one-click PDF of the firm dashboard for the month-end file.

---

## 8. Definition of done

- Zero Word/Dext fee notes.
- One approval action updates Hub + Xero + attachment atomically.
- No draft can sit unactioned >3 days without an alert.
- 100% of invoices attributed to a raiser.
- Billing spreadsheet retired; dashboard reconciles to Xero to the penny automatically.
