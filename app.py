"""
Astons Invoice Platform (v2) — Streamlit app.

Workflow:
  - Team members log in, upload BrightManager PDFs, pick a portfolio,
    generate branded invoices, and submit them for approval.
  - The approver (Ash) sees a queue of pending invoices, reviews each,
    and approves or rejects with a note.
  - Approved invoices become available to the submitting team member
    for download and sending to the client. They can then mark them
    as sent.
"""

import io
import json
import os
import re
import tempfile
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from datetime import timezone
from zoneinfo import ZoneInfo

import streamlit as st

import db
import auth
import xero_client
import xero_sync
import xero_actions
import xero_anomaly
import xero_attrib
import xero_pdf
import xero_recon
import xero_reports
import xero_watchdog
import billing_import
import hub_addresses

UK_TZ = ZoneInfo("Europe/London")
from generate_invoice import (
    PORTFOLIOS,
    parse_brightmanager_pdf,
    generate_branded_invoice,
    apply_portfolio,
    validate_invoice_data,
)


STATUS_LABELS = {
    "pending_approval": "pending approval",
    "approved": "approved",
    "rejected": "rejected",
    "sent": "sent",
}


# === PAGE CONFIG ===
st.set_page_config(
    page_title="Astons Invoice Hub",
    page_icon="astons_logo.png",
    layout="wide",
    initial_sidebar_state="expanded",
)


ASTONS_DARK_GREEN = "#1a5c2e"
ASTONS_MID_GREEN = "#3a8c4e"

st.markdown(
    f"""
    <style>
    .stButton > button[kind="primary"] {{
        background-color: {ASTONS_DARK_GREEN};
        border-color: {ASTONS_DARK_GREEN};
    }}
    .stButton > button[kind="primary"]:hover {{
        background-color: {ASTONS_MID_GREEN};
        border-color: {ASTONS_MID_GREEN};
    }}
    h1, h2, h3 {{
        color: {ASTONS_DARK_GREEN};
    }}
    .status-pending  {{ color: #b58900; font-weight: 600; }}
    .status-approved {{ color: #1a5c2e; font-weight: 600; }}
    .status-rejected {{ color: #b00020; font-weight: 600; }}
    .status-sent     {{ color: #555555; font-weight: 600; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# === BOOT ===
db.init_db()


# === HELPERS ===

def format_status(status: str) -> str:
    labels = {
        "pending_approval": ("Pending approval", "status-pending"),
        "approved": ("Approved", "status-approved"),
        "rejected": ("Rejected", "status-rejected"),
        "sent": ("Sent", "status-sent"),
    }
    text, css = labels.get(status, (status, ""))
    return f'<span class="{css}">{text}</span>'


def format_timestamp(iso: str) -> str:
    """Stored timestamps are naive UTC; display in UK time (GMT/BST)."""
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        return stamp.astimezone(UK_TZ).strftime("%d %b %Y %H:%M")
    except Exception:
        return iso


def format_date(iso: str) -> str:
    """'2026-07-01T00:00:00' -> '01 Jul 2026'."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso[:10]).strftime("%d %b %Y")
    except Exception:
        return iso


def header(user):
    logo_path = Path(__file__).parent / "astons_logo.png"
    cols = st.columns([1, 4, 2])
    with cols[0]:
        if logo_path.exists():
            st.image(str(logo_path), width=120)
    with cols[1]:
        st.title("Invoice Hub")
        st.caption("Generate branded Astons invoices with approval workflow")
    with cols[2]:
        st.write("")
        st.write(f"**{user['full_name']}**")
        st.caption(
            f"Role: {'Approver' if user['role'] == 'approver' else 'Team member'}"
        )
        if st.button("Sign out", use_container_width=True):
            auth.logout()
            st.rerun()
    st.divider()


def fmt_money(value) -> str:
    """Currency per SPEC.md: £ #,##0.00."""
    if value is None or value == "":
        return ""
    try:
        return f"£{float(value):,.2f}"
    except (TypeError, ValueError):
        return f"£{value}"


def sidebar_counts(user):
    with st.sidebar:
        if xero_client.is_connected():
            xc = db.xero_count_drafts()
            st.subheader("Xero drafts")
            st.metric("Awaiting review", xc.get("PENDING_REVIEW", 0))
            if user["role"] == "approver":
                report = xero_watchdog.overdue_report()
                overdue = len(report["warn"]) + len(report["escalate"])
                if overdue:
                    st.metric("Waiting 3+ days", overdue,
                              delta=f"{len(report['escalate'])} escalated"
                              if report["escalate"] else None,
                              delta_color="inverse")
                exceptions = (xc.get("EXTERNAL_ACTION", 0)
                              + xc.get("APPROVED_NO_ATTACHMENT", 0))
                if exceptions:
                    st.metric("Exceptions", exceptions)
        legacy_pending = db.count_by_status().get("pending_approval", 0)
        if legacy_pending:
            st.divider()
            st.caption(f"Legacy uploads pending: {legacy_pending}")
        st.divider()
        st.caption(
            "All data on this platform stays inside Astons. "
            "Invoices are processed on our private Railway deployment."
        )


# === TEAM MEMBER: NEW SUBMISSION ===

def team_new_submission(user):
    st.subheader("New submission")
    st.caption(
        "1. Pick which portfolio these invoices belong to. "
        "2. Drag your BrightManager PDFs onto the drop zone. "
        "3. Click Generate to create branded previews. "
        "4. Review, then submit for Ash's approval."
    )

    portfolio_key = st.radio(
        "Portfolio",
        options=list(PORTFOLIOS.keys()),
        format_func=lambda k: PORTFOLIOS[k]["label"],
        horizontal=True,
        key="portfolio_choice",
    )

    uploaded = st.file_uploader(
        "Drop BrightManager PDFs here",
        type=["pdf"],
        accept_multiple_files=True,
        key="upload",
    )

    # Clear generated previews if upload set or portfolio changed
    current_key = (
        portfolio_key,
        tuple((f.name, f.size) for f in uploaded) if uploaded else (),
    )
    if st.session_state.get("last_gen_key") != current_key:
        # Clear old submitted flags from previous batch
        for old_key in [k for k in st.session_state if k.startswith("submitted_")]:
            del st.session_state[old_key]
        st.session_state["generated"] = []
        st.session_state["gen_errors"] = []
        st.session_state["last_gen_key"] = current_key

    if uploaded:
        st.write(f"**{len(uploaded)}** file{'s' if len(uploaded) != 1 else ''} ready")

        if st.button("Generate branded previews", type="primary"):
            generated = []
            errors = []
            batch_invoice_nos = {}  # invoice_no -> source filename, for same-batch duplicates
            progress = st.progress(0, text="Starting...")

            with tempfile.TemporaryDirectory() as tmp:
                for i, up in enumerate(uploaded):
                    progress.progress(
                        i / len(uploaded),
                        text=f"Processing {up.name}...",
                    )
                    try:
                        in_path = os.path.join(tmp, up.name)
                        source_bytes = up.getbuffer().tobytes()
                        with open(in_path, "wb") as fh:
                            fh.write(source_bytes)

                        data = parse_brightmanager_pdf(in_path)
                        apply_portfolio(data, portfolio_key)

                        # Guardrail: refuse to generate an invoice with
                        # missing or inconsistent data instead of producing
                        # a bad PDF that has to be caught at approval.
                        problems = validate_invoice_data(data)
                        if problems:
                            errors.append({
                                "source": up.name,
                                "error": "Couldn't read this fee note correctly — not generated.",
                                "trace": "\n".join(f"• {p}" for p in problems),
                            })
                            continue

                        # Duplicate checks: against the database, and
                        # against other files in this same upload.
                        existing = db.find_active_by_invoice_no(data["invoice_no"])
                        duplicate = existing[0] if existing else None
                        dup_in_batch = batch_invoice_nos.get(data["invoice_no"])
                        batch_invoice_nos.setdefault(data["invoice_no"], up.name)

                        clean = re.sub(r"[^\w\s-]", "", data["client_name"]).strip().replace(" ", "_")
                        out_filename = f"Astons_Invoice_{data['invoice_no']}_{clean}.pdf"
                        out_path = os.path.join(tmp, out_filename)
                        generate_branded_invoice(data, out_path)

                        with open(out_path, "rb") as fh:
                            branded_bytes = fh.read()

                        generated.append({
                            "source_name": up.name,
                            "source_bytes": source_bytes,
                            "branded_name": out_filename,
                            "branded_bytes": branded_bytes,
                            "client_name": data["client_name"],
                            "invoice_no": data["invoice_no"],
                            "total": data.get("total", ""),
                            "portfolio": portfolio_key,
                            "duplicate": duplicate,
                            "dup_in_batch": dup_in_batch,
                        })
                    except Exception as exc:
                        errors.append({
                            "source": up.name,
                            "error": str(exc),
                            "trace": traceback.format_exc(),
                        })

            progress.empty()
            st.session_state["generated"] = generated
            st.session_state["gen_errors"] = errors

    generated = st.session_state.get("generated", [])
    errors = st.session_state.get("gen_errors", [])

    if generated:
        st.success(
            f"Generated {len(generated)} branded preview"
            f"{'s' if len(generated) != 1 else ''}. "
            "Review and submit for approval below."
        )

        for idx, item in enumerate(generated):
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 2])
                with cols[0]:
                    st.write(f"**{item['client_name']}**")
                    st.caption(
                        f"Invoice {item['invoice_no']}  |  "
                        f"Portfolio {item['portfolio']}"
                    )
                with cols[1]:
                    if item["total"]:
                        st.write(f"£{item['total']}")
                with cols[2]:
                    st.download_button(
                        "Preview PDF",
                        data=item["branded_bytes"],
                        file_name=item["branded_name"],
                        mime="application/pdf",
                        key=f"preview_{idx}",
                        use_container_width=True,
                    )
                with cols[3]:
                    submit_key = f"submit_{idx}"
                    if item.get("duplicate") or item.get("dup_in_batch"):
                        st.error("Duplicate — blocked")
                    elif st.session_state.get(f"submitted_{idx}"):
                        st.success("Submitted")
                    else:
                        if st.button(
                            "Submit for approval",
                            key=submit_key,
                            type="primary",
                            use_container_width=True,
                        ):
                            # Re-check just before writing, in case a
                            # colleague submitted the same invoice since
                            # the previews were generated.
                            if db.find_active_by_invoice_no(item["invoice_no"]):
                                st.error(
                                    f"Invoice {item['invoice_no']} has just been "
                                    "submitted by someone else — not submitted again."
                                )
                            else:
                                db.create_invoice(
                                    created_by_user_id=user["id"],
                                    portfolio=item["portfolio"],
                                    client_name=item["client_name"],
                                    invoice_no=item["invoice_no"],
                                    total=item["total"],
                                    source_pdf=item["source_bytes"],
                                    source_pdf_filename=item["source_name"],
                                    branded_pdf=item["branded_bytes"],
                                    branded_pdf_filename=item["branded_name"],
                                )
                                st.session_state[f"submitted_{idx}"] = True
                                st.rerun()

                if item.get("duplicate"):
                    d = item["duplicate"]
                    st.warning(
                        f"Invoice {item['invoice_no']} was already submitted by "
                        f"**{d['created_by_name']}** on {format_timestamp(d['created_at'])} "
                        f"(status: {STATUS_LABELS.get(d['status'], d['status'])}). "
                        "If this is genuinely a new invoice, check the invoice number "
                        "in BrightManager."
                    )
                elif item.get("dup_in_batch"):
                    st.warning(
                        f"Invoice {item['invoice_no']} appears twice in this upload "
                        f"(also in **{item['dup_in_batch']}**) — only one copy can be submitted."
                    )

        # Submit-all convenience (duplicates are excluded)
        unsubmitted_indices = [
            i for i in range(len(generated))
            if not st.session_state.get(f"submitted_{i}")
            and not generated[i].get("duplicate")
            and not generated[i].get("dup_in_batch")
        ]
        if len(unsubmitted_indices) > 1:
            if st.button(
                f"Submit all {len(unsubmitted_indices)} remaining for approval",
                type="primary",
                use_container_width=True,
            ):
                for i in unsubmitted_indices:
                    item = generated[i]
                    if db.find_active_by_invoice_no(item["invoice_no"]):
                        continue  # became a duplicate since previews were generated
                    db.create_invoice(
                        created_by_user_id=user["id"],
                        portfolio=item["portfolio"],
                        client_name=item["client_name"],
                        invoice_no=item["invoice_no"],
                        total=item["total"],
                        source_pdf=item["source_bytes"],
                        source_pdf_filename=item["source_name"],
                        branded_pdf=item["branded_bytes"],
                        branded_pdf_filename=item["branded_name"],
                    )
                    st.session_state[f"submitted_{i}"] = True
                st.rerun()

    if errors:
        st.error(f"{len(errors)} file{'s' if len(errors) != 1 else ''} failed to process")
        for err in errors:
            with st.expander(f"{err['source']} — {err['error']}"):
                st.code(err["trace"])


# === TEAM MEMBER: MY INVOICES ===

def team_my_invoices(user):
    st.subheader("My invoices")
    st.caption(
        "Your submissions. Once Ash approves, the Download button will let "
        "you grab the branded PDF to send to the client."
    )
    rows = db.list_invoices_for_user(user["id"], limit=500)
    if not rows:
        st.info("You haven't submitted any invoices yet.")
        return

    for inv in rows:
        with st.container(border=True):
            top = st.columns([3, 2, 2, 2, 2])
            with top[0]:
                st.write(f"**{inv['client_name']}**")
                st.caption(
                    f"Invoice {inv['invoice_no']}  |  "
                    f"Portfolio {inv['portfolio']}  |  "
                    f"Submitted {format_timestamp(inv['created_at'])}"
                )
            with top[1]:
                if inv["total"]:
                    st.write(f"£{inv['total']}")
            with top[2]:
                st.markdown(format_status(inv["status"]), unsafe_allow_html=True)
                if inv["status"] in ("approved", "sent") and inv["approved_by_name"]:
                    st.caption(
                        f"by {inv['approved_by_name']} on {format_timestamp(inv['approved_at'])}"
                    )
            with top[3]:
                if inv["status"] in ("approved", "sent"):
                    full = db.get_invoice(inv["id"])
                    st.download_button(
                        "Download branded PDF",
                        data=full["branded_pdf"],
                        file_name=full["branded_pdf_filename"],
                        mime="application/pdf",
                        key=f"dl_{inv['id']}",
                        use_container_width=True,
                    )
            with top[4]:
                if inv["status"] == "approved":
                    if st.button(
                        "Mark as sent",
                        key=f"sent_{inv['id']}",
                        use_container_width=True,
                    ):
                        db.mark_invoice_sent(inv["id"], user["id"])
                        st.rerun()

            if inv["status"] == "rejected" and inv["rejection_note"]:
                st.warning(f"**Rejection note:** {inv['rejection_note']}")


def team_xero_drafts(user):
    """Team-member prep screen: raise in BrightManager, then here pick
    the entity and who raised it. Ash's approval queue arrives
    pre-filled — Ash just approves or rejects."""
    st.subheader("Xero drafts")
    show_flash("tq_flash")
    st.caption(
        "Raise your fee note in BrightManager as usual — it lands here "
        "automatically. Pick the entity and who raised it, preview the "
        "branded fee note, and it goes to Ash for approval."
    )
    if not xero_client.is_connected():
        st.info("Xero isn't connected yet — Ash needs to set this up.")
        return

    scols = st.columns([2, 3, 5])
    force = scols[0].button("Sync now", use_container_width=True,
                            help="Pull the latest drafts from Xero immediately")
    with st.spinner("Checking Xero for new drafts..."):
        xero_sync.maybe_sync(force=force)
    with scols[1]:
        last = db.xero_last_sync_time()
        if last:
            st.caption(f"Last successful sync: {format_timestamp(last)}")

    raisers_reg = db.xero_raisers_all(active_only=True)
    raiser_names = {r["initials"]: r["name"] for r in raisers_reg}
    known = set(raiser_names)
    my_initials = (user.get("initials") or "").strip().upper()
    if my_initials and my_initials not in known:
        my_initials = ""
    if not my_initials:
        st.warning(
            "Your account has no raiser initials yet — ask Ash to set "
            "them in the Users tab so 'raised by' can default to you."
        )

    drafts = db.xero_list_drafts("PENDING_REVIEW")
    if not drafts:
        st.success("No drafts waiting — raise one in BrightManager and "
                   "it will appear here within a few minutes.")
    for d in drafts:
        iid = d["invoice_id"]
        prepped = bool(d.get("entity") and d.get("raiser_pair"))
        with st.container(border=True):
            top = st.columns([3, 2, 2, 3])
            with top[0]:
                st.write(f"**{d['contact_name'] or '(no contact)'}**")
                st.caption(f"{d['invoice_number'] or '(no number)'}  |  "
                           f"{format_date(d['date'])}")
            with top[1]:
                st.write(fmt_money(d["total"]))
                st.caption(f"Net {fmt_money(d['sub_total'])}")
            with top[2]:
                if prepped:
                    st.success("Ready for Ash", icon="✅")
                    st.caption(f"{d['entity']} · {d['raiser_pair']}")
                else:
                    st.warning("Needs prep", icon="📝")
            with top[3]:
                if d.get("reference"):
                    st.caption(f"Reference: {d['reference']}")

            client_address_widget(d, "tq")
            stored_raisers = [i for i in (d.get("raiser_pair") or "").split("/")
                              if i in known]
            default_raisers = (stored_raisers
                               or xero_attrib.parse_reference(d.get("reference"), known)
                               or ([my_initials] if my_initials else []))
            pcols = st.columns([3, 3, 2, 2])
            entity_choice = pcols[0].selectbox(
                "Entity", ["AA", "CW"],
                index=["AA", "CW"].index(d["entity"]) if d.get("entity") else None,
                format_func=lambda e: ENTITY_LABELS[e],
                placeholder="Select AA or CW...",
                key=f"tq_entity_{iid}",
            )
            raiser_choice = pcols[1].multiselect(
                "Raised by", options=sorted(known), default=default_raisers,
                max_selections=2,
                format_func=lambda i: f"{i} — {raiser_names[i]}",
                placeholder="Pick 1 or 2...",
                key=f"tq_raisers_{iid}",
            )
            with pcols[2]:
                st.write("")
                if entity_choice and st.button("Preview PDF", key=f"tq_prev_{iid}",
                                               use_container_width=True):
                    try:
                        with st.spinner("Generating preview..."):
                            st.session_state[f"tq_pdf_{iid}"] = (
                                xero_actions.preview_pdf(iid, entity_choice))
                    except Exception as exc:
                        st.error(f"Preview failed: {exc}")
                preview = st.session_state.get(f"tq_pdf_{iid}")
                if preview:
                    st.download_button(
                        "Download preview", data=preview["pdf"],
                        file_name=preview["filename"], mime="application/pdf",
                        key=f"tq_prevdl_{iid}", use_container_width=True,
                    )
            with pcols[3]:
                st.write("")
                if st.button("Save for approval", key=f"tq_save_{iid}",
                             type="primary", use_container_width=True):
                    if not entity_choice or not raiser_choice:
                        st.error("Pick the entity and at least one raiser first.")
                    else:
                        db.xero_set_draft_prep(
                            iid, entity_choice, "/".join(raiser_choice), user["id"])
                        st.session_state["tq_flash"] = (
                            "success",
                            f"{d['invoice_number']} is ready for Ash's approval.")
                        st.rerun()

    actioned = db.xero_recent_actioned()
    if actioned:
        st.divider()
        st.write("**Recently actioned by Ash**")
        for a in actioned[:30]:
            with st.container(border=True):
                cols = st.columns([3, 2, 4])
                cols[0].write(f"**{a['contact_name']}**")
                cols[0].caption(a["invoice_number"] or "")
                cols[1].write(fmt_money(a["total"]))
                with cols[2]:
                    if a["hub_status"].startswith("APPROVED"):
                        st.success(f"Approved {format_timestamp(a['decided_at'])}")
                    else:
                        st.error(f"Rejected: {a['reject_reason']} — delete the "
                                 "old invoice in BrightManager, amend, and "
                                 "re-raise.")


def render_team_view(user):
    sidebar_counts(user)
    tabs = st.tabs(["Xero drafts", "Legacy (uploads)"])
    with tabs[0]:
        team_xero_drafts(user)
    with tabs[1]:
        st.caption(
            "The old upload workflow — kept for your past records. All "
            "new fee notes go through BrightManager → Xero drafts."
        )
        team_my_invoices(user)


# === APPROVER VIEWS ===

def approver_queue(user):
    st.subheader("Pending approvals")
    pending = db.list_invoices_by_status("pending_approval")
    if not pending:
        st.success("You're all caught up — no invoices awaiting approval.")
        return

    st.caption(
        f"{len(pending)} invoice{'s' if len(pending) != 1 else ''} awaiting your review. "
        "Download each branded PDF to review it, then approve or reject."
    )

    for inv in pending:
        with st.container(border=True):
            top = st.columns([3, 2, 2, 2])
            with top[0]:
                st.write(f"**{inv['client_name']}**")
                st.caption(
                    f"Invoice {inv['invoice_no']}  |  "
                    f"Portfolio {inv['portfolio']}  |  "
                    f"Submitted by {inv['created_by_name']} on "
                    f"{format_timestamp(inv['created_at'])}"
                )
            with top[1]:
                if inv["total"]:
                    st.write(f"£{inv['total']}")
            with top[2]:
                full = db.get_invoice(inv["id"])
                st.download_button(
                    "Review branded PDF",
                    data=full["branded_pdf"],
                    file_name=full["branded_pdf_filename"],
                    mime="application/pdf",
                    key=f"rev_{inv['id']}",
                    use_container_width=True,
                )
            with top[3]:
                st.download_button(
                    "Original BrightManager PDF",
                    data=full["source_pdf"],
                    file_name=full["source_pdf_filename"],
                    mime="application/pdf",
                    key=f"rev_src_{inv['id']}",
                    use_container_width=True,
                )

            actions = st.columns([2, 2, 4])
            with actions[0]:
                if st.button(
                    "Approve",
                    key=f"app_{inv['id']}",
                    type="primary",
                    use_container_width=True,
                ):
                    db.approve_invoice(inv["id"], user["id"])
                    st.rerun()
            with actions[1]:
                reject_key = f"rej_open_{inv['id']}"
                if st.button(
                    "Reject",
                    key=f"rej_btn_{inv['id']}",
                    use_container_width=True,
                ):
                    st.session_state[reject_key] = True
            if st.session_state.get(reject_key):
                with actions[2]:
                    note = st.text_input(
                        "Why rejected?",
                        key=f"rej_note_{inv['id']}",
                        placeholder="e.g. wrong portfolio / line item missing / client name typo",
                    )
                    if st.button(
                        "Confirm rejection",
                        key=f"rej_conf_{inv['id']}",
                    ):
                        if not note.strip():
                            st.error("Please add a note explaining the rejection.")
                        else:
                            db.reject_invoice(inv["id"], user["id"], note.strip())
                            st.session_state[reject_key] = False
                            st.rerun()


def approver_archive(user, status, title, empty_msg):
    st.subheader(title)
    rows = db.list_invoices_by_status(status)
    if not rows:
        st.info(empty_msg)
        return
    for inv in rows:
        with st.container(border=True):
            cols = st.columns([3, 2, 2, 2])
            with cols[0]:
                st.write(f"**{inv['client_name']}**")
                st.caption(
                    f"Invoice {inv['invoice_no']}  |  "
                    f"Portfolio {inv['portfolio']}  |  "
                    f"By {inv['created_by_name']} on {format_timestamp(inv['created_at'])}"
                )
            with cols[1]:
                if inv["total"]:
                    st.write(f"£{inv['total']}")
            with cols[2]:
                st.markdown(format_status(inv["status"]), unsafe_allow_html=True)
                if inv["approved_at"]:
                    st.caption(
                        f"{format_timestamp(inv['approved_at'])} "
                        f"by {inv['approved_by_name'] or '-'}"
                    )
            with cols[3]:
                full = db.get_invoice(inv["id"])
                st.download_button(
                    "Branded PDF",
                    data=full["branded_pdf"],
                    file_name=full["branded_pdf_filename"],
                    mime="application/pdf",
                    key=f"arch_dl_{status}_{inv['id']}",
                    use_container_width=True,
                )
            if inv["status"] == "rejected" and inv["rejection_note"]:
                st.warning(f"**Rejection note:** {inv['rejection_note']}")


def approver_users(user):
    st.subheader("Users")
    st.caption("Add, deactivate or reset passwords for team members and approvers.")

    with st.expander("Add new user", expanded=False):
        with st.form("new_user_form", clear_on_submit=True):
            nu_username = st.text_input("Username (lowercase, no spaces)")
            nu_full_name = st.text_input("Full name")
            nu_initials = st.text_input("Raiser initials (2-3 letters, e.g. BT)",
                                        max_chars=3)
            nu_password = st.text_input("Temporary password", type="password")
            nu_role = st.selectbox("Role", options=["team_member", "approver"])
            submitted = st.form_submit_button("Create user", type="primary")
        if submitted:
            if not nu_username or not nu_password or not nu_full_name:
                st.error("Username, full name and password are required.")
            elif db.get_user_by_username(nu_username):
                st.error(f"A user with username '{nu_username}' already exists.")
            else:
                new_id = db.create_user(
                    username=nu_username,
                    password=nu_password,
                    full_name=nu_full_name,
                    role=nu_role,
                )
                if nu_initials.strip():
                    db.set_user_initials(new_id, nu_initials)
                st.success(f"Created user '{nu_username}'.")
                st.rerun()

    st.divider()
    st.write("**Existing users**")
    users = db.list_users()
    for u in users:
        with st.container(border=True):
            cols = st.columns([3, 2, 2, 2, 3])
            with cols[0]:
                st.write(f"**{u['full_name']}**")
                st.caption(f"@{u['username']}")
                new_initials = st.text_input(
                    "Raiser initials", value=u.get("initials") or "",
                    max_chars=3, key=f"user_init_{u['id']}",
                    label_visibility="collapsed", placeholder="initials",
                )
                if (new_initials or "").strip().upper() != (u.get("initials") or ""):
                    if st.button("Save initials", key=f"user_init_save_{u['id']}"):
                        db.set_user_initials(u["id"], new_initials)
                        st.rerun()
            with cols[1]:
                st.caption(u["role"])
            with cols[2]:
                st.caption("Active" if u["active"] else "Inactive")
            with cols[3]:
                if u["id"] != user["id"]:
                    label = "Deactivate" if u["active"] else "Reactivate"
                    if st.button(label, key=f"toggle_{u['id']}", use_container_width=True):
                        db.set_user_active(u["id"], not bool(u["active"]))
                        st.rerun()
                else:
                    st.caption("(you)")
            with cols[4]:
                reset_open = f"reset_open_{u['id']}"
                if st.button("Reset password", key=f"reset_btn_{u['id']}", use_container_width=True):
                    st.session_state[reset_open] = True
                if st.session_state.get(reset_open):
                    new_pw = st.text_input(
                        "New password",
                        type="password",
                        key=f"reset_pw_{u['id']}",
                    )
                    if st.button("Confirm reset", key=f"reset_conf_{u['id']}"):
                        if len(new_pw) < 8:
                            st.error("Password must be at least 8 characters.")
                        else:
                            db.reset_user_password(u["id"], new_pw)
                            st.session_state[reset_open] = False
                            st.success("Password reset.")
                            st.rerun()


def approver_audit(user):
    st.subheader("Audit log")
    st.caption("Recent activity across the platform (most recent first).")
    rows = db.recent_audit(limit=200)
    if not rows:
        st.info("No activity yet.")
        return
    for r in rows:
        with st.container(border=True):
            cols = st.columns([2, 2, 2, 4])
            cols[0].caption(format_timestamp(r["created_at"]))
            cols[1].write(r["full_name"] or r["username"] or "-")
            cols[2].write(r["action"])
            cols[3].caption(r["note"] or (f"Invoice #{r['invoice_id']}" if r["invoice_id"] else ""))


# === XERO (SPEC.md Phase 1) ===

def handle_xero_oauth_callback():
    """Complete the Xero consent flow when the app is loaded with
    ?code=...&state=... after the redirect back from Xero.

    Runs before login: the redirect back from Xero always starts a fresh
    browser session, so requiring login first would strand the flow at
    the sign-in form. The HMAC-signed state (see xero_client) proves the
    consent link was generated from this app's configuration."""
    params = st.query_params
    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return
    st.query_params.clear()
    try:
        xero_client.exchange_code(code, state)
        db.record_xero_event(None, "xero_connect", "Consent flow completed")
        names = ", ".join(
            c["tenant_name"] for c in db.xero_list_connections()
        ) or "no organisations"
        print(f"Xero connected: {names}", flush=True)
        results = xero_sync.sync_all()
        print(f"Xero initial sync: {results}", flush=True)
        fetched = sum(r.get("fetched", 0) for r in results if r.get("ok"))
        st.success(
            f"Xero connected: {names}. Initial sync pulled {fetched} "
            f"draft{'s' if fetched != 1 else ''}. Sign in to review the queue."
        )
    except Exception as exc:
        print(f"Xero connect failed: {exc}", flush=True)
        st.error(f"Xero connection failed: {exc}")


def xero_entity_badge(draft) -> str:
    entity = draft.get("entity")
    if entity:
        return entity
    return draft.get("tenant_name") or "Untagged"


REJECT_REASONS = ["Wrong amount", "Wrong client", "Wrong narrative", "Duplicate", "Other"]

BM_TIDY_NOTE = (
    "Note: deleting the Xero draft does NOT remove it from BrightManager "
    "— delete or amend the invoice in BM as well before re-raising."
)


def client_address_widget(d, key_prefix):
    """Resolved client address for the fee note, with a manual override
    that is remembered per client."""
    resolved = hub_addresses.resolve(d.get("contact_name"))
    missing = not resolved["lines"]
    label = "Client address" + (" — ⚠️ MISSING" if missing else "")
    with st.expander(label, expanded=False):
        if missing:
            st.warning(
                "No address found in the Hub address book, the Xero "
                "contact, or BrightManager. The fee note will print "
                "without an address unless one is added here (saved once "
                "per client, remembered for all future fee notes)."
            )
        else:
            st.caption(f"Source: {resolved['source']}")
        new_text = st.text_area(
            "Address (one line per row)",
            value="\n".join(resolved["lines"]),
            key=f"{key_prefix}_addr_{d['invoice_id']}",
        )
        if st.button("Save address for this client",
                     key=f"{key_prefix}_addrsave_{d['invoice_id']}"):
            if not new_text.strip():
                st.error("Enter the address first.")
            else:
                hub_addresses.save_manual(d["contact_name"], new_text)
                st.rerun()
    return resolved

ENTITY_LABELS = {
    "AA": "AA — bank 60-83-71 / 19010489 (A-portfolio)",
    "CW": "CW — bank 04-13-76 / 00273335 (C-portfolio)",
}


def show_flash(key: str):
    flash = st.session_state.pop(key, None)
    if flash:
        kind, msg = flash
        {"success": st.success, "error": st.error, "warning": st.warning}[kind](msg)


def approver_xero_queue(user):
    st.subheader("Xero review queue")
    show_flash("xq_flash")
    st.caption(
        "Fee-note drafts raised in BrightManager, pulled automatically from "
        "Xero. Approve sets the invoice to AUTHORISED in Xero and attaches "
        "the branded fee note; Reject deletes the draft in Xero — no "
        "duplicate actions needed in Xero itself."
    )

    if not xero_client.is_configured():
        st.warning(
            "Xero credentials are not set. Add XERO_CLIENT_ID, "
            "XERO_CLIENT_SECRET and XERO_REDIRECT_URI as environment "
            "variables, then connect from the Xero settings tab."
        )
        return
    if not xero_client.is_connected():
        st.info("Xero is not connected yet — go to the Xero settings tab.")
        return

    # Throttled auto-sync on page load + manual sync.
    cols = st.columns([2, 3, 5])
    with cols[0]:
        force = st.button("Sync now", use_container_width=True)
    with st.spinner("Checking Xero for new drafts..."):
        results = xero_sync.maybe_sync(force=force)
    xero_watchdog.maybe_run()
    failures = [r for r in results if not r.get("ok")]
    for f in failures:
        st.error(f"Sync failed for tenant {f['tenant_id']}: {f['error']}")
    with cols[1]:
        last = db.xero_last_sync_time()
        if last:
            st.caption(f"Last successful sync: {format_timestamp(last)}")

    entity_map = db.xero_entity_map_all()
    raisers_reg = db.xero_raisers_all(active_only=True)
    raiser_names = {r["initials"]: r["name"] for r in raisers_reg}
    known = set(raiser_names)
    if not raisers_reg:
        st.info(
            "No raisers registered yet — add the team's initials in the "
            "Xero settings tab so fee notes auto-attribute from the "
            "invoice Reference (e.g. LG/BT)."
        )
    drafts = db.xero_list_drafts("PENDING_REVIEW")
    if drafts:
        st.write(f"**{len(drafts)}** draft{'s' if len(drafts) != 1 else ''} awaiting review")
    else:
        st.success("No Xero drafts awaiting review.")

    anomaly_history = xero_anomaly.load_history() if drafts else []

    for d in drafts:
        iid = d["invoice_id"]
        flags = xero_anomaly.flags_for_draft(d, anomaly_history)
        with st.container(border=True):
            top = st.columns([3, 2, 2, 2, 2])
            with top[0]:
                st.write(f"**{d['contact_name'] or '(no contact)'}**")
                st.caption(
                    f"{d['invoice_number'] or '(no number)'}  |  "
                    f"Xero status: {d['xero_status']}"
                )
                age = xero_watchdog.draft_age_days(d)
                if age >= xero_watchdog.ESCALATE_DAYS:
                    st.error(f"Escalated — waiting {age} days", icon="🚨")
                elif age >= xero_watchdog.WARN_DAYS:
                    st.warning(f"Waiting {age} days", icon="⚠️")
                for f in flags:
                    if f["level"] == "red":
                        st.error(f"{f['label']}: {f['detail']}", icon="⛔")
                    elif f["level"] == "amber":
                        st.warning(f"{f['label']}: {f['detail']}", icon="🟠")
                    else:
                        st.info(f"{f['label']}: {f['detail']}", icon="ℹ️")
            with top[1]:
                st.write(fmt_money(d["total"]))
                st.caption(
                    f"Net {fmt_money(d['sub_total'])} + "
                    f"VAT {fmt_money(d['total_tax'])}"
                )
            with top[2]:
                st.caption(f"Invoice date\n\n{format_date(d['date'])}")
            with top[3]:
                st.caption(f"Due date\n\n{format_date(d['due_date'])}")
            with top[4]:
                if d["reference"]:
                    st.caption(f"Reference\n\n{d['reference']}")

            try:
                line_items = json.loads(d["line_items_json"] or "[]")
            except json.JSONDecodeError:
                line_items = []
            if line_items:
                with st.expander(f"Line items ({len(line_items)})"):
                    for li in line_items:
                        tracking = ", ".join(
                            f"{t.get('Name')}: {t.get('Option')}"
                            for t in (li.get("Tracking") or [])
                        )
                        extra = f"  ·  nominal {li.get('AccountCode')}" if li.get("AccountCode") else ""
                        extra += f"  ·  {tracking}" if tracking else ""
                        st.write(
                            f"- {li.get('Description', '(no description)')} — "
                            f"{fmt_money(li.get('LineAmount'))}{extra}"
                        )

            # --- Actions ---
            address = client_address_widget(d, "xq")
            # Defaults: team prep beats auto-detection beats blank.
            derived = d.get("entity") or xero_pdf.derive_entity(d, entity_map)
            stored_raisers = [i for i in (d.get("raiser_pair") or "").split("/")
                              if i in known]
            parsed_raisers = stored_raisers or xero_attrib.parse_reference(
                d.get("reference"), known)
            action_cols = st.columns([3, 3, 2, 2])
            with action_cols[0]:
                options = ["AA", "CW"]
                entity_choice = st.selectbox(
                    "Entity (sets bank details on the fee note)",
                    options,
                    index=options.index(derived) if derived else None,
                    format_func=lambda e: ENTITY_LABELS[e],
                    placeholder="Select AA or CW...",
                    key=f"xq_entity_{iid}",
                )
                if d.get("entity"):
                    st.caption("Set by the team at prep.")
                elif derived:
                    st.caption(f"Auto-detected {derived} from tracking/nominals.")
                if entity_choice:
                    if st.button("Preview fee note", key=f"xq_prev_{iid}",
                                 use_container_width=True):
                        try:
                            with st.spinner("Generating preview..."):
                                st.session_state[f"xq_pdf_{iid}"] = (
                                    xero_actions.preview_pdf(iid, entity_choice))
                        except Exception as exc:
                            st.error(f"Preview failed: {exc}")
                    preview = st.session_state.get(f"xq_pdf_{iid}")
                    if preview:
                        st.download_button(
                            "Download preview PDF",
                            data=preview["pdf"],
                            file_name=preview["filename"],
                            mime="application/pdf",
                            key=f"xq_prevdl_{iid}",
                            use_container_width=True,
                        )
            with action_cols[1]:
                raiser_choice = st.multiselect(
                    "Raised by (required)",
                    options=sorted(known),
                    default=parsed_raisers,
                    max_selections=2,
                    format_func=lambda i: f"{i} — {raiser_names[i]}",
                    placeholder="Pick 1 or 2 raisers...",
                    key=f"xq_raisers_{iid}",
                )
                if stored_raisers:
                    st.caption("Set by the team at prep.")
                elif parsed_raisers:
                    st.caption(f"From reference: {'/'.join(parsed_raisers)}")
                else:
                    hint = xero_attrib.unregistered_pair_hint(d.get("reference"), known)
                    if hint:
                        st.caption(f"Reference contains '{hint}' — add these "
                                   "initials in Xero settings to auto-attribute.")
            with action_cols[2]:
                if st.button("Approve", key=f"xq_app_{iid}", type="primary",
                             use_container_width=True):
                    st.session_state[f"xq_confirm_app_{iid}"] = True
                    st.session_state.pop(f"xq_confirm_rej_{iid}", None)
            with action_cols[3]:
                if st.button("Reject", key=f"xq_rej_{iid}", use_container_width=True):
                    st.session_state[f"xq_confirm_rej_{iid}"] = True
                    st.session_state.pop(f"xq_confirm_app_{iid}", None)

            if st.session_state.get(f"xq_confirm_app_{iid}"):
                if not entity_choice:
                    st.error("Choose AA or CW first — it decides which bank "
                             "details print on the fee note.")
                elif not raiser_choice:
                    st.error("Pick who raised this fee note first — every "
                             "invoice must be attributed (add missing initials "
                             "in Xero settings).")
                else:
                    if not address["lines"]:
                        st.warning(
                            "No client address on file — the fee note will "
                            "print without one. Add it in the client address "
                            "box above if needed.", icon="⚠️")
                    credit = xero_attrib.describe(raiser_choice)
                    st.warning(
                        f"This will set **{d['invoice_number']}** "
                        f"({d['contact_name']}, {fmt_money(d['total'])}) to "
                        f"AUTHORISED in Xero and attach the branded "
                        f"{entity_choice} fee note to the client-facing invoice. "
                        f"Credit: {credit}"
                        + (" — split 50/50 unless overridden in settings."
                           if len(raiser_choice) == 2 else ".")
                    )
                    ccols = st.columns([2, 2, 6])
                    if ccols[0].button("Confirm approve", key=f"xq_capp_{iid}",
                                       type="primary", use_container_width=True):
                        try:
                            with st.spinner("Authorising in Xero and attaching PDF..."):
                                result = xero_actions.approve_draft(
                                    iid, user["id"], entity_choice,
                                    raisers=raiser_choice,
                                )
                            if flags:
                                # SPEC 7.2: flag events logged so caught
                                # errors are countable per month/raiser.
                                db.record_xero_event(
                                    user["id"], "anomaly_flags_at_decision",
                                    f"xero_invoice_id={iid} decision=approve "
                                    f"raisers={'/'.join(raiser_choice)} "
                                    f"flags={xero_anomaly.summarise(flags)}")
                            if result["attachment_ok"]:
                                st.session_state["xq_flash"] = (
                                    "success",
                                    f"{result['invoice_number']} authorised in Xero "
                                    "and branded fee note attached.",
                                )
                            else:
                                st.session_state["xq_flash"] = (
                                    "error",
                                    f"{result['invoice_number']} was AUTHORISED but the "
                                    f"attachment failed: {result['error']} — it is "
                                    "flagged in the Exceptions tab with a retry button.",
                                )
                            st.session_state.pop(f"xq_confirm_app_{iid}", None)
                            st.rerun()
                        except xero_actions.ActionBlocked as exc:
                            st.error(str(exc))
                        except Exception as exc:
                            st.error(f"Approve failed before any Xero change: {exc}")
                    if ccols[1].button("Cancel", key=f"xq_capp_no_{iid}",
                                       use_container_width=True):
                        st.session_state.pop(f"xq_confirm_app_{iid}", None)
                        st.rerun()

            if st.session_state.get(f"xq_confirm_rej_{iid}"):
                rcols = st.columns([3, 4, 2, 2])
                reason_pick = rcols[0].selectbox(
                    "Reason", REJECT_REASONS, key=f"xq_reason_{iid}")
                note = rcols[1].text_input(
                    "Details (required for Other)", key=f"xq_rnote_{iid}",
                    placeholder="e.g. VAT missing / should be £1,200 not £120")
                if rcols[2].button("Confirm reject", key=f"xq_crej_{iid}",
                                   use_container_width=True):
                    if reason_pick == "Other" and not note.strip():
                        st.error("Please add details for an 'Other' rejection.")
                    else:
                        reason = reason_pick + (f" — {note.strip()}" if note.strip() else "")
                        try:
                            with st.spinner("Deleting draft in Xero..."):
                                result = xero_actions.reject_draft(iid, user["id"], reason)
                            if flags:
                                db.record_xero_event(
                                    user["id"], "anomaly_flags_at_decision",
                                    f"xero_invoice_id={iid} decision=reject "
                                    f"flags={xero_anomaly.summarise(flags)}")
                            notified = result.get("notified") or []
                            st.session_state["xq_flash"] = (
                                "success",
                                f"{result['invoice_number']} deleted in Xero. "
                                + (f"Raiser notified at {', '.join(notified)}. "
                                   if notified else
                                   "The raiser should amend and re-raise in "
                                   "BrightManager (no raiser email on file to notify). ")
                                + BM_TIDY_NOTE,
                            )
                            st.session_state.pop(f"xq_confirm_rej_{iid}", None)
                            st.rerun()
                        except xero_actions.ActionBlocked as exc:
                            st.error(str(exc))
                        except Exception as exc:
                            st.error(f"Reject failed: {exc}")
                if rcols[3].button("Cancel", key=f"xq_crej_no_{iid}",
                                   use_container_width=True):
                    st.session_state.pop(f"xq_confirm_rej_{iid}", None)
                    st.rerun()

    # --- Recently actioned ---
    actioned = db.xero_recent_actioned()
    if actioned:
        with st.expander(f"Recently actioned ({len(actioned)})"):
            for a in actioned:
                cols2 = st.columns([3, 2, 3, 2])
                with cols2[0]:
                    st.write(f"**{a['contact_name']}**")
                    st.caption(a["invoice_number"] or "")
                with cols2[1]:
                    st.write(fmt_money(a["total"]))
                    st.caption(a["entity"] or "")
                    full_row = db.xero_get_draft(a["invoice_id"]) or {}
                    if full_row.get("raiser_pair"):
                        st.caption(f"Raised by {full_row['raiser_pair']}")
                with cols2[2]:
                    if a["hub_status"] == "APPROVED":
                        st.success("Approved", icon="✅")
                    elif a["hub_status"] == "APPROVED_NO_ATTACHMENT":
                        st.warning("Approved — attachment failed (see Exceptions)")
                    else:
                        st.error(f"Rejected — awaiting re-raise: {a['reject_reason']}")
                    st.caption(
                        f"{format_timestamp(a['decided_at'])} by {a['decided_by_name'] or '-'}"
                    )
                with cols2[3]:
                    if a["hub_status"].startswith("APPROVED"):
                        full = db.xero_get_draft(a["invoice_id"])
                        if full and full.get("branded_pdf"):
                            st.download_button(
                                "Fee note PDF",
                                data=full["branded_pdf"],
                                file_name=full["branded_pdf_filename"],
                                mime="application/pdf",
                                key=f"xq_dl_{a['invoice_id']}",
                                use_container_width=True,
                            )


def approver_xero_exceptions(user):
    st.subheader("Exceptions")
    show_flash("xe_flash")
    st.caption(
        "Drafts actioned outside the Hub (deleted or authorised directly in "
        "Xero) and failed sync attempts. Nothing vanishes silently."
    )

    report = xero_watchdog.overdue_report()
    overdue = report["escalate"] + report["warn"]
    if overdue:
        st.write(f"**{len(overdue)}** draft{'s' if len(overdue) != 1 else ''} "
                 f"waiting {xero_watchdog.WARN_DAYS}+ days for a decision")
        for d in overdue:
            with st.container(border=True):
                cols = st.columns([3, 2, 4])
                with cols[0]:
                    st.write(f"**{d['contact_name'] or '(no contact)'}**")
                    st.caption(d["invoice_number"] or "")
                with cols[1]:
                    st.write(fmt_money(d["total"]))
                with cols[2]:
                    if d["age_days"] >= xero_watchdog.ESCALATE_DAYS:
                        st.error(f"Escalated — waiting {d['age_days']} days. "
                                 "Approve or reject it in the Xero queue.", icon="🚨")
                    else:
                        st.warning(f"Waiting {d['age_days']} days.", icon="⚠️")
        st.divider()

    no_attach = db.xero_list_drafts("APPROVED_NO_ATTACHMENT")
    if no_attach:
        st.write(f"**{len(no_attach)}** authorised without attachment — retry needed")
        for d in no_attach:
            with st.container(border=True):
                cols = st.columns([3, 2, 4, 2])
                with cols[0]:
                    st.write(f"**{d['contact_name']}**")
                    st.caption(d["invoice_number"] or "")
                with cols[1]:
                    st.write(fmt_money(d["total"]))
                with cols[2]:
                    st.error(
                        "AUTHORISED in Xero but the branded fee note failed to "
                        f"attach: {d['action_error']}"
                    )
                with cols[3]:
                    if st.button("Retry attachment", key=f"xe_retry_{d['invoice_id']}",
                                 type="primary", use_container_width=True):
                        result = xero_actions.retry_attachment(d["invoice_id"], user["id"])
                        if result["ok"]:
                            st.session_state["xe_flash"] = (
                                "success", f"{d['invoice_number']}: fee note attached.")
                        else:
                            st.session_state["xe_flash"] = (
                                "error", f"{d['invoice_number']}: still failing — {result['error']}")
                        st.rerun()
        st.divider()

    external = db.xero_list_drafts("EXTERNAL_ACTION")
    if external:
        st.write(f"**{len(external)}** external action{'s' if len(external) != 1 else ''}")
        for d in external:
            with st.container(border=True):
                cols = st.columns([3, 2, 4, 2])
                with cols[0]:
                    st.write(f"**{d['contact_name'] or '(no contact)'}**")
                    st.caption(
                        f"{d['invoice_number'] or '(no number)'}  |  "
                        f"{xero_entity_badge(d)}"
                    )
                with cols[1]:
                    st.write(fmt_money(d["total"]))
                with cols[2]:
                    st.warning(d["external_action_note"] or "Actioned outside the Hub.")
                with cols[3]:
                    if st.button(
                        "Dismiss",
                        key=f"xero_dismiss_{d['invoice_id']}",
                        use_container_width=True,
                    ):
                        db.xero_dismiss_draft(d["invoice_id"], user["id"])
                        st.rerun()
    else:
        st.success("No external actions outstanding.")

    failures = db.xero_recent_sync_failures()
    if failures:
        st.divider()
        st.write("**Recent sync failures**")
        for f in failures:
            st.error(
                f"{format_timestamp(f['created_at'])} — "
                f"{f['tenant_name'] or f['tenant_id'] or 'connection'}: {f['message']}"
            )


def approver_dashboard(user):
    import pandas as pd
    import datetime as _dt

    st.subheader("Billing dashboard")
    show_flash("dash_flash")

    # --- Month picker (last 18 months) ---
    today = _dt.date.today()
    month_options = []
    y, m = today.year, today.month
    for _ in range(18):
        month_options.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    sel = st.selectbox(
        "Month", month_options,
        format_func=lambda ym: _dt.date(ym[0], ym[1], 1).strftime("%B %Y"),
    )
    year, month = sel
    start, end = xero_reports.month_bounds(year, month)
    entries = xero_reports.entries_for_range(start, end)
    targets = db.billing_targets_all()

    if not entries and db.billing_imports_count() == 0:
        st.info(
            "No fee notes recorded for this month yet. Data arrives here "
            "from approvals in the Xero queue, and from the historical "
            "Bill Number List import below."
        )

    # --- Firm view (SPEC 4.2) ---
    split = xero_reports.firm_split(entries)
    firm_target = db.billing_target_for("FIRM", start, targets)
    mcols = st.columns(4)
    mcols[0].metric("Firm net billed", fmt_money(split["total"]))
    mcols[1].metric(
        "Of firm target",
        (f"{split['total'] / firm_target:.0%}" if firm_target else "—"),
        delta=(f"target {fmt_money(firm_target)}" if firm_target else "no target set"),
        delta_color="off",
    )
    mcols[2].metric("Fee notes", split["count"])
    mcols[3].metric("AA / CW",
                    f"{fmt_money(split['aa'])} / {fmt_money(split['cw'])}")
    if split["entity_untagged"]:
        st.caption(f"{fmt_money(split['entity_untagged'])} net not yet tagged AA/CW.")

    chart_cols = st.columns(2)
    with chart_cols[0]:
        st.caption("Cumulative net this month vs prior-months average")
        curve = xero_reports.cumulative_curve(entries, year, month)
        baseline = xero_reports.average_prior_curve(year, month)
        if curve and any(curve):
            frame = {"This month": curve}
            if baseline:
                frame["Prior avg"] = (baseline + [baseline[-1]] *
                                      len(curve))[:len(curve)]
            st.line_chart(pd.DataFrame(frame,
                                       index=range(1, len(curve) + 1)))
        else:
            st.caption("_No data yet._")
    with chart_cols[1]:
        st.caption("12-month net billed vs same month prior year")
        trend = xero_reports.twelve_month_trend(year, month)
        if any(t["net"] or t["prior_year_net"] for t in trend):
            st.bar_chart(pd.DataFrame(
                {"Net": [t["net"] for t in trend],
                 "Prior year": [t["prior_year_net"] for t in trend]},
                index=[t["month"] for t in trend]))
        else:
            st.caption("_No data yet._")

    # --- Per-person view (SPEC 4.1) ---
    st.write("**Per person**")
    people = xero_reports.per_person(entries)
    known_people = sorted(set(list(people.keys())
                              + [t["person"] for t in targets if t["person"] != "FIRM"]))
    if not known_people:
        st.caption("_No attributed fee notes this month._")
    for person in known_people:
        stats = people.get(person, {"net": 0.0, "sole_net": 0.0,
                                    "shared_net": 0.0, "count": 0})
        target = db.billing_target_for(person, start, targets)
        cols = st.columns([2, 2, 2, 2, 3])
        cols[0].write(f"**{person}**")
        cols[1].write(fmt_money(stats["net"]))
        cols[1].caption(f"of {fmt_money(target)} target" if target else "no target set")
        cols[2].write(f"{stats['net'] / target:.0%}" if target else "—")
        cols[2].caption("of target")
        cols[3].caption(f"Sole {fmt_money(stats['sole_net'])}\n\n"
                        f"Shared {fmt_money(stats['shared_net'])}")
        with cols[4]:
            with st.expander(f"{stats['count']} fee note(s)"):
                for e in entries:
                    share = next((s for s in e["splits"]
                                  if s["initials"] == person), None)
                    if share:
                        st.caption(
                            f"{e['fee_note_no']} — {e['client_name']} — "
                            f"{fmt_money(share['net'])}"
                            + (f" (of {fmt_money(e['net'])})"
                               if share.get('share', 1) < 1 else "")
                            + f" — {e['date']}"
                        )

    # --- Register + export (SPEC 4.3) ---
    st.write("**Fee note register**")
    register = xero_reports.register_rows(entries)
    if register:
        frame = pd.DataFrame(register)
        st.dataframe(frame, use_container_width=True, hide_index=True)
        st.download_button(
            "Download register CSV",
            data=frame.to_csv(index=False).encode("utf-8"),
            file_name=f"fee_note_register_{year}-{month:02d}.csv",
            mime="text/csv",
        )
    else:
        st.caption("_Nothing recorded for this month._")

    # --- Reconciliation (SPEC 4.3) ---
    st.write("**Reconciliation vs Xero**")
    st.caption("Sums the Hub's record of the month against Xero's "
               "authorised invoices — the automatic 'Check = 0'.")
    if st.button("Run reconciliation for this month"):
        try:
            with st.spinner("Fetching authorised invoices from Xero..."):
                recon = xero_recon.reconcile_month(year, month)
            if recon["clean"]:
                st.success(
                    f"Reconciles to the penny: Hub {fmt_money(recon['hub_total_net'])} "
                    f"= Xero {fmt_money(recon['xero_total_net'])} net."
                )
            else:
                st.error(
                    f"Variance {fmt_money(recon['variance'])}: "
                    f"Hub {fmt_money(recon['hub_total_net'])} vs "
                    f"Xero {fmt_money(recon['xero_total_net'])} net."
                )
                for i in recon["only_in_xero"]:
                    st.warning(f"In Xero but not the Hub: {i['invoice_number']} "
                               f"{i['contact']} {fmt_money(i['net'])} net "
                               "(authorised outside the Hub?)")
                for e in recon["only_in_hub"]:
                    st.warning(f"In the Hub but not Xero: {e['fee_note_no']} "
                               f"{e['client_name']} {fmt_money(e['net'])} net "
                               "(voided in Xero? imported row?)")
                for x in recon["amount_mismatch"]:
                    st.warning(f"Amount differs: {x['invoice_number']} — Hub "
                               f"{fmt_money(x['hub_net'])} vs Xero "
                               f"{fmt_money(x['xero_net'])}")
        except Exception as exc:
            st.error(f"Reconciliation failed: {exc}")

    # --- Targets editor (SPEC 4.1) ---
    with st.expander("Monthly targets"):
        st.caption(
            "Targets per person (raiser initials) plus a FIRM total. "
            "Effective-from dates preserve history — set a new amount "
            "from a given month and old months keep the old target."
        )
        raiser_opts = ["FIRM"] + [r["initials"] for r in db.xero_raisers_all()]
        with st.form("target_form", clear_on_submit=True):
            tcols = st.columns([3, 3, 3, 2])
            t_person = tcols[0].selectbox("Person", raiser_opts)
            t_from = tcols[1].date_input("Effective from",
                                         value=_dt.date(today.year, today.month, 1))
            t_amount = tcols[2].number_input("Monthly target £", min_value=0.0,
                                             step=500.0, format="%.2f")
            tcols[3].write("")
            t_add = tcols[3].form_submit_button("Set", use_container_width=True)
        if t_add:
            if t_amount <= 0:
                st.error("Target must be above zero.")
            else:
                db.billing_target_set(t_person,
                                      t_from.replace(day=1).isoformat(), t_amount)
                st.session_state["dash_flash"] = (
                    "success", f"Target set: {t_person} "
                    f"{fmt_money(t_amount)}/month from "
                    f"{t_from.replace(day=1).strftime('%B %Y')}.")
                st.rerun()
        for t in targets:
            tcols = st.columns([3, 3, 3, 2])
            tcols[0].write(f"**{t['person']}**")
            tcols[1].caption(f"from {format_date(t['effective_from'])}")
            tcols[2].write(fmt_money(t["monthly_target"]))
            if tcols[3].button("Delete",
                               key=f"tdel_{t['person']}_{t['effective_from']}",
                               use_container_width=True):
                db.billing_target_delete(t["person"], t["effective_from"])
                st.rerun()

    # --- Historical import (SPEC 6.2) ---
    with st.expander(
        f"Historical import — Bill Number List "
        f"({db.billing_imports_count()} rows imported)"
    ):
        st.caption(
            "Upload the Bill Number List workbook. Every tab is parsed "
            "(fee note no., entity, client code, net, issued by, issued "
            "on); nothing is saved until you confirm the preview."
        )
        upload = st.file_uploader("Bill Number List workbook",
                                  type=["xlsx", "xlsm"], key="bnl_upload")
        if upload is not None:
            cache_key = f"bnl_parse_{upload.name}_{upload.size}"
            if cache_key not in st.session_state:
                with st.spinner("Parsing workbook..."):
                    try:
                        st.session_state[cache_key] = billing_import.parse_workbook(
                            upload.getbuffer().tobytes())
                    except Exception as exc:
                        st.session_state[cache_key] = {"error": str(exc)}
            parsed = st.session_state[cache_key]
            if parsed.get("error"):
                st.error(f"Couldn't parse the workbook: {parsed['error']}")
            else:
                st.write(f"**{len(parsed['rows'])}** rows parsed from "
                         f"{len(parsed['sheets_parsed'])} tab(s); "
                         f"{len(parsed['sheets_skipped'])} tab(s) skipped.")
                if parsed["sheets_skipped"]:
                    st.caption("Skipped (no recognisable header row): "
                               + ", ".join(parsed["sheets_skipped"][:20]))
                for issue in parsed["issues"][:30]:
                    st.warning(issue)
                if parsed["rows"]:
                    st.dataframe(pd.DataFrame(parsed["rows"][:20]),
                                 use_container_width=True, hide_index=True)
                    replace = st.checkbox(
                        "Replace all previously imported rows",
                        value=db.billing_imports_count() > 0)
                    if st.button(f"Import {len(parsed['rows'])} rows",
                                 type="primary"):
                        if replace:
                            db.billing_imports_clear()
                        n = db.billing_imports_add(parsed["rows"])
                        db.record_xero_event(user["id"], "billing_import",
                                             f"rows={n} file={upload.name}")
                        st.session_state.pop(cache_key, None)
                        st.session_state["dash_flash"] = (
                            "success", f"Imported {n} historical fee notes.")
                        st.rerun()


def approver_xero_settings(user):
    st.subheader("Xero settings")

    if not xero_client.is_configured():
        st.error(
            "Xero app credentials are missing. Set these environment "
            "variables on Railway (Variables tab), then redeploy:"
        )
        st.code(
            "XERO_CLIENT_ID=<from developer.xero.com>\n"
            "XERO_CLIENT_SECRET=<from developer.xero.com>\n"
            "XERO_REDIRECT_URI=<this app's exact URL, as registered on the Xero app>"
        )
        st.caption(
            "The Xero app needs these scopes: accounting.transactions, "
            "accounting.attachments, accounting.contacts.read, offline_access."
        )
        return

    if xero_client.is_connected():
        st.success("Xero is connected.")
        try:
            xero_client.refresh_connections()
        except Exception as exc:
            st.warning(f"Could not refresh the organisation list: {exc}")

        conns = db.xero_list_connections()
        st.write("**Connected organisations** — tag each as AA or CW so the "
                 "queue and reporting can badge by entity:")
        for c in conns:
            cols = st.columns([4, 3, 3])
            with cols[0]:
                st.write(f"**{c['tenant_name']}**")
                st.caption(f"Tenant {c['tenant_id'][:8]}…  |  "
                           f"Last sync: {format_timestamp(c['last_sync_at']) or 'never'}")
            with cols[1]:
                options = ["(untagged)", "AA", "CW"]
                current = c["entity"] or "(untagged)"
                choice = st.selectbox(
                    "Entity",
                    options,
                    index=options.index(current),
                    key=f"entity_{c['tenant_id']}",
                    label_visibility="collapsed",
                )
                if choice != current:
                    db.xero_set_connection_entity(
                        c["tenant_id"], None if choice == "(untagged)" else choice
                    )
                    st.rerun()

        st.divider()
        cols = st.columns([2, 2, 6])
        with cols[0]:
            st.link_button("Reconnect / add org", xero_client.build_consent_url(),
                           use_container_width=True)
        with cols[1]:
            if st.button("Disconnect Xero", use_container_width=True):
                xero_client.disconnect()
                db.record_xero_event(user["id"], "xero_disconnect",
                                     "Tokens removed from Hub")
                st.rerun()
        st.divider()
        st.write("**AA/CW entity mapping**")
        st.caption(
            "AA and CW live in one Xero org, so the Hub derives each "
            "draft's entity from line-item tracking options and nominal "
            "codes. Map the values seen in synced drafts below; drafts "
            "whose signals all point one way get their entity pre-selected "
            "at review (you can always override per invoice)."
        )
        tracking_seen, accounts_seen = set(), set()
        for d in (db.xero_list_drafts("PENDING_REVIEW", limit=1000)
                  + db.xero_recent_actioned(limit=200)):
            full = d if "line_items_json" in d else db.xero_get_draft(d["invoice_id"])
            sig = xero_pdf.draft_signals(full)
            tracking_seen.update(sig["tracking"])
            accounts_seen.update(sig["accounts"])
        entity_map = {(m["match_type"], m["match_value"]): m["entity"]
                      for m in db.xero_entity_map_all()}
        map_options = ["(unmapped)", "AA", "CW"]
        for match_type, values, label in [
            ("tracking", sorted(tracking_seen), "Tracking option"),
            ("account", sorted(accounts_seen), "Nominal code"),
        ]:
            for val in values:
                cols = st.columns([4, 3, 3])
                cols[0].write(f"{label}: **{val}**")
                current = entity_map.get((match_type, val), "(unmapped)")
                choice = cols[1].selectbox(
                    "Entity", map_options,
                    index=map_options.index(current),
                    key=f"emap_{match_type}_{val}",
                    label_visibility="collapsed",
                )
                if choice != current:
                    db.xero_entity_map_set(
                        match_type, val, None if choice == "(unmapped)" else choice
                    )
                    st.rerun()
        if not tracking_seen and not accounts_seen:
            st.info("No tracking options or nominal codes seen in synced drafts yet.")

        st.divider()
        st.write("**Raisers**")
        st.caption(
            "The initials the team puts in the invoice Reference when "
            "raising in BrightManager (e.g. LG/BT for shared credit). "
            "Email is used to notify a raiser when their fee note is "
            "rejected."
        )
        with st.form("new_raiser_form", clear_on_submit=True):
            rcols = st.columns([2, 4, 4, 2])
            nr_initials = rcols[0].text_input("Initials", max_chars=3, placeholder="LG")
            nr_name = rcols[1].text_input("Name", placeholder="Laura Green")
            nr_email = rcols[2].text_input("Email (optional)",
                                           placeholder="lg@astonsaccountants.co.uk")
            rcols[3].write("")
            add_raiser = rcols[3].form_submit_button("Add", use_container_width=True)
        if add_raiser:
            if not nr_initials.strip() or not nr_name.strip():
                st.error("Initials and name are both required.")
            elif not nr_initials.strip().isalpha() or len(nr_initials.strip()) < 2:
                st.error("Initials must be 2–3 letters.")
            else:
                db.xero_raiser_upsert(nr_initials, nr_name, nr_email)
                st.rerun()
        for r in db.xero_raisers_all():
            rcols = st.columns([2, 4, 4, 2])
            rcols[0].write(f"**{r['initials']}**")
            rcols[1].write(r["name"] + ("" if r["active"] else "  _(inactive)_"))
            rcols[2].caption(r["email"] or "no email")
            label = "Deactivate" if r["active"] else "Reactivate"
            if rcols[3].button(label, key=f"raiser_toggle_{r['initials']}",
                               use_container_width=True):
                db.xero_raiser_set_active(r["initials"], not bool(r["active"]))
                st.rerun()

        st.write("**Shared-credit split overrides**")
        st.caption(
            "Pairs split the net fee 50/50 unless overridden here. The "
            "percentage is the FIRST-named person's share (e.g. LG/BT at "
            "60% gives LG 60%, BT 40%)."
        )
        with st.form("new_split_form", clear_on_submit=True):
            scols = st.columns([3, 3, 2, 4])
            sp_pair = scols[0].text_input("Pair", placeholder="LG/BT")
            sp_share = scols[1].number_input("First person's %", min_value=1,
                                             max_value=99, value=50)
            scols[2].write("")
            add_split = scols[2].form_submit_button("Set", use_container_width=True)
        if add_split:
            pair_clean = sp_pair.strip().upper()
            if not re.fullmatch(r"[A-Z]{2,3}/[A-Z]{2,3}", pair_clean):
                st.error("Pair must look like LG/BT.")
            else:
                db.xero_split_override_set(pair_clean, sp_share / 100)
                st.rerun()
        for o in db.xero_split_overrides_all():
            scols = st.columns([3, 3, 2, 4])
            first, second = o["pair"].split("/")
            scols[0].write(f"**{o['pair']}**")
            scols[1].caption(f"{first} {o['first_share']:.0%} / "
                             f"{second} {1 - o['first_share']:.0%}")
            if scols[2].button("Remove", key=f"split_del_{o['pair']}",
                               use_container_width=True):
                db.xero_split_override_set(o["pair"], None)
                st.rerun()

        st.divider()
        st.write("**Watchdog & alerts**")
        st.caption(
            f"Daily sweep: drafts undecided after {xero_watchdog.WARN_DAYS} days "
            f"are emailed to {xero_watchdog.alert_recipient()}; "
            f"{xero_watchdog.ESCALATE_DAYS}+ days is an escalation. "
            "Weekly digest goes out on Mondays. Sweeps run automatically "
            "whenever an approver has the Hub open."
        )
        if xero_watchdog.email_configured():
            status = db.xero_kv_get("watchdog_email_status") or "not sent yet"
            if status == "ok":
                st.success("Alert email is configured and the last send succeeded.")
            else:
                st.warning(f"Alert email status: {status}")
        else:
            st.warning(
                "Alert emails are NOT configured — flags show in the Hub only. "
                "Add MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID and "
                "MS_SENDER_EMAIL to Railway (same values as the client "
                "onboarding app) to enable them."
            )
        wcols = st.columns([3, 3, 4])
        if wcols[0].button("Run sweep + send test digest now", use_container_width=True):
            xero_watchdog.run_daily_sweep(force=True)
            result = xero_watchdog.run_weekly_digest(force=True)
            if result.get("emailed"):
                st.success("Sweep ran and the digest email was sent.")
            else:
                st.warning("Sweep ran; email not sent (not configured or failed — "
                           "see status above / Exceptions).")
        digest = db.xero_kv_get("watchdog_last_digest_html")
        if digest:
            with st.expander("Latest digest (as emailed)"):
                st.markdown(digest, unsafe_allow_html=True)
    else:
        st.info("Xero is not connected yet.")
        st.link_button("Connect to Xero", xero_client.build_consent_url())
        st.caption(
            "You'll be sent to Xero to authorise the Invoice Hub app. If AA "
            "and CW are separate Xero organisations, run the connect flow "
            "once and select both (or connect a second time for the other org)."
        )


def render_approver_view(user):
    sidebar_counts(user)
    tabs = st.tabs([
        "Xero queue",
        "Dashboard",
        "Exceptions",
        "Users",
        "Audit",
        "Xero settings",
        "Legacy",
    ])
    with tabs[0]:
        approver_xero_queue(user)
    with tabs[1]:
        approver_dashboard(user)
    with tabs[2]:
        approver_xero_exceptions(user)
    with tabs[3]:
        approver_users(user)
    with tabs[4]:
        approver_audit(user)
    with tabs[5]:
        approver_xero_settings(user)
    with tabs[6]:
        st.caption(
            "The old upload workflow, kept for past records. All new fee "
            "notes flow BrightManager → Xero → the Xero queue."
        )
        legacy_pending = db.count_by_status().get("pending_approval", 0)
        if legacy_pending:
            st.warning(f"{legacy_pending} old upload(s) still pending — "
                       "approve/reject them here or ask the team to "
                       "re-raise in BrightManager.")
        with st.expander("Pending approvals", expanded=bool(legacy_pending)):
            approver_queue(user)
        with st.expander("Approved (awaiting send)"):
            approver_archive(user, "approved", "Approved (awaiting send)", "No approved invoices waiting.")
        with st.expander("Sent"):
            approver_archive(user, "sent", "Sent", "No sent invoices yet.")
        with st.expander("Rejected"):
            approver_archive(user, "rejected", "Rejected", "No rejected invoices.")


# === MAIN ===

def main():
    handle_xero_oauth_callback()
    user = auth.require_login()
    header(user)
    if user["role"] == "approver":
        render_approver_view(user)
    else:
        render_team_view(user)


main()
