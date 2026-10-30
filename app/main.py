"""Penny API + static web UI.

Every endpoint under /api returns JSON, so a future iPhone app can talk to
this same server.
"""
import base64
import hashlib
import binascii
import csv
import io
import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backup, db, demo, importer, pdf_statements, simplefin, subscriptions
from .categorize import (COLOR_SLOTS, LOCKED_CATEGORIES, NON_SPENDING, categorize, description_key, display_name,
                         institution_words, normalize_merchant)
from .sync import (match_transfers, purchase_date, refresh_categories, snapshot_balances, sync, upsert_account,
                   upsert_transaction)

# Inside the Mac app (PyInstaller), bundled files sit under sys._MEIPASS.
STATIC = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "static"
DESKTOP = os.environ.get("PENNY_DESKTOP") == "1"  # running as the Mac app (app/desktop.py)
@asynccontextmanager
async def lifespan(app):
    # Apply any keyword improvements to transactions synced before them.
    conn = db.connect()
    try:
        refresh_categories(conn)
        backup_if_real(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="Penny", lifespan=lifespan)

LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def only_local_pages(request: Request, call_next):
    """Block other websites from using Penny's API through your browser.

    The Host check stops DNS-rebinding tricks. The Origin check stops a page on
    another site from sending changes like "delete all data" to this server.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in LOCAL_HOSTS:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD") and origin and urlsplit(origin).hostname not in LOCAL_HOSTS:
        return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.update(NO_CACHE)
    return response

# `merchant` is the combined merchant (see merchant_aliases); rules, logos and
# everything shown use it. `own_merchant` is what the bank's wording gave.
TX_SELECT = """
    SELECT t.id, t.account_id, t.posted, t.amount, t.description, t.merchant, t.own_merchant, t.pending,
           COALESCE(o.category, d.category, r.category, t.category) AS category,
           COALESCE(d.category, r.category, t.category) AS default_category,
           o.category IS NOT NULL AS edited,
           d.pattern AS description_rule,
           strftime('%Y-%m', t.posted, 'unixepoch', 'localtime') AS month,
           COALESCE(a.nickname, a.name) AS account_name
    FROM (SELECT t.id, t.account_id, t.posted, t.amount, t.description, t.payee, t.pending, t.category,
                 t.affects_balance, t.merchant AS own_merchant, COALESCE(ma.merchant, t.merchant) AS merchant
          FROM transactions t LEFT JOIN merchant_aliases ma ON ma.alias = t.merchant) t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN transaction_categories o ON o.tx_id = t.id
    LEFT JOIN merchant_rules r ON r.merchant = t.merchant
    LEFT JOIN description_rules d ON d.merchant = t.merchant AND d.pattern = (
        SELECT d2.pattern FROM description_rules d2
        WHERE d2.merchant = t.merchant AND instr(desc_key(t.description), d2.pattern) > 0
        ORDER BY length(d2.pattern) DESC LIMIT 1)
"""
SPENDING_FILTER = "category NOT IN ({})".format(",".join(f"'{c}'" for c in NON_SPENDING))
# Income sources colored on the dashboard, using the first color slots (checked
# for colorblind separation in that order, light and dark).
INCOME_COLORS = 6


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


# ---------- connection & sync ----------

class ConnectBody(BaseModel):
    setup_token: str


@app.get("/api/status")
def status(conn=Depends(get_conn)):
    last = db.get_setting(conn, "last_sync")
    return {
        "mode": db.get_setting(conn, "mode"),  # None, "demo", or "simplefin"
        "connected": db.get_setting(conn, "simplefin_access_url") is not None,
        "last_sync": datetime.fromtimestamp(int(last)).isoformat() if last else None,
        "desktop": DESKTOP,  # the Mac app stops when you quit it, so it has no Stop button
    }


@app.post("/api/simplefin/connect")
def connect_simplefin(body: ConnectBody, conn=Depends(get_conn)):
    try:
        access_url = simplefin.claim_setup_token(body.setup_token)
    except Exception as e:
        fail(e)
    with conn:
        if db.get_setting(conn, "mode") == "demo":
            db.clear_financial_data(conn)
        db.set_setting(conn, "simplefin_access_url", access_url)
    return run_sync(conn)


@app.post("/api/sync")
def run_sync(conn=Depends(get_conn)):
    try:
        result = sync(conn)
    except Exception as e:
        fail(e)
    backup_if_real(conn)
    return result


def has_real_data(conn) -> bool:
    return db.get_setting(conn, "mode") in ("simplefin", "manual")


def backup_if_real(conn, reason: str | None = None):
    """Daily backup (or a safety backup before something destructive). Skipped for demo data."""
    if not has_real_data(conn):
        return
    try:
        backup.create(conn, reason) if reason else backup.daily_if_needed(conn)
    except OSError as e:
        print(f"Backup failed: {e}")
        if reason:  # never do something destructive without its safety backup
            fail(Exception(f"Couldn't make a safety backup first ({e}), so nothing was changed."))


@app.post("/api/demo")
def load_demo(conn=Depends(get_conn)):
    if db.get_setting(conn, "simplefin_access_url"):
        fail(Exception("Disconnect SimpleFIN before loading demo data."))
    if has_real_data(conn):
        fail(Exception("Loading demo data would replace your real transactions. Delete your data first."))
    demo.load(conn)
    return {"ok": True}


@app.post("/api/disconnect")
def disconnect(conn=Depends(get_conn)):
    """Forget the SimpleFIN connection. Everything already downloaded is kept."""
    with conn:
        db.set_setting(conn, "simplefin_access_url", None)
    return {"ok": True}


@app.post("/api/delete-all-data")
def delete_all_data(conn=Depends(get_conn)):
    """Delete accounts, transactions, originals and the SimpleFIN connection.

    A safety backup is taken first. Categories, rules and pictures are kept.
    """
    backup_if_real(conn, "before-delete")
    with conn:
        db.clear_financial_data(conn)
        conn.execute("DELETE FROM raw_archive")
        db.set_setting(conn, "simplefin_access_url", None)
    return {"ok": True, "backup_dir": str(backup.BACKUP_DIR)}


# ---------- your data & backups ----------

@app.get("/api/data")
def data_summary(conn=Depends(get_conn)):
    tx = conn.execute("""SELECT COUNT(*) AS n, date(MIN(posted), 'unixepoch', 'localtime') AS first,
                                date(MAX(posted), 'unixepoch', 'localtime') AS last FROM transactions""").fetchone()
    originals = dict(conn.execute("SELECT kind, COUNT(*) FROM raw_archive GROUP BY kind").fetchall())
    backups = backup.list_backups()
    return {
        "transactions": tx["n"], "first_date": tx["first"], "last_date": tx["last"],
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "originals": originals,
        "backup_dir": str(backup.BACKUP_DIR).replace(str(Path.home()), "~"),
        "backups": backups[:15],
        "backup_count": len(backups),
        "real_data": has_real_data(conn),
    }


@app.post("/api/backups")
def make_backup(conn=Depends(get_conn)):
    path = backup.create(conn, "manual")
    return {"ok": True, "name": path.name}


class RestoreBody(BaseModel):
    name: str


@app.post("/api/backups/restore")
def restore_backup(body: RestoreBody, conn=Depends(get_conn)):
    try:
        backup.restore(conn, body.name)
    except ValueError as e:
        fail(e)
    return {"ok": True}


@app.post("/api/backups/reveal")
def reveal_backups():
    """Open the backups folder in Finder."""
    backup.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(backup.BACKUP_DIR)], check=False)
    return {"ok": True}


# ---------- accounts & net worth ----------

@app.get("/api/accounts")
def accounts(conn=Depends(get_conn)):
    rows = [dict(r) for r in conn.execute(
        "SELECT id, org_name, COALESCE(nickname, name) AS name, name AS bank_name, nickname, "
        "currency, balance, available_balance, balance_date "
        # Biggest balance first, debts after, empty accounts last.
        "FROM accounts WHERE hidden = 0 ORDER BY balance = 0, balance DESC, COALESCE(nickname, name)")]
    assets = sum(r["balance"] for r in rows if r["balance"] > 0)
    debts = sum(r["balance"] for r in rows if r["balance"] < 0)

    # Net worth over time: for each snapshot day, use each account's latest known balance.
    snaps = [dict(r) for r in conn.execute(
        "SELECT s.account_id, s.day, s.balance FROM balance_snapshots s "
        "JOIN accounts a ON a.id = s.account_id WHERE a.hidden = 0 ORDER BY s.day")]
    snaps = sorted(snaps + earlier_balances(conn, snaps), key=lambda s: s["day"])
    latest: dict[str, float] = {}
    by_day: dict[str, float] = {}
    for s in snaps:
        latest[s["account_id"]] = s["balance"]
        by_day[s["day"]] = round(sum(latest.values()), 2)

    return {
        "accounts": rows,
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net_worth": round(assets + debts, 2),
        "history": [{"day": d, "net_worth": v} for d, v in by_day.items()],
    }


def _walk_back(account_id, day0: date, balance: float, txs, until: date, anchor: date, floor=None) -> list[dict]:
    """Weekly balances before day0, undoing txs ((day, amount), newest first) back to `until`.

    Every account's weeks land on the same days (anchor, anchor - 7, ...). If one
    account's week ended on a Wednesday and another's on a Thursday, money moved
    between them on the Thursday would count in both for a day.
    """
    out, i = [], 0
    steps = (day0 - anchor).days - 1
    d = anchor + timedelta(days=7 * (steps // 7)) if steps >= 0 else anchor - timedelta(days=7)
    while d >= until - timedelta(days=7):
        # Undo every transaction that happened after day d.
        while i < len(txs) and txs[i][0] > d:
            balance -= txs[i][1]
            i += 1
        shown = balance if floor is None else max(floor, balance)
        out.append({"account_id": account_id, "day": d.isoformat(), "balance": round(shown, 2)})
        d -= timedelta(days=7)
    return out


class AccountNameBody(BaseModel):
    id: str
    name: str  # blank goes back to the bank's name


@app.put("/api/accounts/name")
def rename_account(body: AccountNameBody, conn=Depends(get_conn)):
    """Give an account your own name. Syncs keep it; they only update the bank's name."""
    name = " ".join(body.name.split())
    if len(name) > 60:
        fail(Exception("Keep account names to 60 characters or fewer."))
    with conn:
        cur = conn.execute("UPDATE accounts SET nickname = ? WHERE id = ?", (name or None, body.id))
    if not cur.rowcount:
        fail(Exception("That account no longer exists."))
    return {"ok": True}


def earlier_balances(conn, snaps) -> list[dict]:
    """Weekly balances from before each account's first recorded balance.

    Balances are only recorded when Penny syncs, but transactions can go back
    much further (imported CSV history). Walking those transactions backward
    from the first recorded balance rebuilds what the balance must have been.

    Brokerages and crypto wallets often report a balance but no transactions.
    Those are walked back with the deposits and withdrawals seen from your
    other accounts ("FID BKG SVC" in savings is money into Fidelity), with all
    of one institution's accounts as one pool, so moving money in doesn't look
    like it disappeared. Market gains and losses can't be seen, so this part
    is an estimate; it never goes below zero.
    """
    first: dict[str, dict] = {}
    for s in snaps:
        first.setdefault(s["account_id"], s)
    if not first:
        return []
    history = {account_id: [(date.fromtimestamp(r["posted"]), r["amount"], r["description"]) for r in conn.execute(
        "SELECT posted, amount, description FROM transactions WHERE account_id = ? AND affects_balance = 1 "
        "ORDER BY posted DESC", (account_id,))] for account_id in first}
    starts = [txs[-1][0] for txs in history.values() if txs]
    if not starts:
        return []
    earliest = min(starts)
    anchor = min(date.fromisoformat(s["day"]) for s in first.values())

    out, pools = [], {}
    for account_id, s in first.items():
        day0 = date.fromisoformat(s["day"])
        if history[account_id]:
            txs = [(d, amt) for d, amt, _ in history[account_id] if d <= day0]
            if txs and txs[-1][0] < day0:
                out += _walk_back(account_id, day0, s["balance"], txs, txs[-1][0], anchor)
            continue
        org = conn.execute("SELECT org_name FROM accounts WHERE id = ?", (account_id,)).fetchone()["org_name"]
        pools.setdefault(org, []).append(s)

    for org, members in pools.items():
        words = institution_words(org)
        # Money leaving your other accounts toward this institution came into it.
        flows = sorted(((d, -amt) for txs in history.values() for d, amt, desc in txs
                        if words and any(w in desc.lower() for w in words)), reverse=True)
        day0 = min(date.fromisoformat(s["day"]) for s in members)
        flows = [f for f in flows if f[0] <= day0]
        # The pool's history rides on its biggest account; at the first sync
        # every member's real balance takes over.
        lead = max(members, key=lambda s: s["balance"])["account_id"]
        total = sum(s["balance"] for s in members)
        out += _walk_back(lead, day0, total, flows, earliest, anchor, floor=0)
    return out


# ---------- spending ----------

@app.get("/api/categories")
def categories(conn=Depends(get_conn)):
    counts = dict(conn.execute(
        f"SELECT category, COUNT(*) FROM ({TX_SELECT}) GROUP BY category").fetchall())
    return [{
        "name": r["name"],
        "color_slot": r["color_slot"],
        "locked": r["name"] in LOCKED_CATEGORIES,
        "transactions": counts.get(r["name"], 0),
    } for r in conn.execute("SELECT name, color_slot FROM categories ORDER BY position")]


class CategoryBody(BaseModel):
    name: str


@app.post("/api/categories")
def add_category(body: CategoryBody, conn=Depends(get_conn)):
    name = " ".join(body.name.split())
    if not 1 <= len(name) <= 40:
        fail(Exception("Category names must be 1 to 40 characters."))
    if any(n.lower() == name.lower() for n in db.category_names(conn)):
        fail(Exception(f"There's already a category called {name}."))
    used = {r[0] for r in conn.execute("SELECT color_slot FROM categories WHERE color_slot IS NOT NULL")}
    free_slot = next((i for i in range(1, COLOR_SLOTS + 1) if i not in used), None)
    with conn:
        conn.execute(
            "INSERT INTO categories (name, position, color_slot) "
            "VALUES (?, (SELECT COALESCE(MAX(position), 0) + 1 FROM categories), ?)",
            (name, free_slot))
        # If this name was deleted before, stop redirecting it.
        conn.execute("DELETE FROM category_aliases WHERE old_name = ?", (name,))
    return {"ok": True, "name": name}


@app.delete("/api/categories")
def delete_category(name: str, move_to: str = "Other", conn=Depends(get_conn)):
    """Delete a category, moving its transactions and merchant rules to `move_to`."""
    backup_if_real(conn, "before-category-delete")
    names = db.category_names(conn)
    if name not in names:
        fail(Exception(f"Unknown category: {name}"))
    if name in LOCKED_CATEGORIES:
        fail(Exception(f"{name} is built in and can't be deleted."))
    if move_to not in names or move_to == name:
        fail(Exception("Choose a different category to move its transactions to."))
    with conn:
        conn.execute("UPDATE transactions SET category = ? WHERE category = ?", (move_to, name))
        conn.execute("UPDATE merchant_rules SET category = ? WHERE category = ?", (move_to, name))
        conn.execute("UPDATE transaction_categories SET category = ? WHERE category = ?", (move_to, name))
        conn.execute("UPDATE description_rules SET category = ? WHERE category = ?", (move_to, name))
        conn.execute("UPDATE category_aliases SET new_name = ? WHERE new_name = ?", (move_to, name))
        conn.execute(
            "INSERT INTO category_aliases (old_name, new_name) VALUES (?, ?) "
            "ON CONFLICT(old_name) DO UPDATE SET new_name = excluded.new_name", (name, move_to))
        conn.execute("DELETE FROM categories WHERE name = ?", (name,))
    return {"ok": True}


def period_filter(period: str) -> tuple[str, list]:
    """SQL for a period: a year ("2026") or a month ("2026-10")."""
    if len(period) == 4 and period.isdigit():
        return "month LIKE ?", [f"{period}-%"]
    if len(period) == 7 and period[:4].isdigit() and period[4] == "-" and period[5:].isdigit():
        return "month = ?", [period]
    fail(Exception(f"Unknown period: {period}"))


@app.get("/api/periods")
def periods(conn=Depends(get_conn)):
    """Years and months that have transactions, newest first."""
    months = [r[0] for r in conn.execute(
        f"SELECT DISTINCT month FROM ({TX_SELECT}) ORDER BY month DESC")]
    return {"years": sorted({m[:4] for m in months}, reverse=True), "months": months}


@app.get("/api/spending/monthly")
def spending_monthly(months: int = 12, year: str | None = None, conn=Depends(get_conn)):
    """Income and spending per month: the last `months` months, or every month of `year`."""
    totals = f"""
        SELECT month,
               ROUND(-SUM(CASE WHEN {SPENDING_FILTER} THEN amount ELSE 0 END), 2) AS spending,
               ROUND(SUM(CASE WHEN category = 'Income' THEN amount ELSE 0 END), 2) AS income
        FROM ({TX_SELECT})"""
    if year:
        where, params = period_filter(year)
        found = {r["month"]: dict(r) for r in conn.execute(f"{totals} WHERE {where} GROUP BY month", params)}
        months_of_year = [f"{year}-{m:02d}" for m in range(1, 13)]
        return [found.get(m, {"month": m, "spending": 0, "income": 0}) for m in months_of_year]
    rows = conn.execute(f"{totals} GROUP BY month ORDER BY month DESC LIMIT ?", (months,)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/spending/yearly")
def spending_yearly(conn=Depends(get_conn)):
    """Income and spending for every year that has transactions, oldest first."""
    return [dict(r) for r in conn.execute(f"""
        SELECT substr(month, 1, 4) AS year,
               ROUND(-SUM(CASE WHEN {SPENDING_FILTER} THEN amount ELSE 0 END), 2) AS spending,
               ROUND(SUM(CASE WHEN category = 'Income' THEN amount ELSE 0 END), 2) AS income
        FROM ({TX_SELECT}) GROUP BY year ORDER BY year""")]


@app.get("/api/spending/breakdown")
def spending_breakdown(period: str | None = None, month: str | None = None, conn=Depends(get_conn)):
    """Where money went and where it came from, for a year or a month."""
    period = period or month or date.today().strftime("%Y-%m")
    where, params = period_filter(period)
    source = f"FROM ({TX_SELECT}) WHERE {where}"
    by_cat = conn.execute(f"""
        SELECT category, ROUND(-SUM(amount), 2) AS total, COUNT(*) AS count
        {source} AND {SPENDING_FILTER}
        GROUP BY category HAVING total > 0 ORDER BY total DESC""", params).fetchall()
    by_merchant = conn.execute(f"""
        SELECT merchant, category, ROUND(-SUM(amount), 2) AS total, COUNT(*) AS count
        {source} AND {SPENDING_FILTER}
        GROUP BY merchant HAVING total > 0 ORDER BY total DESC LIMIT 10""", params).fetchall()
    by_source = conn.execute(f"""
        SELECT merchant, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
        {source} AND category = 'Income'
        GROUP BY merchant HAVING total > 0 ORDER BY total DESC LIMIT 10""", params).fetchall()
    spending = round(sum(r["total"] for r in by_cat), 2)
    income = round(conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) {source} AND category = 'Income'", params).fetchone()[0], 2)
    # Your biggest income sources of all time get a color each, so a source keeps
    # its color whichever month you look at; the rest share "Everything else".
    income_slots = {r["merchant"]: i + 1 for i, r in enumerate(conn.execute(f"""
        SELECT merchant FROM ({TX_SELECT}) WHERE category = 'Income'
        GROUP BY merchant HAVING SUM(amount) > 0 ORDER BY SUM(amount) DESC LIMIT {INCOME_COLORS}"""))}
    return {
        "period": period,
        "month": period,  # older name, kept for compatibility
        "total": spending,
        "income": income,
        "net": round(income - spending, 2),
        "categories": [dict(r) for r in by_cat],
        "merchants": [dict(r) | {"name": display_name(r["merchant"])} for r in by_merchant],
        "income_sources": [dict(r) | {"name": display_name(r["merchant"]), "color_slot": income_slots.get(r["merchant"])}
                           for r in by_source],
    }


# ---------- transactions ----------

@app.get("/api/transactions")
def transactions(month: str | None = None, category: str | None = None,
                 merchant: str | None = None, q: str | None = None,
                 limit: int = 300, conn=Depends(get_conn)):
    where, params = [], []
    if month:  # a month ("2026-10") or a whole year ("2026")
        clause, values = period_filter(month)
        where.append(clause); params += values
    if category:
        where.append("category = ?"); params.append(category)
    if merchant:
        where.append("merchant = ?"); params.append(merchant)
    if q:
        where.append("(description LIKE ? OR merchant LIKE ?)"); params += [f"%{q}%"] * 2
    sql = f"SELECT * FROM ({TX_SELECT})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY posted DESC LIMIT ?"
    rows = conn.execute(sql, (*params, limit)).fetchall()
    return [dict(r) | {"date": date.fromtimestamp(r["posted"]).isoformat(),
                       "name": display_name(r["merchant"])} for r in rows]


class RuleBody(BaseModel):
    merchant: str
    category: str


@app.put("/api/merchant-rules")
def set_rule(body: RuleBody, conn=Depends(get_conn)):
    if body.category not in db.category_names(conn):
        fail(Exception(f"Unknown category: {body.category}"))
    with conn:
        conn.execute(
            "INSERT INTO merchant_rules (merchant, category) VALUES (?, ?) "
            "ON CONFLICT(merchant) DO UPDATE SET category = excluded.category",
            (body.merchant, body.category))
    return {"ok": True}


@app.delete("/api/merchant-rules")
def delete_rule(merchant: str, conn=Depends(get_conn)):
    """Remove a rule so the merchant goes back to its automatic category."""
    with conn:
        conn.execute("DELETE FROM merchant_rules WHERE merchant = ?", (merchant,))
    return {"ok": True}


class TransactionCategoryBody(BaseModel):
    id: str
    category: str


@app.put("/api/transaction-category")
def set_transaction_category(body: TransactionCategoryBody, conn=Depends(get_conn)):
    """Put one transaction in a category, leaving the rest of its merchant alone."""
    if body.category not in db.category_names(conn):
        fail(Exception(f"Unknown category: {body.category}"))
    if not conn.execute("SELECT 1 FROM transactions WHERE id = ?", (body.id,)).fetchone():
        fail(Exception("That transaction no longer exists."))
    with conn:
        conn.execute(
            "INSERT INTO transaction_categories (tx_id, category) VALUES (?, ?) "
            "ON CONFLICT(tx_id) DO UPDATE SET category = excluded.category",
            (body.id, body.category))
    return {"ok": True}


@app.delete("/api/transaction-category")
def reset_transaction_category(id: str, conn=Depends(get_conn)):
    """Send one transaction back to its merchant's default category."""
    with conn:
        conn.execute("DELETE FROM transaction_categories WHERE tx_id = ?", (id,))
    return {"ok": True}


class DescriptionRuleBody(BaseModel):
    merchant: str
    pattern: str
    category: str


@app.put("/api/description-rules")
def set_description_rule(body: DescriptionRuleBody, conn=Depends(get_conn)):
    """Categorize a merchant's transactions whose description contains `pattern`."""
    pattern = description_key(body.pattern)
    if not pattern:
        fail(Exception("Type some words from the description (numbers are ignored)."))
    if body.category not in db.category_names(conn):
        fail(Exception(f"Unknown category: {body.category}"))
    with conn:
        conn.execute(
            "INSERT INTO description_rules (merchant, pattern, category) VALUES (?, ?, ?) "
            "ON CONFLICT(merchant, pattern) DO UPDATE SET category = excluded.category",
            (body.merchant, pattern, body.category))
    return {"ok": True, "pattern": pattern}


@app.delete("/api/description-rules")
def delete_description_rule(merchant: str, pattern: str, conn=Depends(get_conn)):
    with conn:
        conn.execute("DELETE FROM description_rules WHERE merchant = ? AND pattern = ?", (merchant, pattern))
    return {"ok": True}


@app.get("/api/merchant-descriptions")
def merchant_descriptions(merchant: str, conn=Depends(get_conn)):
    """A merchant's different descriptions (ignoring digits), and its description rules."""
    groups: dict[str, dict] = {}
    for t in conn.execute(f"SELECT * FROM ({TX_SELECT}) WHERE merchant = ? ORDER BY posted DESC", (merchant,)):
        key = description_key(t["description"])
        g = groups.setdefault(key, {
            "key": key, "example": t["description"], "count": 0, "total": 0.0,
            "last_date": date.fromtimestamp(t["posted"]).isoformat(),
            "category": t["default_category"], "rule": t["description_rule"],
        })
        g["count"] += 1
        g["total"] = round(g["total"] + t["amount"], 2)
    rules = [dict(r) for r in conn.execute(
        "SELECT pattern, category FROM description_rules WHERE merchant = ? ORDER BY pattern", (merchant,))]
    return {"descriptions": sorted(groups.values(), key=lambda g: -g["count"]), "rules": rules}


# ---------- merchants ----------

@app.get("/api/merchants")
def merchants(conn=Depends(get_conn)):
    rows = conn.execute(f"""
        SELECT t.merchant, COUNT(*) AS count,
               ROUND(-SUM(CASE WHEN t.amount < 0 AND t.{SPENDING_FILTER} THEN t.amount ELSE 0 END), 2) AS spent,
               MAX(t.posted) AS last_posted, MAX(t.description) AS example,
               COUNT(DISTINCT desc_key(t.description)) AS variants,
               (SELECT COUNT(*) FROM description_rules d WHERE d.merchant = t.merchant) AS description_rules,
               r.category AS rule_category, l.version AS logo_version
        FROM ({TX_SELECT}) t
        LEFT JOIN merchant_rules r ON r.merchant = t.merchant
        LEFT JOIN merchant_logos l ON l.merchant = t.merchant
        GROUP BY t.merchant ORDER BY count DESC, spent DESC""").fetchall()
    # The automatic category is whichever one most of the merchant's transactions got.
    auto: dict[str, tuple[str, int]] = {}
    for r in conn.execute(
            "SELECT COALESCE(ma.merchant, t.merchant) AS merchant, t.category, COUNT(*) AS n "
            "FROM transactions t LEFT JOIN merchant_aliases ma ON ma.alias = t.merchant GROUP BY 1, 2"):
        if r["merchant"] not in auto or r["n"] > auto[r["merchant"]][1]:
            auto[r["merchant"]] = (r["category"], r["n"])
    combined: dict[str, list] = {}
    for r in conn.execute("SELECT alias, merchant FROM merchant_aliases ORDER BY alias"):
        combined.setdefault(r["merchant"], []).append({"merchant": r["alias"], "name": display_name(r["alias"])})
    return [dict(r) | {
        "name": display_name(r["merchant"]),
        "auto_category": auto[r["merchant"]][0],
        "last_date": date.fromtimestamp(r["last_posted"]).isoformat(),
        "combined": combined.get(r["merchant"], []),
    } for r in rows]


class CombineBody(BaseModel):
    merchant: str  # the one that stays
    others: list[str]


@app.post("/api/merchants/combine")
def combine_merchants(body: CombineBody, conn=Depends(get_conn)):
    """Count other merchants' transactions as `merchant` from now on.

    Rules and pictures the others had move over when `merchant` has none of
    its own. Merchants already combined into one of the others come along.
    """
    keep = conn.execute("SELECT merchant FROM merchant_aliases WHERE alias = ?", (body.merchant,)).fetchone()
    target = keep["merchant"] if keep else body.merchant
    others = {o for o in body.others if o != target}
    if not others:
        fail(Exception("Choose at least one other merchant to combine."))
    with conn:
        for other in others:
            conn.execute("INSERT INTO merchant_aliases (alias, merchant) VALUES (?, ?) "
                         "ON CONFLICT(alias) DO UPDATE SET merchant = excluded.merchant", (other, target))
            conn.execute("UPDATE merchant_aliases SET merchant = ? WHERE merchant = ?", (target, other))
            conn.execute("INSERT OR IGNORE INTO merchant_rules (merchant, category) "
                         "SELECT ?, category FROM merchant_rules WHERE merchant = ?", (target, other))
            conn.execute("INSERT OR IGNORE INTO description_rules (merchant, pattern, category) "
                         "SELECT ?, pattern, category FROM description_rules WHERE merchant = ?", (target, other))
            conn.execute("INSERT OR IGNORE INTO merchant_logos (merchant, mime, image, version) "
                         "SELECT ?, mime, image, version FROM merchant_logos WHERE merchant = ?", (target, other))
            for table in ("merchant_rules", "description_rules", "merchant_logos"):
                conn.execute(f"DELETE FROM {table} WHERE merchant = ?", (other,))
    return {"ok": True, "merchant": target}


@app.delete("/api/merchants/combine")
def split_merchant(alias: str, conn=Depends(get_conn)):
    """Undo combining: `alias` is its own merchant again (with no rules of its own)."""
    with conn:
        conn.execute("DELETE FROM merchant_aliases WHERE alias = ?", (alias,))
    return {"ok": True}


@app.get("/api/merchant-logos")
def merchant_logos(conn=Depends(get_conn)):
    """Map of merchant -> logo version, so the UI knows which merchants have a picture."""
    return {r["merchant"]: r["version"] for r in conn.execute("SELECT merchant, version FROM merchant_logos")}


@app.get("/api/merchant-logo")
def get_merchant_logo(merchant: str, conn=Depends(get_conn)):
    row = conn.execute("SELECT mime, image FROM merchant_logos WHERE merchant = ?", (merchant,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No picture for this merchant.")
    # The URL includes ?v=<version>, so the browser can cache it indefinitely.
    return Response(row["image"], media_type=row["mime"], headers={
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })


LOGO_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_LOGO_BYTES = 512 * 1024


class LogoBody(BaseModel):
    merchant: str
    image: str  # data URL, e.g. "data:image/png;base64,..."


@app.put("/api/merchant-logo")
def set_merchant_logo(body: LogoBody, conn=Depends(get_conn)):
    header, _, payload = body.image.partition(",")
    mime = header.removeprefix("data:").removesuffix(";base64")
    if mime not in LOGO_TYPES or not header.endswith(";base64"):
        fail(Exception("Pictures must be PNG, JPEG or WebP."))
    try:
        image = base64.b64decode(payload, validate=True)
    except binascii.Error:
        fail(Exception("That picture couldn't be read."))
    if len(image) > MAX_LOGO_BYTES:
        fail(Exception("That picture is too large."))
    with conn:
        conn.execute(
            "INSERT INTO merchant_logos (merchant, mime, image, version) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(merchant) DO UPDATE SET mime = excluded.mime, image = excluded.image, version = excluded.version",
            (body.merchant, mime, image, int(time.time() * 1000)))
    return {"ok": True}


@app.delete("/api/merchant-logo")
def delete_merchant_logo(merchant: str, conn=Depends(get_conn)):
    with conn:
        conn.execute("DELETE FROM merchant_logos WHERE merchant = ?", (merchant,))
    return {"ok": True}


# ---------- subscriptions ----------

@app.get("/api/subscriptions")
def get_subscriptions(conn=Depends(get_conn)):
    txs = conn.execute(f"SELECT merchant, posted, amount, category, description_rule FROM ({TX_SELECT})").fetchall()
    overrides = {r["merchant"]: r["status"] for r in conn.execute("SELECT * FROM subscription_overrides")}
    kinds = {r["key"]: r["kind"] for r in conn.execute("SELECT * FROM recurring_kinds")}
    found = subscriptions.detect(
        [dict(t) for t in txs], date.today(),
        names={t["merchant"]: display_name(t["merchant"]) for t in txs},
        overrides=overrides, kinds=kinds)
    active = [s for s in found if s.active and not s.ignored]
    total = lambda items, field: round(sum(getattr(s, field) for s in items), 2)
    subs = [s for s in active if s.kind == "subscription"]
    bills = [s for s in active if s.kind == "bill"]
    return {
        "monthly_total": total(active, "monthly_cost"),
        "yearly_total": total(active, "yearly_cost"),
        "subscriptions_monthly": total(subs, "monthly_cost"),
        "bills_monthly": total(bills, "monthly_cost"),
        "items": [s.to_dict() for s in found],
    }


class KindBody(BaseModel):
    key: str
    kind: Literal["bill", "subscription"]


@app.put("/api/subscriptions/kind")
def set_subscription_kind(body: KindBody, conn=Depends(get_conn)):
    with conn:
        conn.execute(
            "INSERT INTO recurring_kinds (key, kind) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET kind = excluded.kind",
            (body.key, body.kind))
    return {"ok": True}


class OverrideBody(BaseModel):
    merchant: str       # the subscription's key
    status: str | None  # "ignored" or null to restore


@app.post("/api/subscriptions/override")
def subscription_override(body: OverrideBody, conn=Depends(get_conn)):
    with conn:
        if body.status is None:
            conn.execute("DELETE FROM subscription_overrides WHERE merchant = ?", (body.merchant,))
        else:
            conn.execute(
                "INSERT INTO subscription_overrides (merchant, status) VALUES (?, ?) "
                "ON CONFLICT(merchant) DO UPDATE SET status = excluded.status",
                (body.merchant, body.status))
    return {"ok": True}


# ---------- CSV import ----------

MAX_CSV_CHARS = 20_000_000


class ImportPreviewBody(BaseModel):
    csv: str
    mapping: dict | None = None
    file_name: str | None = None


def _parse_csv(body) -> tuple[list[str], list[list[str]], dict]:
    if len(body.csv) > MAX_CSV_CHARS:
        fail(Exception("That file is too large."))
    try:
        headers, rows = importer.parse_table(body.csv)
    except ValueError as e:
        fail(e)
    if not rows:
        fail(Exception("That file has no transactions in it."))
    return headers, rows, body.mapping or importer.guess_mapping(headers, rows)


@app.post("/api/import/preview")
def import_preview(body: ImportPreviewBody, conn=Depends(get_conn)):
    """Guess (or apply) a column mapping and show how the first rows will import."""
    headers, rows, mapping = _parse_csv(body)
    result = {"headers": headers, "mapping": mapping, "rows": len(rows), "sample_rows": rows[:3]}
    try:
        txs, errors, _ = importer.interpret(headers, rows, mapping)
    except ValueError as e:
        return result | {"problem": str(e), "preview": [], "errors": [], "error_count": 0}

    rules = dict(conn.execute("SELECT merchant, category FROM merchant_rules").fetchall())
    aliases = dict(conn.execute("SELECT alias, merchant FROM merchant_aliases").fetchall())
    description_rules = conn.execute(
        "SELECT merchant, pattern, category FROM description_rules ORDER BY length(pattern) DESC").fetchall()
    preview = []
    for t in txs[:8]:
        merchant = normalize_merchant(t["description"])
        merchant = aliases.get(merchant, merchant)
        key = description_key(t["description"])
        category = (next((d["category"] for d in description_rules if d["merchant"] == merchant and d["pattern"] in key), None)
                    or rules.get(merchant) or db.resolve_category(conn, categorize(t["description"], t["amount"])))
        posted = int(datetime(t["date"].year, t["date"].month, t["date"].day, 12).timestamp())  # as importer saves it
        day = date.fromtimestamp(purchase_date(posted, t["description"]))
        preview.append({"date": day.isoformat(), "description": t["description"],
                        "amount": t["amount"], "category": category, "name": display_name(merchant)})
    dates = [t["date"] for t in txs]
    return result | {
        "problem": None,
        "preview": preview,
        "count": len(txs),
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
        "errors": errors[:5],
        "error_count": len(errors),
    }


class NewAccount(BaseModel):
    name: str
    org_name: str = ""
    balance: float = 0


class ImportBody(ImportPreviewBody):
    account_id: str | None = None
    new_account: NewAccount | None = None


@app.post("/api/import")
def import_csv(body: ImportBody, conn=Depends(get_conn)):
    headers, rows, mapping = _parse_csv(body)
    try:
        txs, errors, _ = importer.interpret(headers, rows, mapping)
    except ValueError as e:
        fail(e)

    with conn:
        if body.new_account:
            name = " ".join(body.new_account.name.split())
            if not name:
                fail(Exception("Give the new account a name."))
            account_id = f"manual-{uuid.uuid4().hex[:10]}"
            upsert_account(conn, {"id": account_id, "name": name, "org": {"name": body.new_account.org_name.strip()},
                                  "balance": str(body.new_account.balance), "balance-date": int(time.time())})
            conn.execute(
                "INSERT INTO balance_snapshots (account_id, day, balance) VALUES (?, ?, ?)",
                (account_id, date.today().isoformat(), body.new_account.balance))
        else:
            account_id = body.account_id
            if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id or "",)).fetchone():
                fail(Exception("Choose an account to import into."))

        db.archive(conn, "csv", body.file_name or "imported.csv", body.csv.encode())
        before = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ? AND id LIKE ?",
                              (account_id, f"{account_id}:csv:%")).fetchone()[0]
        # Year-end summaries have no card payments, so they can't rebuild past balances.
        affects_balance = not mapping.get("no_balance")
        for tx in importer.to_simplefin_shape(txs, affects_balance):
            upsert_transaction(conn, account_id, tx)
        importer.drop_overlap(conn, account_id)
        match_transfers(conn)
        after = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ? AND id LIKE ?",
                             (account_id, f"{account_id}:csv:%")).fetchone()[0]
        if db.get_setting(conn, "mode") is None:
            db.set_setting(conn, "mode", "manual")

    backup_if_real(conn)
    imported = after - before
    return {"imported": imported, "skipped": len(txs) - imported, "unreadable": len(errors), "account_id": account_id}


class PdfFile(BaseModel):
    name: str
    data: str  # base64


class PdfImportBody(BaseModel):
    files: list[PdfFile]


MAX_PDF_BYTES = 15 * 1024 * 1024


@app.post("/api/import/pdf")
def import_pdf(body: PdfImportBody, conn=Depends(get_conn)):
    """Read statement PDFs into the same CSV form the importer already handles.

    Each PDF is kept in the originals archive as soon as it's read; the
    transactions are only saved once the user confirms the preview.
    """
    statements, rows = [], []
    for f in body.files:
        entry = {"name": f.name}
        try:
            data = base64.b64decode(f.data, validate=True)
            if len(data) > MAX_PDF_BYTES:
                raise pdf_statements.StatementError("is too large.")
            parsed = pdf_statements.parse(data)
            with conn:
                db.archive(conn, "pdf", f.name, data)
        except (binascii.Error, pdf_statements.StatementError) as e:
            statements.append(entry | {"error": f"{f.name} {e}"})
            continue
        statements.append(entry | {
            "kind": parsed["kind"], "period": parsed["period"], "account": parsed["account"],
            "count": len(parsed["transactions"]), "check": parsed["check"],
            "affects_balance": parsed["affects_balance"],
        })
        rows += parsed["transactions"]

    ok = [s for s in statements if "error" not in s]
    if len({s["affects_balance"] for s in ok}) > 1:
        fail(Exception("Import year-end summaries separately from monthly statements."))
    rows.sort(key=lambda t: t["date"])
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Description", "Amount"])
    writer.writerows([t["date"].isoformat(), t["description"], f"{t['amount']:.2f}"] for t in rows)
    return {
        "csv": out.getvalue(),
        "mapping": {"date": 0, "description": 1, "amount": 2, "debit": None, "credit": None,
                    "flip": False, "no_balance": bool(ok) and not ok[0]["affects_balance"]},
        "statements": statements,
    }


@app.get("/api/import/summary")
def import_summary(conn=Depends(get_conn)):
    """Imported history per account, for the undo list."""
    rows = conn.execute("""
        SELECT a.id, COALESCE(a.nickname, a.name) AS name, a.org_name, COUNT(*) AS count,
               date(MIN(t.posted), 'unixepoch', 'localtime') AS first_date,
               date(MAX(t.posted), 'unixepoch', 'localtime') AS last_date
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.id LIKE a.id || ':csv:%'
        GROUP BY a.id ORDER BY 2""").fetchall()
    return [dict(r) | {"manual": r["id"].startswith("manual-")} for r in rows]


@app.delete("/api/import")
def undo_import(account_id: str, conn=Depends(get_conn)):
    """Remove all imported transactions for an account (and the account itself if it was created by an import)."""
    backup_if_real(conn, "before-undo-import")
    with conn:
        if account_id.startswith("manual-"):
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        else:
            conn.execute("DELETE FROM transactions WHERE account_id = ? AND id LIKE ?",
                         (account_id, f"{account_id}:csv:%"))
        if not conn.execute("SELECT 1 FROM accounts").fetchone():
            db.set_setting(conn, "mode", None)
    return {"ok": True}


@app.post("/api/shutdown")
def shutdown(background: BackgroundTasks):
    """Stop the server. Used by the Stop Penny button; the Penny app starts it again."""
    background.add_task(os.kill, os.getpid(), signal.SIGTERM)
    return {"ok": True}


# ---------- web UI ----------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


NO_CACHE = {"Cache-Control": "no-cache"}  # browsers must check for a newer version each time


@app.get("/")
def index():
    """The page, with each script and stylesheet URL stamped with its file's
    version, so a browser never mixes a new page with old cached code."""
    html = (STATIC / "index.html").read_text()
    for asset in ("app.js", "style.css"):
        version = hashlib.sha1((STATIC / asset).read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return Response(html, media_type="text/html", headers=NO_CACHE)
