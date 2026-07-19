"""
Anomaly flags at review (SPEC 7.2).

Checks run against the Hub's own records (approved invoices + the
historical import) — no live Xero queries. Badges appear on each draft
in the review queue; clean invoices show nothing, so review takes
seconds.

Flags:
  duplicate    RED    same client + same net within 30 days
  below_prior  AMBER  fee lower than the comparable fee ~12 months
                      prior (both figures shown)
  first_bill   INFO   no history for this client

The monthly/non-monthly distinction was dropped at Ash's request (all
fee notes are ad hoc), which also removes the spec's monthly
'fee changed' rule. 'Round-number drift' (vs BM's recorded recurring
fee) needs BM data and waits for Phase 7.1's BM access.
"""

import datetime as dt
import re

import xero_reports

HISTORY_DAYS = 460  # ~15 months, covers the prior-year window


def _norm_client(name: str) -> str:
    if not name:
        return ""
    text = re.sub(r"[^a-z0-9 ]", "", name.lower())
    text = re.sub(r"\b(ltd|limited|llp)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def load_history(today: dt.date = None) -> list:
    today = today or dt.date.today()
    start = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    end = (today + dt.timedelta(days=1)).isoformat()
    return xero_reports.entries_for_range(start, end)


def flags_for_draft(draft: dict, history: list) -> list:
    """[{level: 'red'|'amber'|'info', label, detail}] for one pending
    draft. `history` comes from load_history() — pass it in so the
    queue computes it once."""
    flags = []
    client = _norm_client(draft.get("contact_name"))
    if not client:
        return flags
    net = round(float(draft.get("sub_total") or 0), 2)
    try:
        draft_date = dt.date.fromisoformat((draft.get("date") or "")[:10])
    except ValueError:
        draft_date = dt.date.today()

    mine = [e for e in history
            if _norm_client(e.get("client_name")) == client
            and e.get("fee_note_no") != draft.get("invoice_number")]
    if not mine:
        flags.append({"level": "info", "label": "First bill",
                      "detail": "No history for this client in the Hub."})
        return flags

    # Possible duplicate: same net within 30 days (SPEC: red)
    for e in mine:
        try:
            gap = abs((draft_date - dt.date.fromisoformat(e["date"])).days)
        except (ValueError, TypeError):
            continue
        if gap <= 30 and abs(float(e["net"]) - net) <= 0.01:
            flags.append({
                "level": "red", "label": "Possible duplicate",
                "detail": (f"{e['fee_note_no']} for the same client at "
                           f"£{net:,.2f} was billed {gap} day(s) "
                           f"{'earlier' if gap else 'ago'}."),
            })
            break

    # Below prior year: vs comparable fee ~12 months ago
    prior_year = []
    for e in mine:
        try:
            months_back = (draft_date - dt.date.fromisoformat(e["date"])).days / 30.4
        except (ValueError, TypeError):
            continue
        if 10 <= months_back <= 14:
            prior_year.append(e)
    if prior_year:
        comparable = max(prior_year, key=lambda e: e.get("date") or "")
        prior_net = round(float(comparable["net"]), 2)
        if net < prior_net - 0.01:
            flags.append({
                "level": "amber", "label": "Below prior year",
                "detail": (f"£{net:,.2f} vs £{prior_net:,.2f} "
                           f"({comparable['fee_note_no']}, "
                           f"{comparable['date']}) a year ago."),
            })

    return flags


def summarise(flags: list) -> str:
    return "; ".join(f"{f['label']}" for f in flags) or "clean"
