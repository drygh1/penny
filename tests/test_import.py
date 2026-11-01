import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import importer

# Layouts modeled on common bank exports.
SINGLE_AMOUNT = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
01/14/2025,01/15/2025,HULU.COM,Entertainment,Sale,-15.49,
01/12/2025,01/13/2025,KROGER #4410,Groceries,Sale,-82.10,
01/10/2025,01/10/2025,Payment Thank You-Mobile,,Payment,500.00,
"""

DEBIT_CREDIT = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2025-02-03,2025-02-04,1234,STARBUCKS STORE 08123,Dining,6.45,
2025-02-01,2025-02-02,1234,CAPITAL ONE AUTOPAY PYMT,Payment/Credit,,250.00
"""

PREAMBLE_AND_PARENS = """Account Name: Everyday Checking
Account Number: XXXX4471
Date Range: 01/01/2024 - 03/31/2024

Date,Description,Amount,Balance
03/15/2024,"ACME CORP PAYROLL PPD","$2,150.00","$5,200.00"
03/16/2024,"SUNRISE APARTMENTS RENT","($1,450.00)","$3,750.00"
"""

POSITIVE_PURCHASES = """Trans. Date,Post Date,Description,Amount,Category
03/01/2025,03/02/2025,DROPBOX PLUS,11.99,Services
03/04/2025,03/05/2025,WHOLE FOODS #0123,64.20,Supermarkets
03/09/2025,03/10/2025,VALERO 1234,48.00,Gasoline
03/11/2025,03/11/2025,INTERNET PAYMENT - THANK YOU,-200.00,Payments and Credits
"""

NO_HEADER = """01/05/2025,-4.50,LARKSPUR COFFEE
01/06/2025,-61.03,ALDI #0098
"""


def run(text):
    headers, rows = importer.parse_table(text)
    mapping = importer.guess_mapping(headers, rows)
    txs, errors, _ = importer.interpret(headers, rows, mapping)
    return mapping, txs, errors


def test_single_amount_column_uses_posted_date():
    m, txs, errors = run(SINGLE_AMOUNT)
    assert not errors and not m["flip"]
    assert txs[0] == {"date": date(2025, 1, 15), "amount": -15.49, "description": "HULU.COM"}


def test_debit_credit_columns():
    m, txs, _ = run(DEBIT_CREDIT)
    assert m["amount"] is None and m["debit"] is not None and m["credit"] is not None
    assert [t["amount"] for t in txs] == [-6.45, 250.00]


def test_preamble_rows_dollar_signs_and_parentheses():
    _, txs, _ = run(PREAMBLE_AND_PARENS)
    assert [t["amount"] for t in txs] == [2150.00, -1450.00]
    assert txs[1]["description"] == "SUNRISE APARTMENTS RENT"


def test_card_with_positive_purchases_gets_flipped():
    m, txs, _ = run(POSITIVE_PURCHASES)
    assert m["flip"]
    assert [t["amount"] for t in txs] == [-11.99, -64.20, -48.00, 200.00]


def test_file_without_headers():
    _, txs, _ = run(NO_HEADER)
    assert txs[1] == {"date": date(2025, 1, 6), "amount": -61.03, "description": "ALDI #0098"}


def test_unreadable_rows_are_reported_not_fatal():
    _, txs, errors = run(SINGLE_AMOUNT + "not a date,,GARBAGE,,,abc,\n")
    assert len(txs) == 3 and len(errors) == 1


# ---------- through the API ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8000")


def csv_for(rows):
    return "Date,Description,Amount\n" + "".join(f"{d:%m/%d/%Y},{desc},{amt}\n" for d, desc, amt in rows)


def test_import_into_new_account_and_reimport_is_idempotent(client):
    text = csv_for([(date(2025, 1, 5), "LARKSPUR COFFEE", -4.50), (date(2025, 1, 5), "LARKSPUR COFFEE", -4.50),
                    (date(2025, 1, 6), "ALDI", -61.03)])
    preview = client.post("/api/import/preview", json={"csv": text}).json()
    assert preview["count"] == 3 and preview["preview"][2]["category"] == "Groceries"

    body = {"csv": text, "mapping": preview["mapping"], "new_account": {"name": "Old Checking", "balance": 1000}}
    r = client.post("/api/import", json=body).json()
    assert r["imported"] == 3  # both identical coffees are kept
    account = r["account_id"]

    again = client.post("/api/import", json={"csv": text, "mapping": preview["mapping"], "account_id": account}).json()
    assert again == {"imported": 0, "skipped": 3, "unreadable": 0, "account_id": account}
    assert client.get("/api/status").json()["mode"] == "manual"

    client.delete("/api/import", params={"account_id": account})
    assert client.get("/api/accounts").json()["accounts"] == []


def test_overlap_with_synced_transactions_is_skipped(client):
    client.post("/api/demo")
    # Pick three real demo card transactions and re-export them with dates shifted a day.
    txs = client.get("/api/transactions", params={"limit": 3}).json()
    txs = [t for t in txs if t["account_id"] == "demo-card"] or pytest.skip("no card txs")
    rows = [(date.fromisoformat(t["date"]) + timedelta(days=1), t["description"], t["amount"]) for t in txs]
    rows.append((date(2020, 6, 1), "OLD PURCHASE", -12.00))  # older than anything synced
    text = csv_for(rows)
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    r = client.post("/api/import", json={"csv": text, "mapping": mapping, "account_id": "demo-card"}).json()
    assert r["imported"] == 1 and r["skipped"] == len(txs)


def test_net_worth_history_extends_back_through_imports(client):
    text = csv_for([(date.today() - timedelta(days=200), "PAYCHECK PAYROLL", 2000),
                    (date.today() - timedelta(days=100), "RENT", -1500)])
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    client.post("/api/import", json={"csv": text, "mapping": mapping,
                                     "new_account": {"name": "Checking", "balance": 3000}})
    history = client.get("/api/accounts").json()["history"]
    assert history[0]["net_worth"] == 2500  # 3000 today, minus the paycheck, plus the rent
    assert history[-1]["net_worth"] == 3000
    assert len(history) > 25  # weekly points over ~200 days


def test_bank_of_america_checking_layout():
    text = """Description,,Summary Amt.
Beginning balance as of 07/01/2025,,"2,104.33"
Ending balance as of 09/30/2025,,"2,492.15"

Date,Description,Amount,Running Bal.
07/01/2025,Beginning balance as of 07/01/2025,,"2,104.33"
07/02/2025,"ZELLE PAYMENT TO JOHN CONF# abc123","-40.00","2,064.33"
"""
    _, txs, errors = run(text)
    assert errors == [] and [t["amount"] for t in txs] == [-40.00]


def test_money_into_a_brokerage_without_transactions_does_not_vanish(client):
    from app import db
    text = csv_for([(date.today() - timedelta(days=200), "PAYCHECK PAYROLL", 2000),
                    (date.today() - timedelta(days=100), "Withdrawal ACH VANGUARD BUY INVESTMENT", -6000)])
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    client.post("/api/import", json={"csv": text, "mapping": mapping,
                                     "new_account": {"name": "Savings", "balance": 3000}})
    # A brokerage reports today's balance but none of its own transactions.
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO accounts (id, org_name, name, balance) VALUES ('ira', 'Vanguard', 'Retirement Account', 8000)")
        conn.execute("INSERT INTO balance_snapshots (account_id, day, balance) VALUES ('ira', ?, 8000)",
                     (date.today().isoformat(),))
    history = client.get("/api/accounts").json()["history"]
    # Before the deposit: 9000 in savings, 2000 at the brokerage. After: 3000 and 8000.
    between = lambda a, b: [h["net_worth"] for h in history
                            if (date.today() - timedelta(days=a)).isoformat() <= h["day"] < (date.today() - timedelta(days=b)).isoformat()]
    assert set(between(193, 100)) == {11000}  # after the paycheck, before the deposit
    assert set(between(100, 0)) == {11000}    # the deposit never shows as a drop
    assert history[0]["net_worth"] == 9000    # before the paycheck


def test_renamed_account_keeps_its_name_through_syncs(client):
    from app import db, sync
    text = csv_for([(date.today() - timedelta(days=3), "PINECREST GROCERY", -40)])
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    account_id = client.post("/api/import", json={"csv": text, "mapping": mapping,
                                                  "new_account": {"name": "Main Checking (-0042)", "balance": 500}}).json()["account_id"]
    assert client.put("/api/accounts/name", json={"id": account_id, "name": "  Everyday   checking "}).status_code == 200

    names = lambda: {a["id"]: a for a in client.get("/api/accounts").json()["accounts"]}
    acc = names()[account_id]
    assert acc["name"] == "Everyday checking" and acc["bank_name"] == "Main Checking (-0042)"
    assert client.get("/api/transactions").json()[0]["account_name"] == "Everyday checking"

    # A sync updates the bank's name but not yours.
    conn = db.connect()
    with conn:
        sync.upsert_account(conn, {"id": account_id, "name": "Latitude Checking", "currency": "USD",
                                   "balance": "500", "balance-date": 0, "org": {"name": "Westfield Credit Union"}})
    assert names()[account_id]["name"] == "Everyday checking"
    assert names()[account_id]["bank_name"] == "Latitude Checking"

    client.put("/api/accounts/name", json={"id": account_id, "name": ""})
    assert names()[account_id]["name"] == "Latitude Checking"
    assert client.put("/api/accounts/name", json={"id": "nope", "name": "X"}).status_code == 400


def test_brokerage_with_a_few_recent_transactions_still_counts_back(client):
    """A brokerage started reporting a 1¢ dividend and its reinvestment. That's not
    the account's history, so the deposit from savings must still carry it back."""
    from app import db
    text = csv_for([(date.today() - timedelta(days=200), "PAYCHECK PAYROLL", 2000),
                    (date.today() - timedelta(days=100), "Withdrawal ACH VANGUARD BUY INVESTMENT", -6000)])
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    client.post("/api/import", json={"csv": text, "mapping": mapping,
                                     "new_account": {"name": "Savings", "balance": 3000}})
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO accounts (id, org_name, name, balance) VALUES ('ira', 'Vanguard', 'Retirement Account', 8000)")
        conn.execute("INSERT INTO balance_snapshots (account_id, day, balance) VALUES ('ira', ?, 8000)",
                     ((date.today() - timedelta(days=2)).isoformat(),))
        for amt, desc in ((0.01, "DIVIDEND RECEIVED"), (-0.01, "REINVESTMENT")):
            conn.execute("INSERT INTO transactions (id, account_id, posted, amount, description, merchant, category) "
                         "VALUES (?, 'ira', ?, ?, ?, 'vanguard', 'Investments')",
                         (f"ira-{desc}", int(time.time()) - 86400, amt, desc))
    steps = [abs(b["net_worth"] - a["net_worth"]) for a, b in
             zip(client.get("/api/accounts").json()["history"], client.get("/api/accounts").json()["history"][1:])]
    assert max(steps) < 2500  # only the paycheck moves it; the deposit and the first IRA balance don't


def test_account_whose_history_starts_later_does_not_jump(client):
    """An account's balance before its first transaction is unknown, not $0."""
    early = csv_for([(date.today() - timedelta(days=300), "PINECREST GROCERY", -20)])
    late = csv_for([(date.today() - timedelta(days=60), "PINECREST GROCERY", -20)])
    for name, text, balance in (("Checking", early, 1000), ("Savings", late, 7000)):
        mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
        client.post("/api/import", json={"csv": text, "mapping": mapping,
                                         "new_account": {"name": name, "balance": balance}})
    history = client.get("/api/accounts").json()["history"]
    steps = [abs(b["net_worth"] - a["net_worth"]) for a, b in zip(history, history[1:])]
    assert max(steps) <= 20.01 and history[0]["net_worth"] == 8040
