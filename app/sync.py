"""Pull accounts and transactions from SimpleFIN into the local database."""
import json
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, time as dtime

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


PURCHASE_DATE_MAX_DAYS = 10
_DESCRIPTION_DATE = re.compile(r"date:?\s*(\d{1,2})/(\d{1,2})/(\d{2}(?:\d{2})?)\b", re.I)


def purchase_date(posted: int, description: str, transacted_at: int | None = None) -> int:
    """The day you made the purchase, which can be days before the bank posted it.

    SimpleFIN sends this as transacted_at. Bank CSVs only have the posted
    date, but some write the purchase date into the description
    ("...JUNEAU AKDate 10/24/26 PURCH..."). Only earlier dates within a few
    days count, so a stray date in a description can't move a transaction far.
    """
    candidates = [transacted_at] if transacted_at else []
    for m, d, y in _DESCRIPTION_DATE.findall(description):
        try:
            day = date(int(y) + 2000 if len(y) == 2 else int(y), int(m), int(d))
        except ValueError:
            continue
        candidates.append(int(datetime.combine(day, dtime(12, 0)).timestamp()))
    earlier = [c for c in candidates if 0 < posted - c <= PURCHASE_DATE_MAX_DAYS * DAY]
    return min(earlier, default=posted)


def fix_purchase_dates(conn) -> int:
    """Move already-saved transactions to the purchase date in their description."""
    updates = []
    for r in conn.execute("SELECT id, posted, description FROM transactions WHERE description LIKE '%date%'"):
        day = purchase_date(r["posted"], r["description"])
        if day != r["posted"]:
            updates.append((day, r["id"]))
    with conn:
        conn.executemany("UPDATE transactions SET posted = ? WHERE id = ?", updates)
    return len(updates)


def upsert_transaction(conn, account_id: str, tx: dict):
    amount = float(tx["amount"])
    description = " ".join((tx.get("description") or tx.get("memo") or "").split()) or "Unknown"
    payee = tx.get("payee")
    transacted = int(tx.get("transacted_at") or 0)
    posted = int(tx.get("posted") or 0) or transacted or int(time.time())
    posted = purchase_date(posted, description, transacted)
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


TRANSFER_WINDOW_DAYS = 4
# Only transactions Penny couldn't otherwise place are paired, so a $20 dinner
# never pairs with a $20 payment from a friend.
_PAIRABLE = {"Other", "Income", "Transfers", "Credit Card Payments"}


def match_transfers(conn) -> int:
    """Mark money moving between your own accounts as Transfers.

    A withdrawal from one account and a deposit of the same amount into a
    different account within a few days are the two sides of one transfer
    (savings to checking, or checking paying off a credit card), whatever
    words the bank uses. Both sides become Transfers, so the move counts as
    neither spending nor income. When either side reads as a card payment, both
    become Credit Card Payments instead.
    """
    rows = conn.execute(
        "SELECT id, account_id, posted, amount, category FROM transactions WHERE amount != 0 ORDER BY posted"
    ).fetchall()
    deposits: dict[int, list] = {}
    for r in rows:
        if r["amount"] > 0 and r["category"] in _PAIRABLE:
            deposits.setdefault(round(r["amount"] * 100), []).append(r)

    window = TRANSFER_WINDOW_DAYS * 86400
    paired = []
    for w in rows:
        if w["amount"] >= 0 or w["category"] not in _PAIRABLE:
            continue
        candidates = [d for d in deposits.get(round(-w["amount"] * 100), [])
                      if d["account_id"] != w["account_id"] and abs(d["posted"] - w["posted"]) <= window]
        if not candidates:
            continue
        d = min(candidates, key=lambda d: abs(d["posted"] - w["posted"]))
        deposits[round(d["amount"] * 100)].remove(d)
        card = "Credit Card Payments" in (w["category"], d["category"])
        paired += [(i, "Credit Card Payments" if card else "Transfers") for i in (w["id"], d["id"])]
    conn.executemany("UPDATE transactions SET category = ? WHERE id = ? AND category != ?",
                     [(cat, i, cat) for i, cat in paired])
    return len(paired) // 2


MARK_MATCH_DAYS = 7
TIP_RANGE = 0.3  # a pending restaurant charge can post up to 30% higher once the tip is added


@contextmanager
def keeping_marks(conn):
    """Keep your checkmarks (Review) and one-purchase categories when rows are replaced.

    Syncing drops pending rows and re-adds what's still pending, a pending
    charge posts under a new ID (sometimes with a tip added), and imported
    rows give way to the same transaction from SimpleFIN. Deleting a row
    deletes its marks, so they're noted first and put back: on the same ID
    if it returns, otherwise on the new row that replaced it.
    """
    saved = conn.execute("""
        SELECT t.id, t.account_id, t.posted, t.amount, t.merchant, r.reviewed, r.reviewed_at, o.category
        FROM transactions t
        LEFT JOIN reviewed r ON r.tx_id = t.id
        LEFT JOIN transaction_categories o ON o.tx_id = t.id
        WHERE r.tx_id IS NOT NULL OR o.tx_id IS NOT NULL""").fetchall()
    before = {r[0] for r in conn.execute("SELECT id FROM transactions")}
    yield
    now_rows = {r["id"]: r for r in conn.execute(
        "SELECT id, account_id, posted, amount, merchant FROM transactions")}
    claimed = set()

    marked = {s["id"] for s in saved}

    def replacement(s):
        # Rows new in this sync come first. An older row can take the marks only
        # on an exact amount and if it has none of its own: that's an imported row
        # giving way to the same transaction from SimpleFIN.
        def score(t):
            if t["account_id"] != s["account_id"] or abs(t["posted"] - s["posted"]) > MARK_MATCH_DAYS * DAY:
                return None
            new = t["id"] not in before
            if round(t["amount"], 2) == round(s["amount"], 2):
                return (0 if new else 2, abs(t["posted"] - s["posted"]))
            same_sign = (t["amount"] < 0) == (s["amount"] < 0)
            close = abs(t["amount"] - s["amount"]) <= abs(s["amount"]) * TIP_RANGE
            if new and t["merchant"] == s["merchant"] and same_sign and close:
                return (1, abs(t["amount"] - s["amount"]), abs(t["posted"] - s["posted"]))
            return None
        options = [(sc, t["id"]) for t in now_rows.values()
                   if t["id"] not in claimed and t["id"] not in marked and (sc := score(t)) is not None]
        return min(options)[1] if options else None

    for s in saved:
        target = s["id"] if s["id"] in now_rows else replacement(s)
        if target is None:
            continue
        claimed.add(target)
        if s["reviewed"] is not None:
            conn.execute("INSERT OR IGNORE INTO reviewed (tx_id, reviewed, reviewed_at) VALUES (?, ?, ?)",
                         (target, s["reviewed"], s["reviewed_at"]))
        if s["category"] is not None:
            conn.execute("INSERT OR IGNORE INTO transaction_categories (tx_id, category) VALUES (?, ?)",
                         (target, s["category"]))


def refresh_categories(conn):
    """Fix purchase dates, drop overlapping imports, re-run keyword categories,
    then pair transfers between accounts."""
    fix_purchase_dates(conn)
    with conn, keeping_marks(conn):
        for (account_id,) in conn.execute("SELECT id FROM accounts").fetchall():
            importer.drop_overlap(conn, account_id)
    recategorize_all(conn)
    with conn:
        match_transfers(conn)


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

    # Download everything first, saving each response as soon as it arrives,
    # so what SimpleFIN sent is kept even if processing it fails.
    responses = []
    window_start = start
    while window_start < now:
        window_end = min(window_start + WINDOW_DAYS * DAY, now)
        data = simplefin.fetch_accounts(access_url, window_start, window_end)
        with conn:
            db.archive(conn, "simplefin", f"{date.fromtimestamp(window_start)} to {date.fromtimestamp(window_end)}",
                       json.dumps(data, sort_keys=True).encode())
        responses.append(data)
        window_start = window_end

    errors: list[str] = []
    tx_count = 0
    seen_accounts: set[str] = set()
    with conn:
        with keeping_marks(conn):
            # Pending transactions get new IDs once they post, so drop the old
            # pending rows and let this sync re-add whatever is still pending.
            conn.execute("DELETE FROM transactions WHERE pending = 1")
            for data in responses:
                errors += data.get("errors") or []
                errors += [e.get("msg", str(e)) if isinstance(e, dict) else str(e) for e in data.get("errlist") or []]
                for acct in data.get("accounts", []):
                    upsert_account(conn, acct)
                    seen_accounts.add(acct["id"])
                    for tx in acct.get("transactions", []):
                        upsert_transaction(conn, acct["id"], tx)
                        tx_count += 1
            # Imported CSV history may overlap what SimpleFIN just returned.
            for account_id in seen_accounts:
                importer.drop_overlap(conn, account_id)
        match_transfers(conn)
        snapshot_balances(conn)
        db.set_setting(conn, "last_sync", now)
        db.set_setting(conn, "mode", "simplefin")

    return {"accounts": len(seen_accounts), "transactions": tx_count, "errors": sorted(set(errors))}
