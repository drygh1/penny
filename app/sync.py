"""Pull accounts and transactions from SimpleFIN into the local database."""
import time
from datetime import date

from . import db, importer, simplefin
from .categorize import categorize, normalize_merchant

DAY = 86400
WINDOW_DAYS = 45       # SimpleFIN Bridge recommends at most 45 days per request
FIRST_SYNC_DAYS = 365  # how far back to look the first time
OVERLAP_DAYS = 14      # re-fetch recent days so late-posting transactions are caught


def upsert_account(conn, acct: dict):
    org = acct.get("org") or {}
    conn.execute(
        """INSERT INTO accounts (id, org_name, name, currency, balance, available_balance, balance_date)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             org_name = excluded.org_name, name = excluded.name, currency = excluded.currency,
             balance = excluded.balance, available_balance = excluded.available_balance,
             balance_date = excluded.balance_date""",
        (
            acct["id"],
            org.get("name") or org.get("domain"),
            acct.get("name") or "Account",
            acct.get("currency") or "USD",
            float(acct.get("balance") or 0),
            float(acct["available-balance"]) if acct.get("available-balance") else None,
            acct.get("balance-date"),
        ),
    )


def upsert_transaction(conn, account_id: str, tx: dict):
    amount = float(tx["amount"])
    description = (tx.get("description") or tx.get("memo") or "").strip() or "Unknown"
    payee = tx.get("payee")
    posted = int(tx.get("posted") or 0) or int(tx.get("transacted_at") or 0) or int(time.time())
    conn.execute(
        """INSERT INTO transactions (id, account_id, posted, amount, description, payee, merchant, category, pending,
                                     affects_balance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             posted = excluded.posted, amount = excluded.amount, description = excluded.description,
             payee = excluded.payee, pending = excluded.pending""",
        (
            f"{account_id}:{tx['id']}",
            account_id,
            posted,
            amount,
            description,
            payee,
            normalize_merchant(description, payee),
            db.resolve_category(conn, categorize(description, amount)),
            1 if tx.get("pending") else 0,
            0 if tx.get("affects_balance") is False else 1,
        ),
    )


def recategorize_all(conn) -> int:
    """Re-run automatic categories on every transaction.

    Auto categories depend only on the description and amount, so this is
    safe to repeat. It lets keyword improvements reach transactions synced
    earlier. Merchant rules are stored separately and still win.
    """
    names = set(db.category_names(conn))
    aliases = dict(conn.execute("SELECT old_name, new_name FROM category_aliases").fetchall())
    updates = []
    for r in conn.execute("SELECT id, description, amount, category FROM transactions"):
        new = aliases.get(categorize(r["description"], r["amount"]), categorize(r["description"], r["amount"]))
        new = new if new in names else "Other"
        if new != r["category"]:
            updates.append((new, r["id"]))
    with conn:
        conn.executemany("UPDATE transactions SET category = ? WHERE id = ?", updates)
    return len(updates)


def snapshot_balances(conn, day: str | None = None):
    day = day or date.today().isoformat()
    conn.execute(
        """INSERT INTO balance_snapshots (account_id, day, balance)
           SELECT id, ?, balance FROM accounts WHERE true  -- WHERE needed for SQLite to parse ON CONFLICT
           ON CONFLICT(account_id, day) DO UPDATE SET balance = excluded.balance""",
        (day,),
    )


def sync(conn) -> dict:
    access_url = db.get_setting(conn, "simplefin_access_url")
    if not access_url:
        raise simplefin.SimpleFINError("Not connected to SimpleFIN yet.")

    now = int(time.time())
    last = db.get_setting(conn, "last_sync")
    start = int(last) - OVERLAP_DAYS * DAY if last else now - FIRST_SYNC_DAYS * DAY

    errors: list[str] = []
    tx_count = 0
    seen_accounts: set[str] = set()
    with conn:
        # Pending transactions get new IDs once they post, so drop the old
        # pending rows and let this sync re-add whatever is still pending.
        conn.execute("DELETE FROM transactions WHERE pending = 1")
        window_start = start
        while window_start < now:
            window_end = min(window_start + WINDOW_DAYS * DAY, now)
            data = simplefin.fetch_accounts(access_url, window_start, window_end)
            errors += data.get("errors") or []
            errors += [e.get("msg", str(e)) if isinstance(e, dict) else str(e) for e in data.get("errlist") or []]
            for acct in data.get("accounts", []):
                upsert_account(conn, acct)
                seen_accounts.add(acct["id"])
                for tx in acct.get("transactions", []):
                    upsert_transaction(conn, acct["id"], tx)
                    tx_count += 1
            window_start = window_end
        # Imported CSV history may overlap what SimpleFIN just returned.
        for account_id in seen_accounts:
            importer.drop_overlap(conn, account_id)
        snapshot_balances(conn)
        db.set_setting(conn, "last_sync", now)
        db.set_setting(conn, "mode", "simplefin")

    return {"accounts": len(seen_accounts), "transactions": tx_count, "errors": sorted(set(errors))}
