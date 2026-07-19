"""
Billing dashboard reporting (SPEC 4).

One unified entry stream feeds every view: Hub-approved Xero invoices
plus historical Bill Number List imports. Each entry carries per-person
net splits so sole vs shared credit can be shown either way.

All aggregation is Python-side — volumes (hundreds of fee notes a
month) don't justify SQL gymnastics.
"""

import calendar
import datetime as dt
import json

import db
import xero_attrib


def month_bounds(year: int, month: int):
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), (month % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def entries_for_range(start: str, end: str) -> list:
    """Unified fee-note entries with invoice date in [start, end).
    Each: {fee_note_no, client_name, client_code, entity, net, vat,
    gross, monthly, date, splits: [{initials, net, share}], source}."""
    entries = []
    for r in db.xero_approved_range(start, end):
        try:
            splits = json.loads(r.get("split_json") or "[]")
        except json.JSONDecodeError:
            splits = []
        entries.append({
            "fee_note_no": r["invoice_number"],
            "client_name": r["contact_name"],
            "client_code": r.get("client_code"),
            "entity": r.get("entity"),
            "net": float(r.get("sub_total") or 0),
            "vat": float(r.get("total_tax") or 0),
            "gross": float(r.get("total") or 0),
            "monthly": r.get("monthly"),
            "date": (r.get("date") or "")[:10],
            "splits": splits,
            "source": "hub",
            "xero_status": "AUTHORISED",
        })
    for r in db.billing_imports_range(start, end):
        raisers = xero_attrib.parse_reference(
            r.get("issued_by") or "",
            {x["initials"] for x in db.xero_raisers_all()},
        ) or ([r["issued_by"].strip().upper()] if (r.get("issued_by") or "").strip() else [])
        try:
            splits = json.loads(xero_attrib.compute_split(raisers, r.get("net")))
        except Exception:
            splits = []
        entries.append({
            "fee_note_no": r.get("fee_note_no"),
            "client_name": r.get("client_name"),
            "client_code": r.get("client_code"),
            "entity": r.get("entity"),
            "net": float(r.get("net") or 0),
            "vat": None,
            "gross": None,
            "monthly": r.get("monthly"),
            "date": (r.get("issued_on") or "")[:10],
            "splits": splits,
            "source": "import",
            "xero_status": "(imported)",
        })
    return entries


def per_person(entries: list) -> dict:
    """initials -> {net, sole_net, shared_net, count}."""
    people = {}
    for e in entries:
        for s in e["splits"]:
            p = people.setdefault(s["initials"],
                                  {"net": 0.0, "sole_net": 0.0,
                                   "shared_net": 0.0, "count": 0})
            amount = float(s.get("net") or 0)
            p["net"] += amount
            p["count"] += 1
            if float(s.get("share") or 1) >= 1:
                p["sole_net"] += amount
            else:
                p["shared_net"] += amount
    return people


def firm_split(entries: list) -> dict:
    aa = sum(e["net"] for e in entries if e["entity"] == "AA")
    cw = sum(e["net"] for e in entries if e["entity"] == "CW")
    untagged = sum(e["net"] for e in entries if e["entity"] not in ("AA", "CW"))
    return {"total": sum(e["net"] for e in entries),
            "aa": aa, "cw": cw, "entity_untagged": untagged,
            "count": len(entries)}


def twelve_month_trend(end_year: int, end_month: int) -> list:
    """[{month: 'Aug 2025', net, prior_year_net}] for the 12 months
    ending at (end_year, end_month)."""
    out = []
    y, m = end_year, end_month
    months = []
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for (yy, mm) in reversed(months):
        start, end = month_bounds(yy, mm)
        net = sum(e["net"] for e in entries_for_range(start, end))
        p_start, p_end = month_bounds(yy - 1, mm)
        prior = sum(e["net"] for e in entries_for_range(p_start, p_end))
        out.append({"month": dt.date(yy, mm, 1).strftime("%b %Y"),
                    "net": round(net, 2), "prior_year_net": round(prior, 2)})
    return out


def cumulative_curve(entries: list, year: int, month: int) -> list:
    """Cumulative net by day of month."""
    days_in_month = calendar.monthrange(year, month)[1]
    by_day = [0.0] * (days_in_month + 1)
    for e in entries:
        try:
            day = dt.date.fromisoformat(e["date"]).day
        except (ValueError, TypeError):
            continue
        by_day[day] += e["net"]
    cum, out = 0.0, []
    for day in range(1, days_in_month + 1):
        cum += by_day[day]
        out.append(round(cum, 2))
    return out


def average_prior_curve(year: int, month: int, lookback: int = 6) -> list:
    """Average cumulative curve (normalised to 31 slots by day index) of
    the prior `lookback` months — the 'spot a slow month by day 10'
    baseline (SPEC 4.2)."""
    curves = []
    y, m = year, month
    for _ in range(lookback):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        start, end = month_bounds(y, m)
        entries = entries_for_range(start, end)
        if entries:
            curves.append(cumulative_curve(entries, y, m))
    if not curves:
        return []
    longest = max(len(c) for c in curves)
    padded = [c + [c[-1]] * (longest - len(c)) for c in curves]
    return [round(sum(vals) / len(vals), 2) for vals in zip(*padded)]


def register_rows(entries: list) -> list:
    """SPEC 4.3 register columns."""
    return [{
        "Fee note": e["fee_note_no"],
        "Entity": e["entity"] or "",
        "Client code": e["client_code"] or "",
        "Client": e["client_name"] or "",
        "Net": round(e["net"], 2),
        "VAT": round(e["vat"], 2) if e["vat"] is not None else None,
        "Gross": round(e["gross"], 2) if e["gross"] is not None else None,
        "Issued by": "/".join(s["initials"] for s in e["splits"]),
        "Issued on": e["date"],
        "Xero status": e["xero_status"],
        "Source": e["source"],
    } for e in entries]
