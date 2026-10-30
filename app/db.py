"""SQLite storage. Everything lives in data/penny.db on your machine."""
import gzip
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from .categorize import COLOR_SLOTS, DEFAULT_CATEGORIES, DEFAULT_COLOR_SLOTS, LATER_DEFAULT_CATEGORIES, description_key

DATA_DIR = Path(os.environ.get("PENNY_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "penny.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT PRIMARY KEY,
    org_name          TEXT,
    name              TEXT NOT NULL,            -- the bank's name; syncs overwrite it
    nickname          TEXT,                     -- your name for it, shown instead
    currency          TEXT NOT NULL DEFAULT 'USD',
    balance           REAL NOT NULL DEFAULT 0,
    available_balance REAL,
    balance_date      INTEGER,
    hidden            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    posted      INTEGER NOT NULL,           -- unix seconds
    amount      REAL NOT NULL,              -- negative = money out
    description TEXT NOT NULL,
    payee       TEXT,
    merchant    TEXT NOT NULL,              -- normalized key used for grouping
    category    TEXT NOT NULL,              -- auto-assigned category
    pending     INTEGER NOT NULL DEFAULT 0,
    affects_balance INTEGER NOT NULL DEFAULT 1  -- 0 for imports that lack card payments
);
CREATE INDEX IF NOT EXISTS idx_tx_posted   ON transactions(posted);
CREATE INDEX IF NOT EXISTS idx_tx_merchant ON transactions(merchant);

-- One balance per account per day, recorded at each sync. Drives net worth history.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    day        TEXT NOT NULL,               -- YYYY-MM-DD
    balance    REAL NOT NULL,
    PRIMARY KEY (account_id, day)
);

-- User overrides: "always put this merchant in this category".
CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

-- Rules for some of a merchant's transactions: those whose description
-- contains `pattern` (compared via description_key, so digits don't count).
-- They win over the merchant's rule; the longest matching pattern wins.
CREATE TABLE IF NOT EXISTS description_rules (
    merchant TEXT NOT NULL,
    pattern  TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (merchant, pattern)
);

-- Merchants you combined: transactions under `alias` count as `merchant`.
-- Transactions keep their own merchant, so combining can be undone.
CREATE TABLE IF NOT EXISTS merchant_aliases (
    alias    TEXT PRIMARY KEY,
    merchant TEXT NOT NULL
);

-- One transaction put in a category by hand. Wins over every rule.
CREATE TABLE IF NOT EXISTS transaction_categories (
    tx_id    TEXT PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
    category TEXT NOT NULL
);

-- Profile pictures for merchants, resized in the browser before upload.
CREATE TABLE IF NOT EXISTS merchant_logos (
    merchant TEXT PRIMARY KEY,
    mime     TEXT NOT NULL,
    image    BLOB NOT NULL,
    version  INTEGER NOT NULL             -- changes on each upload, for cache busting
);

-- User overrides for detected subscriptions ('ignored').
CREATE TABLE IF NOT EXISTS subscription_overrides (
    merchant TEXT PRIMARY KEY,  -- the subscription's key: a merchant, or "merchant|price"
    status   TEXT NOT NULL
);

-- Recurring charges the user moved between bills and subscriptions.
CREATE TABLE IF NOT EXISTS recurring_kinds (
    key  TEXT PRIMARY KEY,      -- same keys as subscription_overrides
    kind TEXT NOT NULL CHECK (kind IN ('bill', 'subscription'))
);

-- The user's categories. color_slot is 1-20 (a palette color) or NULL (gray, past 20).
CREATE TABLE IF NOT EXISTS categories (
    name       TEXT PRIMARY KEY,
    position   INTEGER NOT NULL,
    color_slot INTEGER UNIQUE
);

-- When a category is deleted, transactions that would have been auto-assigned
-- to it go to its replacement instead.
CREATE TABLE IF NOT EXISTS category_aliases (
    old_name TEXT PRIMARY KEY,
    new_name TEXT NOT NULL
);

-- Every raw download from SimpleFIN and every imported file, kept as
-- received (gzipped) so transactions can be rebuilt if anything goes wrong.
CREATE TABLE IF NOT EXISTS raw_archive (
    sha256      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- 'simplefin', 'csv' or 'pdf'
    name        TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    data        BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # FastAPI may open and close a request's connection on different threads;
    # each request gets its own connection, so sharing across threads is safe.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("desc_key", 1, description_key, deterministic=True)
    conn.executescript(SCHEMA)
    # Databases created before affects_balance existed need the column added.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "affects_balance" not in columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN affects_balance INTEGER NOT NULL DEFAULT 1")
    if "nickname" not in {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}:
        conn.execute("ALTER TABLE accounts ADD COLUMN nickname TEXT")
    # The DB holds your SimpleFIN access URL, so keep it readable only by you.
    os.chmod(DB_PATH, 0o600)
    if not get_setting(conn, "categories_seeded"):
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO categories (name, position, color_slot) VALUES (?, ?, ?)",
                [(c, i, DEFAULT_COLOR_SLOTS.get(c)) for i, c in enumerate(DEFAULT_CATEGORIES)])
            set_setting(conn, "categories_seeded", 1)
            for name in LATER_DEFAULT_CATEGORIES:
                set_setting(conn, f"category_added:{name}", 1)
    # Defaults added later go in once, in their usual spot. If you delete one, it stays deleted.
    for name, before in LATER_DEFAULT_CATEGORIES.items():
        if get_setting(conn, f"category_added:{name}"):
            continue
        with conn:
            if not conn.execute("SELECT 1 FROM categories WHERE lower(name) = lower(?)", (name,)).fetchone():
                at = conn.execute("SELECT COALESCE((SELECT position FROM categories WHERE name = ?), "
                                  "(SELECT MAX(position) + 1 FROM categories), 0)", (before,)).fetchone()[0]
                conn.execute("UPDATE categories SET position = position + 1 WHERE position >= ?", (at,))
                conn.execute("INSERT INTO categories (name, position) VALUES (?, ?)", (name, at))
                conn.execute("DELETE FROM category_aliases WHERE old_name = ?", (name,))
            set_setting(conn, f"category_added:{name}", 1)
    # Categories from before there were enough colors may still be gray.
    gray = conn.execute("SELECT name FROM categories WHERE color_slot IS NULL ORDER BY position").fetchall()
    if gray:
        used = {r[0] for r in conn.execute("SELECT color_slot FROM categories WHERE color_slot IS NOT NULL")}
        free = [s for s in range(1, COLOR_SLOTS + 1) if s not in used]
        with conn:
            conn.executemany("UPDATE categories SET color_slot = ? WHERE name = ?",
                             [(slot, r["name"]) for slot, r in zip(free, gray)])
    return conn


def category_names(conn) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY position")]


def resolve_category(conn, category: str) -> str:
    """Map an auto-assigned category onto one that still exists."""
    row = conn.execute("SELECT new_name FROM category_aliases WHERE old_name = ?", (category,)).fetchone()
    if row:
        category = row["new_name"]
    exists = conn.execute("SELECT 1 FROM categories WHERE name = ?", (category,)).fetchone()
    return category if exists else "Other"


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def clear_financial_data(conn):
    for table in ("transactions", "balance_snapshots", "accounts"):
        conn.execute(f"DELETE FROM {table}")
    set_setting(conn, "last_sync", None)
    set_setting(conn, "mode", None)


def archive(conn, kind: str, name: str, data: bytes):
    """Keep an original download or file. Identical content is stored once."""
    conn.execute(
        "INSERT OR IGNORE INTO raw_archive (sha256, kind, name, received_at, data) VALUES (?, ?, ?, ?, ?)",
        (hashlib.sha256(data).hexdigest(), kind, name, int(time.time()), gzip.compress(data)))
