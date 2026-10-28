"""Generate a year of realistic fake data so the app can be tried without a bank.

Data is shaped like SimpleFIN responses and loaded through the same upsert
functions a real sync uses.
"""
import random
from datetime import date, datetime, time as dtime, timedelta

from . import db
from .sync import snapshot_balances, upsert_account, upsert_transaction

DAYS = 400


def _ts(d: date) -> int:
    return int(datetime.combine(d, dtime(12, 0)).timestamp())


def generate(today: date | None = None, seed: int = 7):
    today = today or date.today()
    rng = random.Random(seed)
    start = today - timedelta(days=DAYS)
    txs = {"chk": [], "sav": [], "card": [], "inv": []}

    def add(acct, d, amount, desc):
        if start <= d <= today:
            txs[acct].append({"id": f"{acct}-{len(txs[acct])}", "posted": _ts(d),
                              "amount": f"{amount:.2f}", "description": desc})

    def monthly(day_of_month):
        d = date(start.year, start.month, 1)
        while d <= today:
            yield d.replace(day=min(day_of_month, 28))
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Income: every other Friday
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    while d <= today:
        add("chk", d, 2150.00, "ACME CORP PAYROLL PPD ID 8834")
        d += timedelta(days=14)

    for m in monthly(1):
        add("chk", m, -1450.00, "JUNIPER APARTMENTS RENT WEB PMT")
        add("chk", m + timedelta(days=2), -300.00, "TRANSFER TO SAVINGS 4471")
        add("sav", m + timedelta(days=2), 300.00, "TRANSFER FROM CHECKING 9921")
        add("sav", m + timedelta(days=27), round(rng.uniform(18, 26), 2), "INTEREST PAID")
    for m in monthly(12):
        add("chk", m, -round(rng.uniform(85, 150), 2), "RIVERBEND ELECTRIC CO WEB PMT")
        add("chk", m + timedelta(days=3), -75.00, "XFINITY INTERNET")
        add("chk", m + timedelta(days=6), -112.40, "GEICO INSURANCE PPD")

    # Subscriptions on the credit card
    for m in monthly(9):
        months_ago = (today.year - m.year) * 12 + today.month - m.month
        add("card", m, -(17.99 if months_ago < 3 else 15.49), "DISNEY PLUS 888-905-7888 CA")
        add("card", m + timedelta(days=5), -11.99, "PEACOCK TV 888-288-4554")
        add("card", m + timedelta(days=11), -2.99, "DROPBOX*PLUS 855-237-6726")
        add("card", m + timedelta(days=14), -40.00, "EVERGREEN FITNESS CLUB FEES")
        add("card", m + timedelta(days=17), -20.00, "ADOBE *CREATIVE CLOUD")
        if months_ago >= 4:  # cancelled four months ago
            add("card", m + timedelta(days=3), -7.99, "HULU 877-8244858 CA")
    for days_ago in (370, 5):
        add("card", today - timedelta(days=days_ago), -139.00, "NYTIMES*DIGITAL YEARLY")

    # Everyday spending on the card
    groceries = ["KROGER #0123", "WHOLE FOODS MKT #10293", "TRADER JOE'S #551", "ALDI 74012"]
    dining = ["STARBUCKS STORE 08123", "SQ *BRAMBLE PIZZA", "GRUBHUB*LOTUS THAI",
              "TST* MEADOWBROOK CAFE", "CHIPOTLE 2291", "SQ *LARKSPUR COFFEE"]
    shopping = ["ETSY.COM BROOKVALE NY", "OLD NAVY #2291", "IKEA #0041",
                "NORDSTROM #8802", "LANTERN HARDWARE 00011"]
    d = start
    while d <= today:
        if d.weekday() in (2, 6):
            add("card", d, -round(rng.uniform(45, 190), 2), rng.choice(groceries))
        for _ in range(rng.choice([0, 0, 1, 1, 2])):
            add("card", d, -round(rng.uniform(6, 55), 2), rng.choice(dining))
        if rng.random() < 0.11:
            add("card", d, -round(rng.uniform(45, 75), 2), "VALERO 2345 SPRINGFIELD IL")
        if rng.random() < 0.08:
            add("card", d, -round(rng.uniform(15, 220), 2), rng.choice(shopping))
        if rng.random() < 0.015:
            add("card", d, -round(rng.uniform(120, 480), 2), "SOUTHWEST AIR 5262154")
        d += timedelta(days=1)

    # Pay off the card each month for what was charged the month before
    for m in monthly(20):
        prev_start = (m.replace(day=1) - timedelta(days=1)).replace(day=1)
        owed = -sum(float(t["amount"]) for t in txs["card"]
                    if prev_start <= date.fromtimestamp(t["posted"]) < m.replace(day=1)
                    and float(t["amount"]) < 0)
        if owed > 0:
            add("chk", m, -owed, "ONLINE PAYMENT TO VISA 4410")
            add("card", m, owed, "PAYMENT THANK YOU")

    # Retirement contributions and growth (no spending, just balance history)
    for m in monthly(15):
        add("inv", m, 250.00, "CONTRIBUTION 401K")

    current = {"chk": 3184.22, "sav": 12650.00, "card": -846.31, "inv": 41210.55}
    accounts = [
        ("chk", "Northern Credit Union", "Everyday Checking"),
        ("sav", "Northern Credit Union", "High-Yield Savings"),
        ("card", "Aurora Bank", "Rewards Visa"),
        ("inv", "Summit Investments", "401(k)"),
    ]
    return accounts, txs, current


def load(conn, today: date | None = None):
    today = today or date.today()
    accounts, txs, current = generate(today)
    with conn:
        db.clear_financial_data(conn)
        for key, org, name in accounts:
            acct_id = f"demo-{key}"
            upsert_account(conn, {"id": acct_id, "org": {"name": org}, "name": name,
                                  "currency": "USD", "balance": str(current[key]),
                                  "balance-date": _ts(today)})
            for tx in txs[key]:
                upsert_transaction(conn, acct_id, tx)

        # Weekly balance history, reconstructed by walking transactions backward
        # from today's balance. The 401(k) also gets a smooth market-growth curve.
        d = today
        while d >= today - timedelta(days=DAYS):
            for key, _, _ in accounts:
                later = sum(float(t["amount"]) for t in txs[key] if date.fromtimestamp(t["posted"]) > d)
                bal = current[key] - later
                if key == "inv":
                    bal *= 0.92 + 0.08 * (1 - (today - d).days / DAYS)
                conn.execute(
                    "INSERT OR REPLACE INTO balance_snapshots (account_id, day, balance) VALUES (?, ?, ?)",
                    (f"demo-{key}", d.isoformat(), round(bal, 2)),
                )
            d -= timedelta(days=7)
        snapshot_balances(conn, today.isoformat())
        db.set_setting(conn, "mode", "demo")
        db.set_setting(conn, "last_sync", _ts(today))
