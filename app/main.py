"""Penny API + static web UI.

Every endpoint under /api returns JSON, so a future iPhone app can talk to
this same server.
"""
import base64
import binascii
import csv
import io
import os
import signal
import time
from urllib.parse import urlsplit
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, demo, importer, pdf_statements, simplefin, subscriptions
from .categorize import COLOR_SLOTS, LOCKED_CATEGORIES, NON_SPENDING, categorize, display_name, normalize_merchant
from .sync import recategorize_all, snapshot_balances, sync, upsert_account, upsert_transaction

STATIC = Path(__file__).resolve().parent.parent / "static"
@asynccontextmanager
async def lifespan(app):
    # Apply any keyword improvements to transactions synced before them.
    conn = db.connect()
    try:
        recategorize_all(conn)
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
    return await call_next(request)

TX_SELECT = """
    SELECT t.id, t.account_id, t.posted, t.amount, t.description, t.merchant, t.pending,
           COALESCE(r.category, t.category) AS category,
           strftime('%Y-%m', t.posted, 'unixepoch', 'localtime') AS month,
           a.name AS account_name
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN merchant_rules r ON r.merchant = t.merchant
"""
SPENDING_FILTER = "category NOT IN ({})".format(",".join(f"'{c}'" for c in NON_SPENDING))


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
        return sync(conn)
    except Exception as e:
        fail(e)


@app.post("/api/demo")
def load_demo(conn=Depends(get_conn)):
    if db.get_setting(conn, "simplefin_access_url"):
        fail(Exception("Disconnect SimpleFIN before loading demo data."))
    if db.get_setting(conn, "mode") == "manual":
        fail(Exception("Loading demo data would replace your imported transactions. Remove your imports first."))
    demo.load(conn)
    return {"ok": True}


@app.post("/api/disconnect")
def disconnect(conn=Depends(get_conn)):
    """Forget the SimpleFIN connection and delete all synced data."""
    with conn:
        db.clear_financial_data(conn)
        db.set_setting(conn, "simplefin_access_url", None)
    return {"ok": True}


# ---------- accounts & net worth ----------

@app.get("/api/accounts")
def accounts(conn=Depends(get_conn)):
    rows = [dict(r) for r in conn.execute(
        "SELECT id, org_name, name, currency, balance, available_balance, balance_date "
        "FROM accounts WHERE hidden = 0 ORDER BY org_name, name")]
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


def earlier_balances(conn, snaps) -> list[dict]:
    """Weekly balances from before each account's first recorded balance.

    Balances are only recorded when Penny syncs, but transactions can go back
    much further (imported CSV history). Walking those transactions backward
    from the first recorded balance rebuilds what the balance must have been.
    """
    first: dict[str, dict] = {}
    for s in snaps:
        first.setdefault(s["account_id"], s)
    out = []
    for account_id, s in first.items():
        day0 = date.fromisoformat(s["day"])
        txs = [(date.fromtimestamp(r["posted"]), r["amount"]) for r in conn.execute(
            "SELECT posted, amount FROM transactions WHERE account_id = ? AND affects_balance = 1 "
            "ORDER BY posted DESC", (account_id,))]
        txs = [t for t in txs if t[0] <= day0]
        if not txs or txs[-1][0] >= day0:
            continue
        balance, i = s["balance"], 0
        d = day0 - timedelta(days=7)
        while d >= txs[-1][0] - timedelta(days=7):
            # Undo every transaction that happened after day d.
            while i < len(txs) and txs[i][0] > d:
                balance -= txs[i][1]
                i += 1
            out.append({"account_id": account_id, "day": d.isoformat(), "balance": round(balance, 2)})
            d -= timedelta(days=7)
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
        conn.execute("UPDATE category_aliases SET new_name = ? WHERE new_name = ?", (move_to, name))
        conn.execute(
            "INSERT INTO category_aliases (old_name, new_name) VALUES (?, ?) "
            "ON CONFLICT(old_name) DO UPDATE SET new_name = excluded.new_name", (name, move_to))
        conn.execute("DELETE FROM categories WHERE name = ?", (name,))
    return {"ok": True}


@app.get("/api/spending/monthly")
def spending_monthly(months: int = 12, conn=Depends(get_conn)):
    rows = conn.execute(f"""
        SELECT month,
               ROUND(-SUM(CASE WHEN {SPENDING_FILTER} THEN amount ELSE 0 END), 2) AS spending,
               ROUND(SUM(CASE WHEN category = 'Income' THEN amount ELSE 0 END), 2) AS income
        FROM ({TX_SELECT}) GROUP BY month ORDER BY month DESC LIMIT ?""", (months,)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/spending/breakdown")
def spending_breakdown(month: str | None = None, conn=Depends(get_conn)):
    month = month or date.today().strftime("%Y-%m")
    by_cat = conn.execute(f"""
        SELECT category, ROUND(-SUM(amount), 2) AS total, COUNT(*) AS count
        FROM ({TX_SELECT}) WHERE month = ? AND {SPENDING_FILTER}
        GROUP BY category HAVING total > 0 ORDER BY total DESC""", (month,)).fetchall()
    by_merchant = conn.execute(f"""
        SELECT merchant, category, ROUND(-SUM(amount), 2) AS total, COUNT(*) AS count
        FROM ({TX_SELECT}) WHERE month = ? AND {SPENDING_FILTER}
        GROUP BY merchant HAVING total > 0 ORDER BY total DESC LIMIT 10""", (month,)).fetchall()
    return {
        "month": month,
        "total": round(sum(r["total"] for r in by_cat), 2),
        "categories": [dict(r) for r in by_cat],
        "merchants": [dict(r) | {"name": display_name(r["merchant"])} for r in by_merchant],
    }


# ---------- transactions ----------

@app.get("/api/transactions")
def transactions(month: str | None = None, category: str | None = None,
                 merchant: str | None = None, q: str | None = None,
                 limit: int = 300, conn=Depends(get_conn)):
    where, params = [], []
    if month:
        where.append("month = ?"); params.append(month)
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


# ---------- merchants ----------

@app.get("/api/merchants")
def merchants(conn=Depends(get_conn)):
    rows = conn.execute("""
        SELECT t.merchant, COUNT(*) AS count,
               ROUND(-SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END), 2) AS spent,
               MAX(t.posted) AS last_posted, MAX(t.description) AS example,
               r.category AS rule_category, l.version AS logo_version
        FROM transactions t
        LEFT JOIN merchant_rules r ON r.merchant = t.merchant
        LEFT JOIN merchant_logos l ON l.merchant = t.merchant
        GROUP BY t.merchant ORDER BY count DESC, spent DESC""").fetchall()
    # The automatic category is whichever one most of the merchant's transactions got.
    auto: dict[str, tuple[str, int]] = {}
    for r in conn.execute("SELECT merchant, category, COUNT(*) AS n FROM transactions GROUP BY merchant, category"):
        if r["merchant"] not in auto or r["n"] > auto[r["merchant"]][1]:
            auto[r["merchant"]] = (r["category"], r["n"])
    return [dict(r) | {
        "name": display_name(r["merchant"]),
        "auto_category": auto[r["merchant"]][0],
        "last_date": date.fromtimestamp(r["last_posted"]).isoformat(),
    } for r in rows]


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
    txs = conn.execute(f"SELECT merchant, posted, amount, category FROM ({TX_SELECT})").fetchall()
    overrides = {r["merchant"]: r["status"] for r in conn.execute("SELECT * FROM subscription_overrides")}
    found = subscriptions.detect(
        [dict(t) for t in txs], date.today(),
        names={t["merchant"]: display_name(t["merchant"]) for t in txs},
        overrides=overrides)
    active = [s for s in found if s.active and not s.ignored]
    return {
        "monthly_total": round(sum(s.monthly_cost for s in active), 2),
        "yearly_total": round(sum(s.yearly_cost for s in active), 2),
        "items": [s.to_dict() for s in found],
    }


class OverrideBody(BaseModel):
    merchant: str
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
    preview = []
    for t in txs[:8]:
        merchant = normalize_merchant(t["description"])
        category = rules.get(merchant) or db.resolve_category(conn, categorize(t["description"], t["amount"]))
        preview.append({"date": t["date"].isoformat(), "description": t["description"],
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

        before = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ? AND id LIKE ?",
                              (account_id, f"{account_id}:csv:%")).fetchone()[0]
        # Year-end summaries have no card payments, so they can't rebuild past balances.
        affects_balance = not mapping.get("no_balance")
        for tx in importer.to_simplefin_shape(txs, affects_balance):
            upsert_transaction(conn, account_id, tx)
        importer.drop_overlap(conn, account_id)
        after = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ? AND id LIKE ?",
                             (account_id, f"{account_id}:csv:%")).fetchone()[0]
        if db.get_setting(conn, "mode") is None:
            db.set_setting(conn, "mode", "manual")

    imported = after - before
    return {"imported": imported, "skipped": len(txs) - imported, "unreadable": len(errors), "account_id": account_id}


class PdfFile(BaseModel):
    name: str
    data: str  # base64


class PdfImportBody(BaseModel):
    files: list[PdfFile]


MAX_PDF_BYTES = 15 * 1024 * 1024


@app.post("/api/import/pdf")
def import_pdf(body: PdfImportBody):
    """Read statement PDFs into the same CSV form the importer already handles.

    Nothing is saved here; the UI previews the result and then imports it.
    """
    statements, rows = [], []
    for f in body.files:
        entry = {"name": f.name}
        try:
            data = base64.b64decode(f.data, validate=True)
            if len(data) > MAX_PDF_BYTES:
                raise pdf_statements.StatementError("is too large.")
            parsed = pdf_statements.parse(data)
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
        SELECT a.id, a.name, a.org_name, COUNT(*) AS count,
               date(MIN(t.posted), 'unixepoch', 'localtime') AS first_date,
               date(MAX(t.posted), 'unixepoch', 'localtime') AS last_date
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE t.id LIKE a.id || ':csv:%'
        GROUP BY a.id ORDER BY a.name""").fetchall()
    return [dict(r) | {"manual": r["id"].startswith("manual-")} for r in rows]


@app.delete("/api/import")
def undo_import(account_id: str, conn=Depends(get_conn)):
    """Remove all imported transactions for an account (and the account itself if it was created by an import)."""
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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
