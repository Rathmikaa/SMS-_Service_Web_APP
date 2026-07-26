"""
db.py - tiny SQLite wrapper for the GCC SMS app.
No ORM needed; the schema is small and the app is single-tenant.
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "app.db"))


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_number TEXT UNIQUE NOT NULL,
            sl_no TEXT,
            dn TEXT,
            owner_name TEXT,
            new_door_no TEXT,
            old_door_no TEXT,
            street TEXT,
            mobile TEXT,
            property_type TEXT,
            property_usage TEXT,
            current_tax_due INTEGER DEFAULT 0,
            arrear_due INTEGER DEFAULT 0,
            balance_amount INTEGER DEFAULT 0,
            remarks TEXT,
            source_file TEXT,
            needs_review INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            last_sent_at TEXT,
            imported_at TEXT
        );

        CREATE TABLE IF NOT EXISTS send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER REFERENCES records(id),
            mobile TEXT,
            message TEXT,
            success INTEGER,
            dry_run INTEGER,
            provider TEXT,
            provider_response TEXT,
            sent_at TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def update_record(record_id, r):
    """Update an existing record in place (used by the manual-edit form).
    Unlike upsert_records, this updates by primary key id, not bill_number,
    so editing a record's bill number doesn't create a duplicate row."""
    conn = get_conn()
    conn.execute(
        """UPDATE records SET
            bill_number=?, sl_no=?, dn=?, owner_name=?, new_door_no=?, old_door_no=?,
            street=?, mobile=?, property_type=?, property_usage=?,
            current_tax_due=?, arrear_due=?, balance_amount=?, remarks=?
           WHERE id=?""",
        (
            r["bill_number"], r["sl_no"], r["dn"], r["owner_name"], r["new_door_no"], r["old_door_no"],
            r["street"], r["mobile"], r["property_type"], r["property_usage"],
            r["current_tax_due"], r["arrear_due"], r["balance_amount"], r["remarks"], record_id,
        ),
    )
    conn.commit()
    conn.close()


def upsert_records(records):
    """Insert or update records keyed by bill_number.
    If a duplicate bill_number is imported again (e.g. the same property
    appears in two overlapping report PDFs), the newer row's non-empty
    fields win, but we never let a good mobile number get overwritten by
    a blank one."""
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    inserted, updated = 0, 0
    for r in records:
        existing = conn.execute(
            "SELECT id, mobile FROM records WHERE bill_number = ?", (r["bill_number"],)
        ).fetchone()
        mobile = r.get("mobile", "")
        if existing:
            keep_mobile = mobile or existing["mobile"]
            conn.execute(
                """UPDATE records SET
                    sl_no=?, dn=?, owner_name=?, new_door_no=?, old_door_no=?,
                    street=?, mobile=?, property_type=?, property_usage=?,
                    current_tax_due=?, arrear_due=?, balance_amount=?,
                    remarks=?, source_file=?, needs_review=?
                   WHERE bill_number=?""",
                (
                    r["sl_no"], r["dn"], r["owner_name"], r["new_door_no"], r["old_door_no"],
                    r["street"], keep_mobile, r["property_type"], r["property_usage"],
                    r["current_tax_due"], r["arrear_due"], r["balance_amount"],
                    r["remarks"], r["source_file"], int(r.get("needs_review", False)), r["bill_number"],
                ),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO records
                   (bill_number, sl_no, dn, owner_name, new_door_no, old_door_no, street,
                    mobile, property_type, property_usage, current_tax_due, arrear_due,
                    balance_amount, remarks, source_file, needs_review, status, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                (
                    r["bill_number"], r["sl_no"], r["dn"], r["owner_name"], r["new_door_no"],
                    r["old_door_no"], r["street"], mobile, r["property_type"], r["property_usage"],
                    r["current_tax_due"], r["arrear_due"], r["balance_amount"], r["remarks"],
                    r["source_file"], int(r.get("needs_review", False)), now,
                ),
            )
            inserted += 1
    conn.commit()
    conn.close()
    return inserted, updated


def list_records(filter_mobile=None, status=None, search=None, review_only=False):
    conn = get_conn()
    q = "SELECT * FROM records WHERE 1=1"
    params = []
    if filter_mobile == "has":
        q += " AND mobile != ''"
    elif filter_mobile == "missing":
        q += " AND mobile = ''"
    if review_only:
        q += " AND needs_review = 1"
    if status:
        q += " AND status = ?"
        params.append(status)
    if search:
        q += " AND (owner_name LIKE ? OR street LIKE ? OR bill_number LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like])
    q += " ORDER BY CAST(sl_no AS INTEGER) ASC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_records_by_ids(ids):
    if not ids:
        return []
    conn = get_conn()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM records WHERE id IN ({placeholders})", ids).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_status(record_id, status):
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE records SET status=?, last_sent_at=? WHERE id=?", (status, now, record_id)
    )
    conn.commit()
    conn.close()


def log_send(record_id, mobile, message, success, dry_run, provider, provider_response):
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO send_log (record_id, mobile, message, success, dry_run, provider,
                                  provider_response, sent_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (record_id, mobile, message, int(success), int(dry_run), provider, provider_response, now),
    )
    conn.commit()
    conn.close()


def list_logs(limit=500):
    conn = get_conn()
    rows = conn.execute(
        """SELECT send_log.*, records.owner_name, records.bill_number
           FROM send_log JOIN records ON send_log.record_id = records.id
           ORDER BY send_log.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    with_mobile = conn.execute("SELECT COUNT(*) c FROM records WHERE mobile != ''").fetchone()["c"]
    sent = conn.execute("SELECT COUNT(*) c FROM records WHERE status='sent'").fetchone()["c"]
    failed = conn.execute("SELECT COUNT(*) c FROM records WHERE status='failed'").fetchone()["c"]
    total_balance = conn.execute("SELECT COALESCE(SUM(balance_amount),0) s FROM records").fetchone()["s"]
    needs_review = conn.execute("SELECT COUNT(*) c FROM records WHERE needs_review=1").fetchone()["c"]
    conn.close()
    return {
        "total": total,
        "with_mobile": with_mobile,
        "missing_mobile": total - with_mobile,
        "sent": sent,
        "failed": failed,
        "total_balance": total_balance,
        "needs_review": needs_review,
    }


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()