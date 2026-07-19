"""
Authentication helpers for Astons Invoice Platform (v2).

Uses Streamlit session state. The user has to re-authenticate once per
browser session. Simple username + password stored hashed in SQLite.
"""

from pathlib import Path

import streamlit as st

import db


SESSION_KEY = "authed_user"


def current_user():
    """Return the currently logged-in user dict, or None."""
    return st.session_state.get(SESSION_KEY)


def logout():
    st.session_state.pop(SESSION_KEY, None)


def _login_form():
    """Render the login form. Does NOT stop the page — caller is responsible."""
    logo_path = Path(__file__).parent / "astons_logo.png"
    if logo_path.exists():
        # 200px = exact quarter of the 800px source, so it stays crisp
        st.image(str(logo_path), width=200)

    st.title("Astons Invoice Hub")
    st.caption("Please sign in to continue.")

    if not db.has_any_users():
        st.error(
            "No users exist yet. Set the `INITIAL_APPROVER_USERNAME`, "
            "`INITIAL_APPROVER_PASSWORD` and `INITIAL_APPROVER_NAME` environment "
            "variables on the Railway service and restart, so the first approver "
            "account is seeded automatically."
        )
        st.stop()

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if submitted:
        if not username or not password:
            st.error("Please enter both username and password.")
            return

        user = db.get_user_by_username(username)
        if not user or not db.verify_password(password, user["password_hash"]):
            st.error("Incorrect username or password.")
            return

        # Don't hold the password hash in session state — strip it out
        user.pop("password_hash", None)
        st.session_state[SESSION_KEY] = user
        db.record_login(user["id"])
        st.rerun()


def require_login():
    """If no user is logged in, render the login form and stop page render."""
    user = current_user()
    if user:
        return user
    _login_form()
    st.stop()


def require_role(role: str):
    """Require that the current user has a specific role. 403 if not."""
    user = require_login()
    if user["role"] != role:
        st.error("You don't have permission to view this page.")
        st.stop()
    return user
