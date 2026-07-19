"""
Automated reconciliation (SPEC 4.3): the Hub's record of a month vs
Xero's authorised invoices for the same month. Turns the spreadsheet's
manual "Check <> 0" hunt into a listed variance.
"""

import db
import xero_client
import xero_reports

PAGE_SIZE = 100


def xero_invoices_for_month(year: int, month: int) -> list:
    """All AUTHORISED/PAID ACCREC invoices dated in the month, across
    every connected tenant. Uses the filtered list endpoint with paging
    (line items not needed — summary fields only)."""
    start, end = xero_reports.month_bounds(year, month)
    where = (
        f'Type=="ACCREC" AND Date >= DateTime({year},{month:02d},01) '
        f'AND Date < DateTime({end[:4]},{int(end[5:7]):02d},01)'
    )
    out = []
    for conn in db.xero_list_connections():
        page = 1
        while True:
            data = xero_client.api_get(
                conn["tenant_id"], "Invoices",
                {"Statuses": "AUTHORISED,PAID", "where": where, "page": page},
            )
            invoices = (data or {}).get("Invoices", [])
            for inv in invoices:
                out.append({
                    "invoice_number": inv.get("InvoiceNumber") or "",
                    "contact": (inv.get("Contact") or {}).get("Name", ""),
                    "net": float(inv.get("SubTotal") or 0),
                    "total": float(inv.get("Total") or 0),
                })
            if len(invoices) < PAGE_SIZE:
                break
            page += 1
    return out


def reconcile_month(year: int, month: int) -> dict:
    """Compare Hub-logged fee notes vs Xero authorised invoices for the
    month. Any variance is listed, never just netted off."""
    start, end = xero_reports.month_bounds(year, month)
    hub_entries = xero_reports.entries_for_range(start, end)
    hub_by_no = {e["fee_note_no"]: e for e in hub_entries if e["fee_note_no"]}
    xero_invs = xero_invoices_for_month(year, month)
    xero_by_no = {i["invoice_number"]: i for i in xero_invs if i["invoice_number"]}

    only_in_xero = [i for n, i in sorted(xero_by_no.items()) if n not in hub_by_no]
    only_in_hub = [e for n, e in sorted(hub_by_no.items()) if n not in xero_by_no]
    amount_mismatch = [
        {"invoice_number": n, "hub_net": hub_by_no[n]["net"],
         "xero_net": xero_by_no[n]["net"]}
        for n in sorted(set(hub_by_no) & set(xero_by_no))
        if abs(hub_by_no[n]["net"] - xero_by_no[n]["net"]) > 0.01
    ]

    hub_total = round(sum(e["net"] for e in hub_entries), 2)
    xero_total = round(sum(i["net"] for i in xero_invs), 2)
    return {
        "hub_total_net": hub_total,
        "xero_total_net": xero_total,
        "variance": round(xero_total - hub_total, 2),
        "only_in_xero": only_in_xero,
        "only_in_hub": only_in_hub,
        "amount_mismatch": amount_mismatch,
        "clean": (abs(xero_total - hub_total) <= 0.01
                  and not only_in_xero and not only_in_hub and not amount_mismatch),
    }
