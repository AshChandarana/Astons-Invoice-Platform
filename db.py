"""
Database layer for Astons Invoice Platform (v2).

Uses SQLite. Database path is controlled by the DATABASE_PATH env var
(default ./astons.db for local dev). On Railway this should point to a
mounted volume, e.g. /data/astons.db, so data survives restarts and
redeploys.
"""

import os
import sqlite3
import datetime as dt
from pathlib import Path
from contextlib import contextmanager

import bcrypt


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", "./astons.db")


def _connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# === SCHEMA ===

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    full_name       TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK(role IN ('team_member', 'approver')),
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT    NOT NULL,
    created_by_user_id      INTEGER NOT NULL REFERENCES users(id),
    portfolio               TEXT    NOT NULL CHECK(portfolio IN ('A', 'C')),
    client_name             TEXT    NOT NULL,
    invoice_no              TEXT    NOT NULL,
    total                   TEXT    NOT NULL,
    status                  TEXT    NOT NULL CHECK(status IN ('pending_approval', 'approved', 'rejected', 'sent')),
    source_pdf              BLOB    NOT NULL,
    source_pdf_filename     TEXT    NOT NULL,
    branded_pdf             BLOB    NOT NULL,
    branded_pdf_filename    TEXT    NOT NULL,
    approved_by_user_id     INTEGER REFERENCES users(id),
    approved_at             TEXT,
    rejection_note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_created_by ON invoices(created_by_user_id);
CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    user_id      INTEGER REFERENCES users(id),
    invoice_id   INTEGER REFERENCES invoices(id),
    action       TEXT    NOT NULL,
    note         TEXT
);
"""


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every app boot."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)

    # Seed initial approver if DB has no users at all
    seed_initial_user_if_empty()


def seed_initial_user_if_empty() -> None:
    """
    On first boot (empty users table), create one approver account from
    environment variables. This is the only way the app bootstraps a
    first user — we never ship default credentials.

    Required env vars (only used on first boot):
      INITIAL_APPROVER_USERNAME
      INITIAL_APPROVER_PASSWORD
      INITIAL_APPROVER_NAME
    """
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] > 0:
            return

    username = os.environ.get("INITIAL_APPROVER_USERNAME")
    password = os.environ.get("INITIAL_APPROVER_PASSWORD")
    name = os.environ.get("INITIAL_APPROVER_NAME", "Administrator")

    if not username or not password:
        # No env vars set — leave DB empty. The UI will show a first-run
        # message explaining how to set them.
        return

    create_user(
        username=username,
        password=password,
        full_name=name,
        role="approver",
    )


# === PASSWORDS ===

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# === USERS ===

def create_user(username: str, password: str, full_name: str, role: str) -> int:
    if role not in ("team_member", "approver"):
        raise ValueError("role must be 'team_member' or 'approver'")
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, full_name, role, active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (username.strip().lower(), hash_password(password), full_name.strip(), role, now),
        )
        return cur.lastrowid


def get_user_by_username(username: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1",
            (username.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None


def list_users():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, full_name, role, active, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def set_user_active(user_id: int, active: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))


def reset_user_password(user_id: int, new_password: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )


def has_any_users() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return row["n"] > 0


# === INVOICES ===

def create_invoice(
    *,
    created_by_user_id: int,
    portfolio: str,
    client_name: str,
    invoice_no: str,
    total: str,
    source_pdf: bytes,
    source_pdf_filename: str,
    branded_pdf: bytes,
    branded_pdf_filename: str,
) -> int:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                created_at, created_by_user_id, portfolio,
                client_name, invoice_no, total,
                status, source_pdf, source_pdf_filename,
                branded_pdf, branded_pdf_filename
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?, ?, ?)
            """,
            (
                now, created_by_user_id, portfolio,
                client_name, invoice_no, total,
                source_pdf, source_pdf_filename,
                branded_pdf, branded_pdf_filename,
            ),
        )
        invoice_id = cur.lastrowid
        _write_audit(conn, created_by_user_id, invoice_id, "submit", f"portfolio={portfolio}")
        return invoice_id


def list_invoices_by_status(status: str, limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.created_at, i.portfolio, i.client_name, i.invoice_no, i.total,
                   i.status, i.source_pdf_filename, i.branded_pdf_filename,
                   u.username AS created_by_username, u.full_name AS created_by_name,
                   au.username AS approved_by_username, au.full_name AS approved_by_name,
                   i.approved_at, i.rejection_note
            FROM invoices i
            JOIN users u ON i.created_by_user_id = u.id
            LEFT JOIN users au ON i.approved_by_user_id = au.id
            WHERE i.status = ?
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_invoices_for_user(user_id: int, limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.created_at, i.portfolio, i.client_name, i.invoice_no, i.total,
                   i.status, i.source_pdf_filename, i.branded_pdf_filename,
                   u.username AS created_by_username, u.full_name AS created_by_name,
                   au.username AS approved_by_username, au.full_name AS approved_by_name,
                   i.approved_at, i.rejection_note
            FROM invoices i
            JOIN users u ON i.created_by_user_id = u.id
            LEFT JOIN users au ON i.approved_by_user_id = au.id
            WHERE i.created_by_user_id = ?
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_invoice(invoice_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return dict(row) if row else None


def approve_invoice(invoice_id: int, approver_user_id: int) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE invoices
            SET status = 'approved', approved_by_user_id = ?, approved_at = ?
            WHERE id = ? AND status = 'pending_approval'
            """,
            (approver_user_id, now, invoice_id),
        )
        _write_audit(conn, approver_user_id, invoice_id, "approve")


def reject_invoice(invoice_id: int, approver_user_id: int, note: str) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE invoices
            SET status = 'rejected', approved_by_user_id = ?, approved_at = ?, rejection_note = ?
            WHERE id = ? AND status = 'pending_approval'
            """,
            (approver_user_id, now, note, invoice_id),
        )
        _write_audit(conn, approver_user_id, invoice_id, "reject", note)


def mark_invoice_sent(invoice_id: int, user_id: int) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE invoices SET status = 'sent' WHERE id = ? AND status = 'approved'
            """,
            (invoice_id,),
        )
        _write_audit(conn, user_id, invoice_id, "mark_sent")


def count_by_status() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM invoices GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# === AUDIT ===

def _write_audit(conn, user_id, invoice_id, action, note=None):
    now = dt.datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO audit_log (created_at, user_id, invoice_id, action, note) VALUES (?, ?, ?, ?, ?)",
        (now, user_id, invoice_id, action, note),
    )


def record_login(user_id: int) -> None:
    with get_conn() as conn:
        _write_audit(conn, user_id, None, "login")


def record_download(user_id: int, invoice_id: int) -> None:
    with get_conn() as conn:
        _write_audit(conn, user_id, invoice_id, "download")


def recent_audit(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.created_at, a.action, a.note,
                   u.username, u.full_name,
                   a.invoice_id
            FROM audit_log a
            LEFT JOIN users u ON a.user_id = u.id
            ORDER BY a.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
