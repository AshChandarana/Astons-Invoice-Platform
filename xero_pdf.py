"""
Branded fee-note PDF generation from Xero draft data (SPEC 2.2).

Reuses the existing ReportLab renderer (generate_invoice.py) by building
the same data dict it expects, but sourced from the Hub's synced Xero
draft instead of a parsed BrightManager PDF. Amounts come straight from
Xero — the Hub never edits them.
"""

import datetime as dt
import json
import os
import tempfile

from generate_invoice import generate_branded_invoice, apply_portfolio

# Which portfolio's bank details print on each entity's fee notes.
# AA = original Astons practice (A-portfolio), CW = acquired practice
# (C-portfolio). Confirmed mapping is surfaced in the approve UI so the
# bank account is visible before anything is authorised.
ENTITY_TO_PORTFOLIO = {"AA": "A", "CW": "C"}


def fmt_amount(value) -> str:
    """Xero decimal -> '1,234.00' (renderer adds the £)."""
    return f"{float(value or 0):,.2f}"


def fmt_date_long(iso: str) -> str:
    """'2026-07-01T00:00:00' -> '1 July 2026' (BrightManager style)."""
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B %Y')}"


def contact_address_lines(contact: dict) -> list:
    """Pick the best address from a Xero contact: POBOX (postal) first,
    then STREET. Returns display lines, possibly empty."""
    addresses = (contact or {}).get("Addresses", [])
    best = None
    for want in ("POBOX", "STREET"):
        for a in addresses:
            if a.get("AddressType") == want and any(
                a.get(k) for k in ("AddressLine1", "City", "PostalCode")
            ):
                best = a
                break
        if best:
            break
    if not best:
        return []
    lines = []
    for key in ("AddressLine1", "AddressLine2", "AddressLine3", "AddressLine4",
                "City", "Region", "PostalCode"):
        v = (best.get(key) or "").strip()
        if v:
            lines.append(v)
    return lines


def derive_entity(draft: dict, entity_map: list) -> str:
    """Derive AA/CW for a draft from its line items' tracking options and
    account codes using the configured mapping. Returns 'AA', 'CW', or
    None when nothing matches or the matches conflict."""
    lookup = {(m["match_type"], m["match_value"]): m["entity"] for m in entity_map}
    found = set()
    try:
        line_items = json.loads(draft.get("line_items_json") or "[]")
    except json.JSONDecodeError:
        return None
    for li in line_items:
        code = str(li.get("AccountCode") or "").strip()
        if code and ("account", code) in lookup:
            found.add(lookup[("account", code)])
        for t in li.get("Tracking", []) or []:
            opt = str(t.get("Option") or "").strip()
            if opt and ("tracking", opt) in lookup:
                found.add(lookup[("tracking", opt)])
    return found.pop() if len(found) == 1 else None


def draft_signals(draft: dict) -> dict:
    """Distinct tracking options and account codes on a draft — used by
    the settings screen to offer mapping choices."""
    tracking, accounts = set(), set()
    try:
        line_items = json.loads(draft.get("line_items_json") or "[]")
    except json.JSONDecodeError:
        return {"tracking": [], "accounts": []}
    for li in line_items:
        code = str(li.get("AccountCode") or "").strip()
        if code:
            accounts.add(code)
        for t in li.get("Tracking", []) or []:
            opt = str(t.get("Option") or "").strip()
            if opt:
                tracking.add(opt)
    return {"tracking": sorted(tracking), "accounts": sorted(accounts)}


def build_invoice_data(draft: dict, contact: dict, entity: str,
                       address_lines: list = None) -> dict:
    """Assemble the data dict generate_branded_invoice expects.
    address_lines overrides the Xero contact address when provided (the
    address book / BrightManager resolution in hub_addresses)."""
    try:
        line_items = json.loads(draft.get("line_items_json") or "[]")
    except json.JSONDecodeError:
        line_items = []

    invoice_date = fmt_date_long(draft.get("date"))
    data = {
        "invoice_no": draft.get("invoice_number") or "",
        "date": invoice_date,
        # Astons terms are "Upon presentation": due date = invoice date,
        # same business rule as the upload workflow.
        "due_date": invoice_date,
        "client_name": draft.get("contact_name") or "",
        "client_address": (address_lines if address_lines is not None
                           else contact_address_lines(contact)),
        "line_items": [
            {
                "description": li.get("Description") or "(no description)",
                "amount": f"£{fmt_amount(li.get('LineAmount'))}",
            }
            for li in line_items
        ],
        "subtotal": fmt_amount(draft.get("sub_total")),
        "vat": fmt_amount(draft.get("total_tax")),
        "total": fmt_amount(draft.get("total")),
        "vat_reg": "361419995",
        "vat_reg_formatted": "GB 361 4199 95",
    }
    data["description"] = [i["description"] for i in data["line_items"]]
    apply_portfolio(data, ENTITY_TO_PORTFOLIO[entity])
    return data


def render_draft_pdf(draft: dict, contact: dict, entity: str,
                     address_lines: list = None) -> bytes:
    """Render the branded fee note for a Xero draft and return the PDF
    bytes. Raises on any problem — nothing is written to Xero by this
    function, so a failure here aborts an approval cleanly."""
    if entity not in ENTITY_TO_PORTFOLIO:
        raise ValueError(f"Unknown entity '{entity}' — must be AA or CW.")
    if not draft.get("invoice_number"):
        raise ValueError("Draft has no invoice number — cannot brand a fee note.")
    data = build_invoice_data(draft, contact, entity, address_lines=address_lines)
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "feenote.pdf")
        generate_branded_invoice(data, out_path)
        with open(out_path, "rb") as fh:
            return fh.read()
