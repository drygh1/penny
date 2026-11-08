"""Penny API + static web UI.

Every endpoint under /api returns JSON, so a future iPhone app can talk to
this same server.
"""
import base64
import hashlib
import binascii
import csv
import io
import json
import os
import re
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

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, backup, db, demo, importer, pdf_statements, remote, simplefin, subscriptions
from .categorize import (COLOR_SLOTS, FIXED_NAMES, LOCKED_CATEGORIES, NON_SPENDING, categorize, description_key, display_name,
                         institution_words, normalize_merchant)
from .sync import (card_money_in, match_transfers, purchase_date, refresh_categories, snapshot_balances, sync, transfer_pairs, upsert_account,
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
    remote.start_watcher(app)  # does nothing unless sharing is turned on in Settings
    yield
    remote.stop()


app = FastAPI(title="Penny", lifespan=lifespan)

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
LOCAL_CLIENTS = {"127.0.0.1", "::1"}
# What a signed-out device may load: the sign-in page and the files it needs.
SIGN_IN_PATHS = {"/login", "/api/login", "/manifest.webmanifest"}
# Things only this Mac can do, even for a signed-in phone: anything that changes
# sharing, your bank connection or backups, or deletes everything.
MAC_ONLY = ("/api/remote", "/api/simplefin", "/api/disconnect", "/api/delete-all-data",
            "/api/backups", "/api/shutdown", "/api/demo", "/api/share")
SECURITY_HEADERS = {
    # Only Penny's own scripts run, and the page can only talk to Penny, so even
    # injected code couldn't send your data anywhere. Styles allow inline
    # style="" (bar widths, colors); images allow data:/blob: for picture uploads.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'; object-src 'none'",
    "X-Frame-Options": "DENY",            # no other page can show Penny inside itself
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def is_local(request: Request) -> bool:
    return (request.client.host if request.client else "") in LOCAL_CLIENTS


@app.middleware("http")
async def guard(request: Request, call_next):
    """Who may use Penny.

    From this Mac: only pages Penny itself serves. The Host check stops
    DNS-rebinding tricks; the Origin check stops a page on another site from
    sending changes like "delete all data".

    From another device (only possible with sharing on, see remote.py): the
    same checks against the shared address, then a signed-in session, and
    never the Mac-only actions. Its data is never cached on the device.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    origin = request.headers.get("origin")
    path = request.url.path
    local = is_local(request)
    allowed = LOCAL_HOSTS if local else remote.allowed_hosts()
    if host not in allowed:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    if request.method not in ("GET", "HEAD") and origin and urlsplit(origin).hostname not in allowed:
        return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    if not local:
        if path.startswith(MAC_ONLY):
            return JSONResponse({"detail": "That can only be done on the Mac running Penny."}, status_code=403)
        if path not in SIGN_IN_PATHS and not path.startswith("/static/"):
            conn = db.connect()
            try:
                signed_in = auth.valid_session(conn, request.cookies.get(auth.COOKIE))
            finally:
                conn.close()
            if not signed_in:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Sign in first."}, status_code=401)
                return Response(status_code=303, headers={"Location": "/login"})
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if path.startswith("/static/"):
        response.headers.update(NO_CACHE)
    elif not local:
        response.headers["Cache-Control"] = "no-store"
    return response

# `merchant` is the combined merchant (see merchant_aliases); rules, logos and
# everything shown use it. `own_merchant` is what the bank's wording gave.
TX_SELECT = """
    SELECT t.id, t.account_id, t.posted, t.amount, t.description, t.merchant, t.own_merchant, t.pending,
           COALESCE(o.category, d.category, r.category, t.category) AS category,
           COALESCE(d.category, r.category, t.category) AS default_category,
           o.category IS NOT NULL AS edited,
           COALESCE(rv.reviewed, t.posted < COALESCE(
               (SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'review_since'), 0)) AS reviewed,
           d.pattern AS description_rule,
           strftime('%Y-%m', t.posted, 'unixepoch', 'localtime') AS month,
           COALESCE(a.nickname, a.name) AS account_name
    FROM (SELECT t.id, t.account_id, t.posted, t.amount, t.description, t.payee, t.pending, t.category,
                 t.affects_balance, t.merchant AS own_merchant, COALESCE(ma.merchant, t.merchant) AS merchant
          FROM transactions t LEFT JOIN merchant_aliases ma ON ma.alias = t.merchant) t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN transaction_categories o ON o.tx_id = t.id
    LEFT JOIN reviewed rv ON rv.tx_id = t.id
    LEFT JOIN merchant_rules r ON r.merchant = t.merchant
    LEFT JOIN description_rules d ON d.merchant = t.merchant AND d.pattern = (
        SELECT d2.pattern FROM description_rules d2
        WHERE d2.merchant = t.merchant AND instr(desc_key(t.description), d2.pattern) > 0
        ORDER BY length(d2.pattern) DESC LIMIT 1)
"""
SPENDING_FILTER = "category NOT IN ({})".format(",".join(f"'{c}'" for c in NON_SPENDING))
# Income sources colored on the dashboard, using the color slots in their
# colorblind-checked order. One per palette color, so in practice every source
# gets its own and only past the 20th do they share the gray slice.
INCOME_COLORS = 20


def merchant_namer(conn):
    """Each merchant's name: yours if you renamed it, else made from the bank's wording."""
    custom = dict(conn.execute("SELECT merchant, name FROM merchant_names").fetchall())
    return lambda merchant: custom.get(merchant) or display_name(merchant)


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
def status(request: Request, conn=Depends(get_conn)):
    last = db.get_setting(conn, "last_sync")
    return {
        "mode": db.get_setting(conn, "mode"),  # None, "demo", or "simplefin"
        "connected": db.get_setting(conn, "simplefin_access_url") is not None,
        "last_sync": datetime.fromtimestamp(int(last)).isoformat() if last else None,
        "desktop": DESKTOP,  # the Mac app stops when you quit it, so it has no Stop button
        "remote": not is_local(request),  # on a phone over Meshnet: Mac-only controls are hidden
        "to_review": conn.execute(f"SELECT COUNT(*) FROM ({TX_SELECT}) WHERE NOT reviewed").fetchone()[0],
    }


# ---------- using Penny from other devices (see remote.py and auth.py) ----------

class LoginBody(BaseModel):
    passcode: str


@app.post("/api/login")
def login(body: LoginBody, request: Request, conn=Depends(get_conn)):
    client = request.client.host if request.client else "?"
    if auth.locked_out(client):
        raise HTTPException(429, "Too many wrong passcodes. Wait 15 minutes and try again.")
    if not auth.check_passcode(conn, body.passcode):
        auth.note_failure(client)
        raise HTTPException(401, "That passcode isn't right.")
    with conn:
        token = auth.new_session(conn, request.headers.get("user-agent", ""))
    response = JSONResponse({"ok": True})
    # Served over Meshnet/Tailscale, which encrypt the connection; HttpOnly keeps
    # page scripts from reading it, SameSite=Strict keeps other sites from using it.
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        samesite="strict", path="/")
    return response


@app.post("/api/logout")
def logout(request: Request, conn=Depends(get_conn)):
    with conn:
        auth.end_session(conn, request.cookies.get(auth.COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/api/remote")
def remote_status(conn=Depends(get_conn)):
    return remote.status(conn)


class RemoteBody(BaseModel):
    enabled: bool | None = None
    address: str | None = None
    passcode: str | None = None
    keep_awake: bool | None = None


@app.post("/api/remote")
def remote_settings(body: RemoteBody, conn=Depends(get_conn)):
    with conn:
        if body.address is not None:
            if body.address not in remote.private_addresses():
                fail(Exception("That address isn't on this Mac right now. Is Meshnet on?"))
            db.set_setting(conn, "remote_address", body.address)
        if body.passcode is not None:
            try:
                auth.set_passcode(conn, body.passcode)
            except ValueError as e:
                fail(e)
        if body.keep_awake is not None:
            db.set_setting(conn, "remote_keep_awake", "1" if body.keep_awake else "0")
        if body.enabled is not None:
            if body.enabled and not (db.get_setting(conn, "remote_address") and db.get_setting(conn, "remote_passcode")):
                fail(Exception("Choose the address and set a passcode first."))
            db.set_setting(conn, "remote_enabled", "1" if body.enabled else None)
    remote.refresh()
    return remote.status(conn)


@app.post("/api/remote/sign-out-all")
def remote_sign_out_all(conn=Depends(get_conn)):
    with conn:
        auth.sign_out_all(conn)
    return {"ok": True}


@app.get("/api/remote/qr")
def remote_qr():
    """The sharing address as a QR code, for the iPhone's camera."""
    import segno
    link = remote.url()
    if not link:
        fail(Exception("Sharing isn't on."))
    svg = segno.make(link, error="m").svg_inline(scale=5, dark="#1a1a19", light="#ffffff", border=2)
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/share")
def share_info():
    from . import share
    label, macs = share.chip()
    return {"available": share.this_app() is not None, "macs": macs}


@app.post("/api/share")
def share_copy():
    """A copy of Penny for a friend on the Desktop, checked for anything of yours first."""
    from . import share
    try:
        path = share.make_friend_copy()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
        fail(Exception(str(e)))
    subprocess.run(["open", "-R", str(path)], check=False)  # show it in Finder
    return {"ok": True, "path": str(path), "name": path.name}


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html", headers=NO_CACHE)


@app.get("/manifest.webmanifest")
def manifest():
    """Lets an iPhone add Penny to its Home Screen as an app."""
    return JSONResponse({
        "name": "Penny", "short_name": "Penny", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#fcfcfb", "theme_color": "#fcfcfb",
        "icons": [{"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icons/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}],
    }, media_type="application/manifest+json")


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
    with conn:
        db.set_setting(conn, "guide:demo", 1)
    return {"ok": True}


# ---------- getting started ----------
# A checklist on the dashboard and a tip on each page, for someone new to Penny.
# Steps tick themselves off from what you've done; visits count only once real
# data is in, so poking around the demo doesn't check anything off.

GUIDE_VIEWS = ("dashboard", "review", "subscriptions", "transactions", "merchants", "accounts")


@app.get("/api/guide")
def guide(conn=Depends(get_conn)):
    real = has_real_data(conn)
    seen = lambda view: bool(db.get_setting(conn, f"guide:seen:{view}"))
    any_rows = lambda table: conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    edited = real and any(any_rows(t) for t in ("merchant_rules", "description_rules",
                                               "transaction_categories", "merchant_aliases", "merchant_names"))
    imported = conn.execute("SELECT 1 FROM raw_archive WHERE kind IN ('csv', 'pdf') LIMIT 1").fetchone()
    steps = [
        {"id": "demo", "optional": True, "done": bool(db.get_setting(conn, "guide:demo")) or real},
        {"id": "connect", "optional": False, "done": real},
        {"id": "import", "optional": True, "done": imported is not None},
        {"id": "categories", "optional": False, "done": edited or seen("merchants")},
        {"id": "subscriptions", "optional": False, "done": seen("subscriptions")},
        {"id": "review", "optional": False,
         "done": real and (seen("review") or any_rows("reviewed"))},
        {"id": "phone", "optional": True,
         "done": any_rows("remote_sessions") or db.get_setting(conn, "remote_enabled") == "1"},
    ]
    return {
        "steps": steps,
        "dismissed": bool(db.get_setting(conn, "guide:dismissed")),
        "tips_dismissed": [v for v in GUIDE_VIEWS if db.get_setting(conn, f"guide:tip:{v}")],
    }


class GuideBody(BaseModel):
    view: str | None = None


@app.post("/api/guide/seen")
def guide_seen(body: GuideBody, conn=Depends(get_conn)):
    """Note a page visit, for the checklist. Ignored until real data is in."""
    if body.view in GUIDE_VIEWS and has_real_data(conn):
        with conn:
            db.set_setting(conn, f"guide:seen:{body.view}", 1)
    return {"ok": True}


@app.post("/api/guide/dismiss")
def guide_dismiss(body: GuideBody, conn=Depends(get_conn)):
    """Hide the checklist (no view) or one page's tip, for good."""
    if body.view is not None and body.view not in GUIDE_VIEWS:
        fail(Exception(f"Unknown page: {body.view}"))
    with conn:
        db.set_setting(conn, f"guide:tip:{body.view}" if body.view else "guide:dismissed", 1)
    return {"ok": True}


# ---------- updates (Mac app) ----------
# Once a day the app asks GitHub for the newest release. Nothing about you or your
# data is sent; it's a plain request for the release list. Turn it off in Settings.

UPDATE_FEED = os.environ.get("PENNY_UPDATE_FEED", "https://api.github.com/repos/drygh1/penny/releases/latest")
UPDATE_EVERY = 24 * 3600


def _version_tuple(v: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


@app.get("/api/update")
def check_update(force: bool = False, conn=Depends(get_conn)):
    from . import __version__
    enabled = db.get_setting(conn, "updates_off") is None
    result = {"current": __version__, "enabled": enabled, "available": False}
    if not (DESKTOP and enabled and UPDATE_FEED):
        return result
    checked = int(db.get_setting(conn, "update_checked_at") or 0)
    if force or time.time() - checked > UPDATE_EVERY:
        try:
            r = httpx.get(UPDATE_FEED, timeout=5, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            release = r.json()
            latest = {"version": release.get("tag_name", "").lstrip("v"), "url": release.get("html_url", ""),
                      "notes": (release.get("body") or "")[:600]}
        except Exception:
            latest = None  # offline, or no public releases yet: try again tomorrow
        with conn:
            db.set_setting(conn, "update_checked_at", int(time.time()))
            if latest and latest["version"]:
                db.set_setting(conn, "update_latest", json.dumps(latest))
    latest = json.loads(db.get_setting(conn, "update_latest") or "null")
    if latest and _version_tuple(latest["version"]) > _version_tuple(__version__) \
            and db.get_setting(conn, "update_dismissed") != latest["version"]:
        result |= {"available": True, "latest": latest["version"], "url": latest["url"], "notes": latest["notes"]}
    return result


class UpdateSettingsBody(BaseModel):
    enabled: bool | None = None
    dismiss: str | None = None  # hide the notice for this version


@app.post("/api/update")
def update_settings(body: UpdateSettingsBody, conn=Depends(get_conn)):
    with conn:
        if body.enabled is not None:
            db.set_setting(conn, "updates_off", None if body.enabled else 1)
        if body.dismiss:
            db.set_setting(conn, "update_dismissed", body.dismiss)
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
        by_day[s["day"]] = sum(latest.values())
    in_flight = transfers_in_flight(conn)
    by_day = {d: round(v - in_flight(d), 2) for d, v in by_day.items()}

    return {
        "accounts": rows,
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net_worth": round(assets + debts, 2),
        "history": [{"day": d, "net_worth": v} for d, v in by_day.items()],
    }


def transfers_in_flight(conn):
    """A function of day: money one of your accounts already shows that the other hasn't.

    Each bank posts its side of a transfer or card payment when it gets to it:
    a card can show a $3,715 payment on Oct 31 while checking only takes it out
    on Nov 2. In between, the money is counted twice (or, the other way round,
    not at all), and net worth jumps for a couple of days. Subtracting the side
    already posted, until the other side posts too, takes the jump out.

    A recorded balance includes what posted before that day (banks report
    balances a day behind their transaction dates); balances rebuilt from
    transactions (before an account's first sync) include that day's too.
    """
    rows = conn.execute(
        "SELECT t.account_id, t.posted, t.amount FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE a.hidden = 0 AND t.amount != 0 AND t.category IN ('Transfers', 'Credit Card Payments') "
        "ORDER BY t.posted").fetchall()
    first_sync = dict(conn.execute("SELECT account_id, MIN(day) FROM balance_snapshots GROUP BY account_id").fetchall())
    sides = [(s["account_id"], date.fromtimestamp(s["posted"]).isoformat(), s["amount"])
             for w, d in transfer_pairs(rows)
             if date.fromtimestamp(w["posted"]) != date.fromtimestamp(d["posted"]) for s in (w, d)]

    def counted_twice(day: str) -> float:
        # Once both sides have posted they cancel out, so only a lone side adds up.
        return sum(amount for account_id, posted, amount in sides
                   if (posted < day if day >= first_sync.get(account_id, day) else posted <= day))
    return counted_twice


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

    Every account counts across the whole chart. Before its own transactions
    begin, an account holds the earliest balance they lead back to: Penny
    doesn't know it, and counting it as $0 would make the chart jump the day
    its history starts.

    Brokerages and crypto wallets often report a balance but few or no
    transactions. For those, the deposits and withdrawals seen from your other
    accounts ("FID BKG SVC" in savings is money into Fidelity) carry on where
    their own transactions stop, with all of one institution's accounts as one
    pool, so moving money in doesn't look like it disappeared. Market gains and
    losses can't be seen, so this part is an estimate; it never goes below zero.
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
        org = conn.execute("SELECT org_name FROM accounts WHERE id = ?", (account_id,)).fetchone()["org_name"]
        if institution_words(org):
            pools.setdefault(org, []).append(s)
            continue
        day0 = date.fromisoformat(s["day"])
        own = [(d, amt) for d, amt, _ in history[account_id] if d <= day0]
        out += _walk_back(account_id, day0, s["balance"], own, earliest, anchor)

    for org, members in pools.items():
        words = institution_words(org)
        ids = {s["account_id"] for s in members}
        day0 = min(date.fromisoformat(s["day"]) for s in members)
        own = [(d, amt) for a in ids for d, amt, _ in history[a] if d <= day0]
        # Money leaving your other accounts toward this institution came into it,
        # counted only from before its own records start, so nothing counts twice.
        own_start = min((d for d, _ in own), default=day0 + timedelta(days=1))
        flows = [(d, -amt) for a, txs in history.items() if a not in ids for d, amt, desc in txs
                 if d < own_start and any(w in desc.lower() for w in words)]
        # The pool's history rides on its biggest account; at the first sync
        # every member's real balance takes over.
        lead = max(members, key=lambda s: s["balance"])["account_id"]
        total = sum(s["balance"] for s in members)
        out += _walk_back(lead, day0, total, sorted(own + flows, reverse=True), earliest, anchor, floor=0)
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
        "fixed_name": r["name"] in FIXED_NAMES,
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


class CategoryRenameBody(BaseModel):
    name: str
    new_name: str


def bill_categories(conn) -> set[str]:
    """Categories that count as bills, under whatever names they have now."""
    saved = db.get_setting(conn, "bill_categories")
    return set(json.loads(saved)) if saved else set(subscriptions.BILL_CATEGORIES)


@app.put("/api/categories")
def rename_category(body: CategoryRenameBody, conn=Depends(get_conn)):
    """Rename a category. Its transactions, rules and color come along, and
    transactions Penny files under the old name from now on land here too."""
    old, new = body.name, " ".join(body.new_name.split())
    names = db.category_names(conn)
    if old not in names:
        fail(Exception(f"Unknown category: {old}"))
    if old in FIXED_NAMES:
        fail(Exception(f"{old} is built into Penny's totals, so its name can't change."))
    if not 1 <= len(new) <= 40:
        fail(Exception("Category names must be 1 to 40 characters."))
    if new == old:
        return {"ok": True, "name": new}
    if new in FIXED_NAMES or any(n.lower() == new.lower() and n != old for n in names):
        fail(Exception(f"There's already a category called {new}."))
    backup_if_real(conn, "before-category-rename")
    bills = bill_categories(conn)
    with conn:
        conn.execute("UPDATE categories SET name = ? WHERE name = ?", (new, old))
        for table in ("transactions", "merchant_rules", "description_rules", "transaction_categories",
                      "manual_recurring"):
            conn.execute(f"UPDATE {table} SET category = ? WHERE category = ?", (new, old))
        conn.execute("UPDATE category_aliases SET new_name = ? WHERE new_name = ?", (new, old))
        conn.execute(
            "INSERT INTO category_aliases (old_name, new_name) VALUES (?, ?) "
            "ON CONFLICT(old_name) DO UPDATE SET new_name = excluded.new_name", (old, new))
        # The new name is a real category now, so nothing should redirect away from it.
        conn.execute("DELETE FROM category_aliases WHERE old_name = ?", (new,))
        if old in bills:
            db.set_setting(conn, "bill_categories", json.dumps(sorted(bills - {old} | {new})))
    return {"ok": True, "name": new}


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
        conn.execute("UPDATE manual_recurring SET category = ? WHERE category = ?", (move_to, name))
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
    every_cat = conn.execute(f"""
        SELECT category, ROUND(-SUM(amount), 2) AS total, COUNT(*) AS count
        {source} AND {SPENDING_FILTER}
        GROUP BY category ORDER BY total DESC""", params).fetchall()
    by_cat = [r for r in every_cat if r["total"] > 0]
    # A category where more came back than went out (refunds) can't be a slice,
    # but it still lowers spending, so it's listed separately and counted, keeping
    # this total the same as the Income & spending chart's.
    credits = [dict(r) | {"total": -r["total"]} for r in every_cat if r["total"] < 0]
    by_merchant = conn.execute(f"""
        SELECT merchant, category, ROUND(-SUM(amount), 2) AS total, COUNT(*) AS count
        {source} AND {SPENDING_FILTER}
        GROUP BY merchant HAVING total > 0 ORDER BY total DESC LIMIT 10""", params).fetchall()
    by_source = conn.execute(f"""
        SELECT merchant, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
        {source} AND category = 'Income'
        GROUP BY merchant HAVING total > 0 ORDER BY total DESC LIMIT 10""", params).fetchall()
    spending = round(sum(r["total"] for r in by_cat) - sum(r["total"] for r in credits), 2)
    income = round(conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) {source} AND category = 'Income'", params).fetchone()[0], 2)
    # Your biggest income sources of all time get a color each, so a source keeps
    # its color whichever month you look at; the rest share "Everything else".
    income_slots = {r["merchant"]: i + 1 for i, r in enumerate(conn.execute(f"""
        SELECT merchant FROM ({TX_SELECT}) WHERE category = 'Income'
        GROUP BY merchant HAVING SUM(amount) > 0 ORDER BY SUM(amount) DESC LIMIT {INCOME_COLORS}"""))}
    name = merchant_namer(conn)
    return {
        "period": period,
        "month": period,  # older name, kept for compatibility
        "total": spending,
        "income": income,
        "net": round(income - spending, 2),
        "categories": [dict(r) for r in by_cat],
        "credits": credits,  # categories where more came back than went out
        "merchants": [dict(r) | {"name": name(r["merchant"])} for r in by_merchant],
        "income_sources": [dict(r) | {"name": name(r["merchant"]), "color_slot": income_slots.get(r["merchant"])}
                           for r in by_source],
    }


# ---------- transactions ----------

@app.get("/api/transactions")
def transactions(month: str | None = None, category: str | None = None,
                 merchant: str | None = None, q: str | None = None,
                 id: list[str] | None = Query(None),
                 limit: int = 300, conn=Depends(get_conn)):
    where, params = [], []
    if id:  # these exact transactions, whatever their date or category
        where.append(f"id IN ({','.join('?' * len(id))})"); params += id
    if month:  # a month ("2026-10") or a whole year ("2026")
        clause, values = period_filter(month)
        where.append(clause); params += values
    if category:
        where.append("category = ?"); params.append(category)
    if merchant:
        where.append("merchant = ?"); params.append(merchant)
    if q:  # your own merchant names count too
        where.append("(description LIKE ? OR merchant LIKE ? OR merchant IN "
                     "(SELECT merchant FROM merchant_names WHERE name LIKE ?))"); params += [f"%{q}%"] * 3
    sql = f"SELECT * FROM ({TX_SELECT})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY posted DESC LIMIT ?"
    rows = conn.execute(sql, (*params, limit)).fetchall()
    name = merchant_namer(conn)
    return [dict(r) | {"date": date.fromtimestamp(r["posted"]).isoformat(),
                       "name": name(r["merchant"]), "bank_name": display_name(r["merchant"]),
                       "description_key": description_key(r["description"])} for r in rows]


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
        mark_reviewed(conn, [body.id])  # choosing a category is checking it
    return {"ok": True}


# ---------- review ----------
# New transactions wait on the Review page until you've checked their category,
# by confirming it or changing it.

REVIEW_RECENT_DAYS = 60


def mark_reviewed(conn, ids: list[str], reviewed: bool = True):
    now = int(time.time())
    conn.executemany("INSERT INTO reviewed (tx_id, reviewed, reviewed_at) "
                     "SELECT id, ?, ? FROM transactions WHERE id = ? "
                     "ON CONFLICT(tx_id) DO UPDATE SET reviewed = excluded.reviewed, reviewed_at = excluded.reviewed_at",
                     [(int(reviewed), now, i) for i in ids])


@app.get("/api/review")
def review_list(show: Literal["unchecked", "recent"] = "unchecked", limit: int = 200, conn=Depends(get_conn)):
    """Unchecked transactions (oldest first, so you work forward in time), or
    everything from the last 60 days, checked or not (newest first)."""
    if show == "unchecked":
        sql = f"SELECT * FROM ({TX_SELECT}) WHERE NOT reviewed ORDER BY posted, id LIMIT ?"
        params = (limit,)
    else:
        since = int(time.time()) - REVIEW_RECENT_DAYS * 86400
        sql = f"SELECT * FROM ({TX_SELECT}) WHERE posted >= ? ORDER BY posted DESC, id LIMIT ?"
        params = (since, limit)
    rows = conn.execute(sql, params).fetchall()
    remaining = conn.execute(f"SELECT COUNT(*) FROM ({TX_SELECT}) WHERE NOT reviewed").fetchone()[0]
    name = merchant_namer(conn)
    return {
        "remaining": remaining,
        "items": [dict(r) | {"date": date.fromtimestamp(r["posted"]).isoformat(),
                             "name": name(r["merchant"]), "bank_name": display_name(r["merchant"]),
                             "reviewed": bool(r["reviewed"]),
                             "description_key": description_key(r["description"])} for r in rows],
    }


class ReviewBody(BaseModel):
    ids: list[str] = []
    reviewed: bool = True
    all_unchecked: bool = False  # "Mark all as checked"


@app.post("/api/review")
def review_mark(body: ReviewBody, conn=Depends(get_conn)):
    """Mark transactions as checked, or back to unchecked."""
    with conn:
        if body.all_unchecked:
            mark_reviewed(conn, [r[0] for r in conn.execute(f"SELECT id FROM ({TX_SELECT}) WHERE NOT reviewed")])
        else:
            mark_reviewed(conn, body.ids, body.reviewed)
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


class TransactionRuleBody(BaseModel):
    id: str
    pattern: str | None = None  # None: the whole merchant


@app.post("/api/transaction-rule")
def make_transaction_rule(body: TransactionRuleBody, conn=Depends(get_conn)):
    """Make a rule from a purchase's category. The purchase then follows the rule,
    unless the rule doesn't reach it (a stronger description rule, or a pattern
    that isn't in its description): then it keeps the category you gave it."""
    tx_sql = f"SELECT merchant, category, default_category FROM ({TX_SELECT}) WHERE id = ?"
    tx = conn.execute(tx_sql, (body.id,)).fetchone()
    if not tx:
        fail(Exception("That transaction no longer exists."))
    if body.pattern is None:
        set_rule(RuleBody(merchant=tx["merchant"], category=tx["category"]), conn)
    else:
        set_description_rule(DescriptionRuleBody(merchant=tx["merchant"], pattern=body.pattern,
                                                 category=tx["category"]), conn)
    if conn.execute(tx_sql, (body.id,)).fetchone()["default_category"] == tx["category"]:
        reset_transaction_category(body.id, conn)
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
    name = merchant_namer(conn)
    combined: dict[str, list] = {}
    for r in conn.execute("SELECT alias, merchant FROM merchant_aliases ORDER BY alias"):
        combined.setdefault(r["merchant"], []).append({"merchant": r["alias"], "name": name(r["alias"])})
    return [dict(r) | {
        "name": name(r["merchant"]),
        "bank_name": display_name(r["merchant"]),  # what Penny would call it on its own
        "auto_category": auto[r["merchant"]][0],
        "last_date": date.fromtimestamp(r["last_posted"]).isoformat(),
        "combined": combined.get(r["merchant"], []),
    } for r in rows]


class MerchantNameBody(BaseModel):
    merchant: str
    name: str  # blank goes back to the name from the bank's wording


@app.put("/api/merchants/name")
def rename_merchant(body: MerchantNameBody, conn=Depends(get_conn)):
    """Give a merchant your own name, shown everywhere it appears. Syncs keep it."""
    name = " ".join(body.name.split())
    if len(name) > 60:
        fail(Exception("Keep merchant names to 60 characters or fewer."))
    with conn:
        if not name or name == display_name(body.merchant):
            conn.execute("DELETE FROM merchant_names WHERE merchant = ?", (body.merchant,))
        else:
            conn.execute("INSERT INTO merchant_names (merchant, name) VALUES (?, ?) "
                         "ON CONFLICT(merchant) DO UPDATE SET name = excluded.name", (body.merchant, name))
    return {"ok": True}


class CombineBody(BaseModel):
    merchant: str  # the one that stays
    others: list[str]


@app.post("/api/merchants/combine")
def combine_merchants(body: CombineBody, conn=Depends(get_conn)):
    """Count other merchants' transactions as `merchant` from now on.

    Rules, pictures and names the others had move over when `merchant` has none of
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
            conn.execute("INSERT OR IGNORE INTO merchant_names (merchant, name) "
                         "SELECT ?, name FROM merchant_names WHERE merchant = ?", (target, other))
            for table in ("merchant_rules", "description_rules", "merchant_logos", "merchant_names"):
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
    name = merchant_namer(conn)
    kinds = {r["key"]: r["kind"] for r in conn.execute("SELECT * FROM recurring_kinds")}
    found = subscriptions.detect(
        [dict(t) for t in txs], date.today(),
        names={m: name(m) for m in {t["merchant"] for t in txs}},
        overrides=overrides, kinds=kinds, bill_categories=bill_categories(conn))
    # A subscription's own name wins over its merchant's; `merchant_name` stays for showing both.
    custom = dict(conn.execute("SELECT key, name FROM recurring_names").fetchall())
    merchant_names = {s.key: s.name for s in found}
    for s in found:
        s.name = custom.get(s.key, s.name)
    found += [subscriptions.manual(dict(r), date.today()) for r in conn.execute("SELECT * FROM manual_recurring")]
    found.sort(key=lambda r: (not r.active, -r.monthly_cost))
    active = [s for s in found if s.active and not s.ignored]
    total = lambda items, field: round(sum(getattr(s, field) for s in items), 2)
    subs = [s for s in active if s.kind == "subscription"]
    bills = [s for s in active if s.kind == "bill"]
    return {
        "monthly_total": total(active, "monthly_cost"),
        "yearly_total": total(active, "yearly_cost"),
        "subscriptions_monthly": total(subs, "monthly_cost"),
        "bills_monthly": total(bills, "monthly_cost"),
        "items": [s.to_dict() | {"merchant_name": merchant_names.get(s.key)} for s in found],
    }


class ManualRecurringBody(BaseModel):
    name: str
    amount: float
    cadence: str
    next_date: date
    category: str
    kind: Literal["bill", "subscription"]


def _manual_values(body: ManualRecurringBody, conn) -> tuple:
    name = " ".join(body.name.split())
    if not 1 <= len(name) <= 60:
        fail(Exception("Give it a name of 1 to 60 characters."))
    if not 0 < body.amount < 1_000_000:
        fail(Exception("Enter the price of each charge, like 9.99."))
    if body.cadence not in subscriptions.CADENCE_NAMES:
        fail(Exception(f"Unknown schedule: {body.cadence}"))
    if body.category not in db.category_names(conn):
        fail(Exception(f"Unknown category: {body.category}"))
    if body.next_date.year < 1990:
        fail(Exception("Check the date of the next charge."))
    return name, round(body.amount, 2), body.cadence, body.next_date.isoformat(), body.category, body.kind


@app.post("/api/subscriptions/manual")
def add_manual_recurring(body: ManualRecurringBody, conn=Depends(get_conn)):
    """Add a subscription or bill yourself, for one Penny can't find on its own."""
    values = _manual_values(body, conn)
    with conn:
        cur = conn.execute("INSERT INTO manual_recurring (name, amount, cadence, start_date, category, kind) "
                           "VALUES (?, ?, ?, ?, ?, ?)", values)
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/subscriptions/manual/{id}")
def edit_manual_recurring(id: int, body: ManualRecurringBody, conn=Depends(get_conn)):
    values = _manual_values(body, conn)
    with conn:
        cur = conn.execute("UPDATE manual_recurring SET name = ?, amount = ?, cadence = ?, start_date = ?, "
                           "category = ?, kind = ? WHERE id = ?", (*values, id))
    if not cur.rowcount:
        fail(Exception("That one was already deleted."))
    return {"ok": True}


@app.delete("/api/subscriptions/manual/{id}")
def delete_manual_recurring(id: int, conn=Depends(get_conn)):
    with conn:
        conn.execute("DELETE FROM manual_recurring WHERE id = ?", (id,))
    return {"ok": True}


class RecurringNameBody(BaseModel):
    key: str
    name: str  # blank goes back to the merchant's name


@app.put("/api/subscriptions/name")
def rename_recurring(body: RecurringNameBody, conn=Depends(get_conn)):
    """Name one subscription or bill Penny found, without renaming its merchant
    (Apple can bill for several things)."""
    if body.key.startswith("manual:"):
        fail(Exception("Use Edit to rename one you added yourself."))
    name = " ".join(body.name.split())
    if len(name) > 60:
        fail(Exception("Keep names to 60 characters or fewer."))
    with conn:
        if name:
            conn.execute("INSERT INTO recurring_names (key, name) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET name = excluded.name", (body.key, name))
        else:
            conn.execute("DELETE FROM recurring_names WHERE key = ?", (body.key,))
    return {"ok": True}


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
    name = merchant_namer(conn)
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
                        "amount": t["amount"], "category": category, "name": name(merchant)})
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
        card_money_in(conn)
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
