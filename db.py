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

# xero_drafts hub_status lifecycle:
#   PENDING_REVIEW          - awaiting Ash's decision in the Xero queue
#   APPROVED                - AUTHORISED in Xero + branded PDF attached
#   APPROVED_NO_ATTACHMENT  - AUTHORISED in Xero but the attachment failed
#                             (visible flag per SPEC 2.2; retryable)
#   REJECTED                - DELETED in Xero, reason recorded
#   EXTERNAL_ACTION         - actioned outside the Hub (SPEC 2.1)
#   DISMISSED               - external action acknowledged by Ash
XERO_DRAFTS_CREATE = """
CREATE TABLE IF NOT EXISTS xero_drafts (
    invoice_id           TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL REFERENCES xero_connections(tenant_id),
    invoice_number       TEXT,
    reference            TEXT,
    contact_id           TEXT,
    contact_name         TEXT,
    line_items_json      TEXT,
    sub_total            REAL,
    total_tax            REAL,
    total                REAL,
    date                 TEXT,
    due_date             TEXT,
    updated_date_utc     TEXT,
    branding_theme_id    TEXT,
    xero_status          TEXT,
    hub_status           TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
                         CHECK(hub_status IN ('PENDING_REVIEW', 'EXTERNAL_ACTION', 'DISMISSED',
                                              'APPROVED', 'APPROVED_NO_ATTACHMENT', 'REJECTED')),
    external_action_note TEXT,
    entity               TEXT CHECK(entity IN ('AA', 'CW') OR entity IS NULL),
    decided_by_user_id   INTEGER REFERENCES users(id),
    decided_at           TEXT,
    reject_reason        TEXT,
    action_error         TEXT,
    branded_pdf          BLOB,
    branded_pdf_filename TEXT,
    raiser_pair          TEXT,
    split_json           TEXT,
    monthly              INTEGER,
    client_code          TEXT,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
)
"""

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

-- === XERO INTEGRATION (SPEC.md Phase 1) ===

-- Single token set per Xero app; covers all connected tenants.
CREATE TABLE IF NOT EXISTS xero_tokens (
    id            INTEGER PRIMARY KEY CHECK(id = 1),
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Small key/value store for OAuth state, poll throttling, etc.
CREATE TABLE IF NOT EXISTS xero_kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per connected Xero organisation. entity is Ash's AA/CW tag.
CREATE TABLE IF NOT EXISTS xero_connections (
    tenant_id    TEXT PRIMARY KEY,
    tenant_name  TEXT NOT NULL,
    entity       TEXT CHECK(entity IN ('AA', 'CW') OR entity IS NULL),
    connected_at TEXT NOT NULL,
    last_sync_at TEXT
);

-- Drafts polled from Xero. Keyed on Xero's InvoiceID (SPEC 2.1).
-- Definition lives in XERO_DRAFTS_CREATE so the v2 migration can rebuild
-- the table with the same DDL (SQLite cannot alter CHECK constraints).
%(xero_drafts_create)s

CREATE INDEX IF NOT EXISTS idx_xero_drafts_hub_status ON xero_drafts(hub_status);
CREATE INDEX IF NOT EXISTS idx_xero_drafts_tenant ON xero_drafts(tenant_id);

-- AA/CW entity derivation for the single-org setup: line-item tracking
-- options and account codes map to an entity (SPEC section 5).
CREATE TABLE IF NOT EXISTS xero_entity_map (
    match_type  TEXT NOT NULL CHECK(match_type IN ('tracking', 'account')),
    match_value TEXT NOT NULL,
    entity      TEXT NOT NULL CHECK(entity IN ('AA', 'CW')),
    PRIMARY KEY (match_type, match_value)
);

-- Raiser registry (SPEC section 3): the initials the team puts in the
-- invoice Reference, mapped to a person. Separate from Hub users — not
-- every raiser has a Hub login.
CREATE TABLE IF NOT EXISTS xero_raisers (
    initials TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    email    TEXT,
    active   INTEGER NOT NULL DEFAULT 1
);

-- Shared-credit split overrides: default is 50/50, a pair like 'AB/CD'
-- can override the first-named person's share (SPEC section 3).
CREATE TABLE IF NOT EXISTS xero_split_overrides (
    pair        TEXT PRIMARY KEY,
    first_share REAL NOT NULL CHECK(first_share > 0 AND first_share < 1)
);

-- Monthly billing targets (SPEC 4.1): per raiser initials or 'FIRM',
-- with effective-from dates so history is preserved.
CREATE TABLE IF NOT EXISTS billing_targets (
    person         TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    monthly_target REAL NOT NULL,
    PRIMARY KEY (person, effective_from)
);

-- Historical fee notes imported from the Bill Number List workbook
-- (SPEC 6.2) so trends and prior-year comparisons work from launch.
CREATE TABLE IF NOT EXISTS billing_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fee_note_no TEXT,
    entity      TEXT,
    client_code TEXT,
    client_name TEXT,
    net         REAL,
    issued_by   TEXT,
    issued_on   TEXT,
    monthly     INTEGER,
    source_tab  TEXT,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_billing_imports_issued ON billing_imports(issued_on);

-- Every poll attempt is logged; failures surface in the exceptions tab.
CREATE TABLE IF NOT EXISTS xero_sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT    NOT NULL,
    tenant_id  TEXT,
    ok         INTEGER NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,
    message    TEXT
);
""" % {"xero_drafts_create": XERO_DRAFTS_CREATE + ";"}


def _migrate_xero_drafts_v2(conn) -> None:
    """Rebuild xero_drafts for the Phase 2/3 statuses and decision
    columns. SQLite can't alter CHECK constraints, so an existing table
    created before 'APPROVED' was a valid status is renamed, recreated
    with the current DDL, and its rows copied across."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='xero_drafts'"
    ).fetchone()
    if not row or "'APPROVED'" in row["sql"]:
        return

    conn.execute("ALTER TABLE xero_drafts RENAME TO xero_drafts_old")
    conn.execute(XERO_DRAFTS_CREATE.replace("IF NOT EXISTS ", ""))
    common = (
        "invoice_id, tenant_id, invoice_number, reference, contact_name, "
        "line_items_json, sub_total, total_tax, total, date, due_date, "
        "updated_date_utc, branding_theme_id, xero_status, hub_status, "
        "external_action_note, first_seen_at, last_seen_at"
    )
    conn.execute(
        f"INSERT INTO xero_drafts ({common}) SELECT {common} FROM xero_drafts_old"
    )
    conn.execute("DROP TABLE xero_drafts_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xero_drafts_hub_status ON xero_drafts(hub_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xero_drafts_tenant ON xero_drafts(tenant_id)"
    )


def _ensure_drafts_columns(conn) -> None:
    """Additive columns (no CHECK changes) arrive via plain ALTERs so an
    existing table gains them without a rebuild."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(xero_drafts)")}
    for name, ddl in [
        ("raiser_pair", "TEXT"),
        ("split_json", "TEXT"),
        ("monthly", "INTEGER"),
        ("client_code", "TEXT"),
    ]:
        if name not in cols:
            conn.execute(f"ALTER TABLE xero_drafts ADD COLUMN {name} {ddl}")


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every app boot."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_xero_drafts_v2(conn)
        _ensure_drafts_columns(conn)

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


def find_active_by_invoice_no(invoice_no: str):
    """Return non-rejected invoices with this invoice number.

    Used as the duplicate-submission check. Rejected invoices are
    excluded so a team member can legitimately resubmit after a
    rejection.
    """
    if not invoice_no:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.created_at, i.status, i.invoice_no, i.client_name,
                   u.full_name AS created_by_name
            FROM invoices i
            JOIN users u ON i.created_by_user_id = u.id
            WHERE i.invoice_no = ? AND i.status != 'rejected'
            ORDER BY i.created_at DESC
            """,
            (invoice_no,),
        ).fetchall()
        return [dict(r) for r in rows]


def count_by_status() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM invoices GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# === XERO: TOKENS & KV ===

def xero_save_tokens(*, access_token: str, refresh_token: str, expires_at: str) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO xero_tokens (id, access_token, refresh_token, expires_at, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (access_token, refresh_token, expires_at, now),
        )


def xero_get_tokens():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM xero_tokens WHERE id = 1").fetchone()
        return dict(row) if row else None


def xero_delete_tokens() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM xero_tokens")


def xero_kv_set(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO xero_kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def xero_kv_get(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM xero_kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def xero_kv_delete(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM xero_kv WHERE key = ?", (key,))


# === XERO: CONNECTIONS ===

def xero_upsert_connection(tenant_id: str, tenant_name: str) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO xero_connections (tenant_id, tenant_name, connected_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET tenant_name = excluded.tenant_name
            """,
            (tenant_id, tenant_name, now),
        )


def xero_list_connections():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM xero_connections ORDER BY tenant_name"
        ).fetchall()
        return [dict(r) for r in rows]


def xero_set_connection_entity(tenant_id: str, entity) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE xero_connections SET entity = ? WHERE tenant_id = ?",
            (entity, tenant_id),
        )


def xero_set_last_sync(tenant_id: str, when_iso: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE xero_connections SET last_sync_at = ? WHERE tenant_id = ?",
            (when_iso, tenant_id),
        )


# === XERO: DRAFTS ===

def xero_upsert_draft(
    *,
    invoice_id: str,
    tenant_id: str,
    invoice_number: str,
    reference: str,
    contact_id: str,
    contact_name: str,
    line_items_json: str,
    sub_total,
    total_tax,
    total,
    date: str,
    due_date: str,
    updated_date_utc: str,
    branding_theme_id: str,
    xero_status: str,
) -> None:
    """Upsert one Xero draft keyed on InvoiceID. A draft that had been
    marked EXTERNAL_ACTION or DISMISSED but reappears as a live draft
    goes back to PENDING_REVIEW (e.g. authorised then reverted in Xero).
    Decided rows (APPROVED*/REJECTED) keep their decision — data fields
    update but the Hub's verdict is never clobbered by a poll."""
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO xero_drafts (
                invoice_id, tenant_id, invoice_number, reference, contact_id,
                contact_name, line_items_json, sub_total, total_tax, total,
                date, due_date, updated_date_utc, branding_theme_id, xero_status,
                hub_status, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_REVIEW', ?, ?)
            ON CONFLICT(invoice_id) DO UPDATE SET
                invoice_number = excluded.invoice_number,
                reference = excluded.reference,
                contact_id = excluded.contact_id,
                contact_name = excluded.contact_name,
                line_items_json = excluded.line_items_json,
                sub_total = excluded.sub_total,
                total_tax = excluded.total_tax,
                total = excluded.total,
                date = excluded.date,
                due_date = excluded.due_date,
                updated_date_utc = excluded.updated_date_utc,
                branding_theme_id = excluded.branding_theme_id,
                xero_status = excluded.xero_status,
                hub_status = CASE
                    WHEN xero_drafts.hub_status IN ('EXTERNAL_ACTION', 'DISMISSED')
                    THEN 'PENDING_REVIEW'
                    ELSE xero_drafts.hub_status END,
                external_action_note = CASE
                    WHEN xero_drafts.hub_status IN ('EXTERNAL_ACTION', 'DISMISSED')
                    THEN NULL
                    ELSE xero_drafts.external_action_note END,
                last_seen_at = excluded.last_seen_at
            """,
            (
                invoice_id, tenant_id, invoice_number, reference, contact_id,
                contact_name, line_items_json, sub_total, total_tax, total,
                date, due_date, updated_date_utc, branding_theme_id, xero_status,
                now, now,
            ),
        )


def xero_touch_drafts_seen(invoice_ids: list) -> None:
    if not invoice_ids:
        return
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.executemany(
            "UPDATE xero_drafts SET last_seen_at = ? WHERE invoice_id = ?",
            [(now, iid) for iid in invoice_ids],
        )


def xero_list_drafts(hub_status: str, limit: int = 500):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.*, c.tenant_name, c.entity
            FROM xero_drafts d
            JOIN xero_connections c ON d.tenant_id = c.tenant_id
            WHERE d.hub_status = ?
            ORDER BY d.date DESC, d.invoice_number DESC
            LIMIT ?
            """,
            (hub_status, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def xero_pending_ids_for_tenant(tenant_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT invoice_id FROM xero_drafts "
            "WHERE tenant_id = ? AND hub_status = 'PENDING_REVIEW'",
            (tenant_id,),
        ).fetchall()
        return [r["invoice_id"] for r in rows]


def xero_mark_external_action(invoice_id: str, note: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE xero_drafts
            SET hub_status = 'EXTERNAL_ACTION', external_action_note = ?
            WHERE invoice_id = ?
            """,
            (note, invoice_id),
        )


def xero_dismiss_draft(invoice_id: str, user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE xero_drafts SET hub_status = 'DISMISSED' WHERE invoice_id = ?",
            (invoice_id,),
        )
        _write_audit(conn, user_id, None, "xero_dismiss_exception",
                     f"xero_invoice_id={invoice_id}")


def xero_get_draft(invoice_id: str):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT d.*, c.tenant_name, c.entity AS tenant_entity
            FROM xero_drafts d
            JOIN xero_connections c ON d.tenant_id = c.tenant_id
            WHERE d.invoice_id = ?
            """,
            (invoice_id,),
        ).fetchone()
        return dict(row) if row else None


def xero_mark_approved(invoice_id: str, user_id: int, entity: str,
                       pdf_bytes: bytes, pdf_filename: str,
                       attachment_ok: bool, error: str = None,
                       raiser_pair: str = None, split_json: str = None,
                       monthly: bool = None, client_code: str = None) -> None:
    now = dt.datetime.utcnow().isoformat()
    status = "APPROVED" if attachment_ok else "APPROVED_NO_ATTACHMENT"
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE xero_drafts
            SET hub_status = ?, entity = ?, decided_by_user_id = ?, decided_at = ?,
                action_error = ?, branded_pdf = ?, branded_pdf_filename = ?,
                raiser_pair = ?, split_json = ?, monthly = ?, client_code = ?,
                xero_status = 'AUTHORISED'
            WHERE invoice_id = ?
            """,
            (status, entity, user_id, now, error, pdf_bytes, pdf_filename,
             raiser_pair, split_json,
             None if monthly is None else (1 if monthly else 0),
             client_code, invoice_id),
        )
        _write_audit(conn, user_id, None, "xero_approve",
                     f"xero_invoice_id={invoice_id} entity={entity} "
                     f"raisers={raiser_pair or '-'} monthly={monthly} "
                     f"attachment={'ok' if attachment_ok else 'FAILED: ' + str(error)}")


def xero_mark_attachment_retried(invoice_id: str, user_id: int,
                                 ok: bool, error: str = None) -> None:
    with get_conn() as conn:
        if ok:
            conn.execute(
                "UPDATE xero_drafts SET hub_status = 'APPROVED', action_error = NULL "
                "WHERE invoice_id = ?",
                (invoice_id,),
            )
        else:
            conn.execute(
                "UPDATE xero_drafts SET action_error = ? WHERE invoice_id = ?",
                (error, invoice_id),
            )
        _write_audit(conn, user_id, None, "xero_attachment_retry",
                     f"xero_invoice_id={invoice_id} {'ok' if ok else 'failed: ' + str(error)}")


def xero_mark_rejected(invoice_id: str, user_id: int, reason: str) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE xero_drafts
            SET hub_status = 'REJECTED', decided_by_user_id = ?, decided_at = ?,
                reject_reason = ?, xero_status = 'DELETED'
            WHERE invoice_id = ?
            """,
            (user_id, now, reason, invoice_id),
        )
        _write_audit(conn, user_id, None, "xero_reject",
                     f"xero_invoice_id={invoice_id} reason={reason}")


def xero_recent_actioned(limit: int = 50):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.invoice_id, d.invoice_number, d.contact_name, d.total,
                   d.hub_status, d.entity, d.decided_at, d.reject_reason,
                   d.action_error, d.branded_pdf_filename,
                   u.full_name AS decided_by_name
            FROM xero_drafts d
            LEFT JOIN users u ON d.decided_by_user_id = u.id
            WHERE d.hub_status IN ('APPROVED', 'APPROVED_NO_ATTACHMENT', 'REJECTED')
            ORDER BY d.decided_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# === XERO: ENTITY MAP (AA/CW from tracking options / account codes) ===

def xero_entity_map_all():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM xero_entity_map").fetchall()
        return [dict(r) for r in rows]


def xero_entity_map_set(match_type: str, match_value: str, entity) -> None:
    """entity None removes the mapping."""
    with get_conn() as conn:
        if entity is None:
            conn.execute(
                "DELETE FROM xero_entity_map WHERE match_type = ? AND match_value = ?",
                (match_type, match_value),
            )
        else:
            conn.execute(
                """
                INSERT INTO xero_entity_map (match_type, match_value, entity)
                VALUES (?, ?, ?)
                ON CONFLICT(match_type, match_value) DO UPDATE SET entity = excluded.entity
                """,
                (match_type, match_value, entity),
            )


def xero_count_drafts() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT hub_status, COUNT(*) AS n FROM xero_drafts GROUP BY hub_status"
        ).fetchall()
        return {r["hub_status"]: r["n"] for r in rows}


# === XERO: RAISERS & SPLITS (SPEC section 3) ===

def xero_raisers_all(active_only: bool = False):
    with get_conn() as conn:
        sql = "SELECT * FROM xero_raisers"
        if active_only:
            sql += " WHERE active = 1"
        rows = conn.execute(sql + " ORDER BY initials").fetchall()
        return [dict(r) for r in rows]


def xero_raiser_upsert(initials: str, name: str, email: str = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO xero_raisers (initials, name, email, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(initials) DO UPDATE SET
                name = excluded.name, email = excluded.email
            """,
            (initials.strip().upper(), name.strip(), (email or "").strip() or None),
        )


def xero_raiser_set_active(initials: str, active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE xero_raisers SET active = ? WHERE initials = ?",
            (1 if active else 0, initials),
        )


def xero_split_overrides_all():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM xero_split_overrides ORDER BY pair"
        ).fetchall()
        return [dict(r) for r in rows]


def xero_split_override_set(pair: str, first_share) -> None:
    """first_share None removes the override (back to 50/50)."""
    with get_conn() as conn:
        if first_share is None:
            conn.execute("DELETE FROM xero_split_overrides WHERE pair = ?", (pair,))
        else:
            conn.execute(
                """
                INSERT INTO xero_split_overrides (pair, first_share) VALUES (?, ?)
                ON CONFLICT(pair) DO UPDATE SET first_share = excluded.first_share
                """,
                (pair.strip().upper(), float(first_share)),
            )


# === BILLING: TARGETS & IMPORTS (SPEC 4 / 6.2) ===

def billing_targets_all():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM billing_targets ORDER BY person, effective_from DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def billing_target_set(person: str, effective_from: str, monthly_target) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO billing_targets (person, effective_from, monthly_target)
            VALUES (?, ?, ?)
            ON CONFLICT(person, effective_from) DO UPDATE SET
                monthly_target = excluded.monthly_target
            """,
            (person.strip().upper(), effective_from, float(monthly_target)),
        )


def billing_target_delete(person: str, effective_from: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM billing_targets WHERE person = ? AND effective_from = ?",
            (person, effective_from),
        )


def billing_target_for(person: str, month_start: str, targets: list = None):
    """Target in force for the month starting month_start (latest
    effective_from <= month_start). None if never set."""
    rows = targets if targets is not None else billing_targets_all()
    candidates = [t for t in rows
                  if t["person"] == person and t["effective_from"] <= month_start]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t["effective_from"])["monthly_target"]


def billing_imports_add(rows: list) -> int:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO billing_imports (
                fee_note_no, entity, client_code, client_name, net,
                issued_by, issued_on, monthly, source_tab, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(r.get("fee_note_no"), r.get("entity"), r.get("client_code"),
              r.get("client_name"), r.get("net"), r.get("issued_by"),
              r.get("issued_on"), r.get("monthly"), r.get("source_tab"), now)
             for r in rows],
        )
    return len(rows)


def billing_imports_clear() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM billing_imports")
        return cur.rowcount


def billing_imports_range(start: str, end: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM billing_imports WHERE issued_on >= ? AND issued_on < ? "
            "ORDER BY issued_on",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def billing_imports_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM billing_imports").fetchone()["n"]


def xero_approved_range(start: str, end: str):
    """Hub-approved Xero invoices with invoice DATE in [start, end)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT invoice_id, invoice_number, contact_name, client_code,
                   sub_total, total_tax, total, date, entity, raiser_pair,
                   split_json, monthly, decided_at
            FROM xero_drafts
            WHERE hub_status IN ('APPROVED', 'APPROVED_NO_ATTACHMENT')
              AND date >= ? AND date < ?
            ORDER BY date
            """,
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


# === XERO: SYNC LOG ===

def xero_log_sync(tenant_id, ok: bool, fetched: int, message: str = None) -> None:
    now = dt.datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO xero_sync_log (created_at, tenant_id, ok, fetched, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, tenant_id, 1 if ok else 0, fetched, message),
        )


def xero_recent_sync_failures(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.*, c.tenant_name
            FROM xero_sync_log s
            LEFT JOIN xero_connections c ON s.tenant_id = c.tenant_id
            WHERE s.ok = 0
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def xero_last_sync_time():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS t FROM xero_sync_log WHERE ok = 1"
        ).fetchone()
        return row["t"] if row else None


# === AUDIT ===

def _write_audit(conn, user_id, invoice_id, action, note=None):
    now = dt.datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO audit_log (created_at, user_id, invoice_id, action, note) VALUES (?, ?, ?, ?, ?)",
        (now, user_id, invoice_id, action, note),
    )


def record_xero_event(user_id: int, action: str, note: str = None) -> None:
    with get_conn() as conn:
        _write_audit(conn, user_id, None, action, note)


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
