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
import os
import re
import tempfile
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

import streamlit as st

import db
import auth
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
    page_title="Astons Invoice Platform",
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
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y %H:%M")
    except Exception:
        return iso


def header(user):
    logo_path = Path(__file__).parent / "astons_logo.png"
    cols = st.columns([1, 4, 2])
    with cols[0]:
        if logo_path.exists():
            st.image(str(logo_path), width=120)
    with cols[1]:
        st.title("Invoice Platform")
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


def sidebar_counts(user):
    counts = db.count_by_status()
    pending = counts.get("pending_approval", 0)
    approved = counts.get("approved", 0)
    sent = counts.get("sent", 0)
    rejected = counts.get("rejected", 0)

    with st.sidebar:
        st.subheader("Status")
        st.metric("Pending approval", pending)
        st.metric("Approved (ready to send)", approved)
        st.metric("Sent", sent)
        st.metric("Rejected", rejected)
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


def render_team_view(user):
    sidebar_counts(user)
    tabs = st.tabs(["New submission", "My invoices"])
    with tabs[0]:
        team_new_submission(user)
    with tabs[1]:
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
            nu_password = st.text_input("Temporary password", type="password")
            nu_role = st.selectbox("Role", options=["team_member", "approver"])
            submitted = st.form_submit_button("Create user", type="primary")
        if submitted:
            if not nu_username or not nu_password or not nu_full_name:
                st.error("All fields are required.")
            elif db.get_user_by_username(nu_username):
                st.error(f"A user with username '{nu_username}' already exists.")
            else:
                db.create_user(
                    username=nu_username,
                    password=nu_password,
                    full_name=nu_full_name,
                    role=nu_role,
                )
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


def render_approver_view(user):
    sidebar_counts(user)
    tabs = st.tabs([
        "Pending approvals",
        "Approved",
        "Sent",
        "Rejected",
        "Users",
        "Audit",
    ])
    with tabs[0]:
        approver_queue(user)
    with tabs[1]:
        approver_archive(user, "approved", "Approved (awaiting send)", "No approved invoices waiting.")
    with tabs[2]:
        approver_archive(user, "sent", "Sent", "No sent invoices yet.")
    with tabs[3]:
        approver_archive(user, "rejected", "Rejected", "No rejected invoices.")
    with tabs[4]:
        approver_users(user)
    with tabs[5]:
        approver_audit(user)


# === MAIN ===

def main():
    user = auth.require_login()
    header(user)
    if user["role"] == "approver":
        render_approver_view(user)
    else:
        render_team_view(user)


main()
