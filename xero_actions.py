"""
Xero write actions for Invoice Hub — approve (SPEC 2.2) and reject
(SPEC 2.3).

Ordering is chosen so failures leave the cleanest possible state:
  - Everything fallible that DOESN'T touch Xero (re-fetch, contact
    lookup, PDF render) happens first — a failure there aborts with no
    Xero write at all.
  - The status change is the single point of no return. If the
    attachment upload fails after AUTHORISED succeeded, the draft is
    flagged APPROVED_NO_ATTACHMENT (never silent, retryable) per SPEC.

Every action is audit-logged with actor and before/after state.
"""

import db
import xero_client
import xero_pdf


class ActionBlocked(Exception):
    """The action cannot proceed (stale draft, bad state). The message
    is shown to the approver as-is."""


def _fetch_live_invoice(tenant_id: str, invoice_id: str) -> dict:
    data = xero_client.api_get(tenant_id, f"Invoices/{invoice_id}")
    invoices = (data or {}).get("Invoices", [])
    if not invoices:
        raise ActionBlocked("Invoice no longer exists in Xero.")
    return invoices[0]


def _require_still_draft(live: dict, invoice_id: str) -> None:
    status = live.get("Status")
    if status not in ("DRAFT", "SUBMITTED"):
        # Hub must never act on a non-draft (SPEC 2.3). Mark it so it
        # surfaces in exceptions rather than sitting stale in the queue.
        db.xero_mark_external_action(
            invoice_id,
            f"Actioned outside the Hub — Xero status is now {status}.",
        )
        raise ActionBlocked(
            f"This invoice is no longer a draft in Xero (status: {status}). "
            "It has been moved to the exceptions list."
        )


def fetch_contact(tenant_id: str, contact_id: str) -> dict:
    if not contact_id:
        return {}
    try:
        cdata = xero_client.api_get(tenant_id, f"Contacts/{contact_id}")
        contacts = (cdata or {}).get("Contacts", [])
        return contacts[0] if contacts else {}
    except xero_client.XeroRateLimited:
        raise                      # no point going on to the Xero write
    except xero_client.XeroApiError:
        return {}


def preview_pdf(invoice_id: str, entity: str) -> dict:
    """Render the branded fee note for a draft WITHOUT touching Xero's
    invoice state — used by the review screens so the PDF can be seen
    before approval. Returns {'pdf', 'filename', 'address_lines',
    'address_source'} so the UI can flag a missing client address."""
    import hub_addresses
    draft = db.xero_get_draft(invoice_id)
    if not draft:
        raise ActionBlocked("Draft not found in the Hub database.")
    contact = fetch_contact(draft["tenant_id"], draft.get("contact_id"))
    address = hub_addresses.resolve(draft.get("contact_name"), contact,
                                    draft.get("contact_id"))
    return {
        "pdf": xero_pdf.render_draft_pdf(draft, contact, entity,
                                         address_lines=address["lines"]),
        "filename": f"FeeNote_{draft['invoice_number'] or 'draft'}.pdf",
        "address_lines": address["lines"],
        "address_source": address["source"],
    }


def approve_draft(invoice_id: str, user_id: int, entity: str,
                  raisers: list = None, monthly: bool = None) -> dict:
    """Approve: AUTHORISED in Xero + branded PDF attached with
    IncludeOnline. Raiser attribution is mandatory (SPEC section 3).
    Returns {'attachment_ok': bool, 'error': str|None}."""
    if not raisers:
        raise ActionBlocked("Pick who raised this fee note before approving — "
                            "every invoice must be attributed.")

    draft = db.xero_get_draft(invoice_id)
    if not draft:
        raise ActionBlocked("Draft not found in the Hub database.")
    if draft["hub_status"] != "PENDING_REVIEW":
        raise ActionBlocked(f"Draft is not awaiting review (status: {draft['hub_status']}).")

    tenant_id = draft["tenant_id"]

    # 1. Re-fetch from Xero: confirm still a draft and get fresh data
    #    (amounts may have changed since the last poll — Xero is the
    #    source of truth, so re-sync before acting).
    live = _fetch_live_invoice(tenant_id, invoice_id)
    _require_still_draft(live, invoice_id)

    import xero_sync
    xero_sync._upsert_invoice(tenant_id, live)
    draft = db.xero_get_draft(invoice_id)

    # 2. Contact address for the fee note (accounting.contacts.read).
    #    Address is nice-to-have; the name is already on the draft.
    contact_id = draft.get("contact_id") or (live.get("Contact") or {}).get("ContactID")
    contact = fetch_contact(tenant_id, contact_id)

    # 3. Render the branded PDF BEFORE any Xero write, with the best
    #    known client address (address book -> Xero -> BrightManager).
    import hub_addresses
    address = hub_addresses.resolve(draft.get("contact_name"), contact,
                                    contact_id)
    filename = f"FeeNote_{draft['invoice_number']}.pdf"
    pdf_bytes = xero_pdf.render_draft_pdf(draft, contact, entity,
                                          address_lines=address["lines"])

    # 4. Point of no return: DRAFT -> AUTHORISED.
    xero_client.api_post(
        tenant_id,
        f"Invoices/{invoice_id}",
        {"InvoiceID": invoice_id, "Status": "AUTHORISED"},
    )

    # 5. Attach with IncludeOnline. Failure here flags, never hides.
    attachment_ok, error = True, None
    try:
        xero_client.api_put_attachment(tenant_id, invoice_id, filename, pdf_bytes)
    except Exception as exc:
        attachment_ok, error = False, str(exc)[:500]

    import xero_attrib
    db.xero_mark_approved(
        invoice_id, user_id, entity, pdf_bytes, filename,
        attachment_ok=attachment_ok, error=error,
        raiser_pair="/".join(raisers),
        split_json=xero_attrib.compute_split(raisers, draft.get("sub_total")),
        monthly=monthly,
        client_code=(contact.get("AccountNumber") or "").strip() or None,
    )

    # Tell the raiser(s) their fee note went through. Best-effort — a
    # send failure never affects the approval itself.
    notified = []
    emails = xero_attrib.raiser_emails(raisers)
    if emails and send_approval_email(draft, filename, pdf_bytes, emails):
        notified = emails
    # 'none' = an email was due but failed (resend candidate);
    # 'no-email' = nobody to tell (raiser has no address on file).
    outcome = ",".join(notified) or ("none" if emails else "no-email")
    db.record_xero_event(user_id, "xero_approve_notify",
                         f"xero_invoice_id={invoice_id} notified={outcome}")
    return {"attachment_ok": attachment_ok, "error": error,
            "invoice_number": draft["invoice_number"], "notified": notified,
            "emails_expected": bool(emails)}


def send_approval_email(draft: dict, filename: str, pdf_bytes: bytes,
                        emails: list, resend: bool = False) -> bool:
    """The 'fee note approved' email to the raiser(s), branded PDF attached.
    Used at approval time and by resend_approval_emails after an outage."""
    import html as html_mod
    import xero_watchdog
    number = draft.get("invoice_number") or ""
    body = (
        f"<p>Your fee note <b>{html_mod.escape(number)}</b> "
        f"for <b>{html_mod.escape(draft.get('contact_name') or '')}</b> "
        f"({'£' + format(draft.get('total') or 0, ',.2f')}) has been approved.</p>"
        f"<p>The branded fee note is attached — send it on to the "
        f"client. It is also on the invoice in Xero (and the client's "
        f"online invoice link).</p>"
    )
    if resend:
        body = ("<p><i>Re-sent: this notification could not be delivered when the "
                "fee note was approved.</i></p>" + body)
    return xero_watchdog.send_email(
        f"Fee note {number} approved — ready to send",
        body, to=emails,
        attachments=[{"name": filename, "bytes": pdf_bytes}],
    )


def unnotified_approvals(since_iso: str) -> list:
    """Fee notes approved since `since_iso` whose raisers never got the
    approval email (used to preview a resend): each with 'emails' and
    'can_send' (has raiser email AND a stored PDF)."""
    import xero_attrib
    out = []
    for d in db.xero_unnotified_approvals_since(since_iso, with_pdf=False):
        raisers = [r for r in (d.get("raiser_pair") or "").split("/") if r]
        d["emails"] = xero_attrib.raiser_emails(raisers)
        d["can_send"] = bool(d["emails"] and d.get("has_pdf"))
        out.append(d)
    return out


def resend_approval_emails(since_iso: str, user_id: int) -> dict:
    """Re-send the approval email (with the stored branded PDF) for every fee
    note approved on or after `since_iso` whose raisers were NOT emailed
    (per the audit trail) — for after an email outage. Never emails anyone
    twice: a successful resend is recorded as a notify event. Stops at the
    first failed send (the outage is probably still on) and reports what was
    sent, skipped (no raiser email / no stored PDF) and not attempted."""
    import xero_attrib
    sent, skipped, failed, not_attempted = [], [], [], []
    rows = db.xero_unnotified_approvals_since(since_iso)
    pending = list(rows)
    while pending:
        d = pending.pop(0)
        label = d["invoice_number"] or d["invoice_id"]
        raisers = [r for r in (d.get("raiser_pair") or "").split("/") if r]
        emails = xero_attrib.raiser_emails(raisers)
        if not emails or not d.get("branded_pdf"):
            skipped.append(label)
            if not emails:
                # Nobody to tell: record it so the preview stops listing it.
                # (A missing PDF is left open — a later attachment retry
                # stores one, and the raiser should then get the email.)
                db.record_xero_event(user_id, "xero_resend_notify",
                                     f"xero_invoice_id={d['invoice_id']} notified=no-email")
            continue
        ok = send_approval_email(d, d.get("branded_pdf_filename")
                                 or f"FeeNote_{d['invoice_number']}.pdf",
                                 d["branded_pdf"], emails, resend=True)
        if ok:
            sent.append(label)
            db.record_xero_event(user_id, "xero_resend_notify",
                                 f"xero_invoice_id={d['invoice_id']} "
                                 f"notified={','.join(emails)}")
        else:
            failed.append(label)
            not_attempted = [r["invoice_number"] or r["invoice_id"] for r in pending]
            break
    db.record_xero_event(
        user_id, "xero_resend_approval_emails",
        f"since={since_iso} sent={len(sent)} failed={len(failed)} "
        f"skipped={len(skipped)} not_attempted={len(not_attempted)}")
    return {"sent": sent, "skipped": skipped, "failed": failed,
            "not_attempted": not_attempted, "considered": len(rows)}


def retry_attachment(invoice_id: str, user_id: int) -> dict:
    """Re-upload the branded PDF for an APPROVED_NO_ATTACHMENT invoice."""
    draft = db.xero_get_draft(invoice_id)
    if not draft:
        raise ActionBlocked("Draft not found in the Hub database.")
    if draft["hub_status"] != "APPROVED_NO_ATTACHMENT":
        raise ActionBlocked("This invoice is not awaiting an attachment retry.")
    if not draft.get("branded_pdf"):
        raise ActionBlocked("No stored branded PDF to upload — contact support.")

    try:
        xero_client.api_put_attachment(
            draft["tenant_id"], invoice_id,
            draft["branded_pdf_filename"], draft["branded_pdf"],
        )
        db.xero_mark_attachment_retried(invoice_id, user_id, ok=True)
        return {"ok": True}
    except Exception as exc:
        db.xero_mark_attachment_retried(invoice_id, user_id, ok=False,
                                        error=str(exc)[:500])
        return {"ok": False, "error": str(exc)}


def reject_draft(invoice_id: str, user_id: int, reason: str) -> dict:
    """Reject: DRAFT -> DELETED in Xero, reason recorded. The team then
    amends and re-raises in BrightManager (SPEC 2.3)."""
    if not reason or not reason.strip():
        raise ActionBlocked("A rejection reason is required.")

    draft = db.xero_get_draft(invoice_id)
    if not draft:
        raise ActionBlocked("Draft not found in the Hub database.")
    if draft["hub_status"] != "PENDING_REVIEW":
        raise ActionBlocked(f"Draft is not awaiting review (status: {draft['hub_status']}).")

    tenant_id = draft["tenant_id"]
    live = _fetch_live_invoice(tenant_id, invoice_id)
    _require_still_draft(live, invoice_id)

    xero_client.api_post(
        tenant_id,
        f"Invoices/{invoice_id}",
        {"InvoiceID": invoice_id, "Status": "DELETED"},
    )
    db.xero_mark_rejected(invoice_id, user_id, reason.strip())

    # Notify the raiser (SPEC 2.3). The team sets "raised by" at prep,
    # so the stored raiser_pair is the source of truth; parsing the
    # reference is only the fallback for unprepped drafts. Best-effort:
    # a send failure never undoes the reject.
    import html as html_mod
    import xero_attrib
    import xero_watchdog
    notified = []
    raisers = ([i for i in (draft.get("raiser_pair") or "").split("/") if i]
               or xero_attrib.parse_reference(draft.get("reference")))
    emails = xero_attrib.raiser_emails(raisers)
    if emails:
        body = (
            f"<p>Your fee note <b>{html_mod.escape(draft['invoice_number'] or '')}</b> "
            f"for <b>{html_mod.escape(draft['contact_name'] or '')}</b> "
            f"({'£' + format(draft['total'] or 0, ',.2f')}) was rejected in the "
            f"Invoice Hub.</p>"
            f"<p><b>Reason:</b> {html_mod.escape(reason.strip())}</p>"
            f"<p>The draft has been deleted in Xero. <b>BrightManager still "
            f"holds the old invoice</b> — delete or amend it in BM, then "
            f"re-raise.</p>"
        )
        if xero_watchdog.send_email(
            f"Fee note {draft['invoice_number']} rejected — please re-raise",
            body, to=emails,
        ):
            notified = emails
    outcome = ",".join(notified) or ("none" if emails else "no-email")
    db.record_xero_event(user_id, "xero_reject_notify",
                         f"xero_invoice_id={invoice_id} notified={outcome}")
    return {"invoice_number": draft["invoice_number"], "notified": notified,
            "emails_expected": bool(emails)}
