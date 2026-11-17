from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8000")


def new_account(client, name, rows):
    """Import made-up transactions into a new account; returns its id."""
    text = "Date,Description,Amount\n" + "".join(f"{d:%m/%d/%Y},{desc},{amt}\n" for d, desc, amt in rows)
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    mapping["flip"] = False  # amounts here are already signed; don't let a one-row file look like a card export
    r = client.post("/api/import", json={"csv": text, "mapping": mapping,
                                         "new_account": {"name": name, "balance": 1000}})
    return r.json()["account_id"]


def categories_by_desc(client):
    return {t["description"]: t["category"] for t in client.get("/api/transactions").json()}


this_month = date.today().replace(day=1)


def test_transfer_between_own_accounts_is_neither_spending_nor_income(client):
    # Wording no keyword knows: only the matching amounts give it away.
    new_account(client, "Savings", [(this_month, "MOVE FUNDS REF 8812", -5000.00)])
    new_account(client, "Checking", [(this_month + timedelta(days=1), "INCOMING REF 8812", 5000.00),
                                     (this_month, "PINECREST GROCERY", -40.00)])
    cats = categories_by_desc(client)
    assert cats["MOVE FUNDS REF 8812"] == "Transfers" and cats["INCOMING REF 8812"] == "Transfers"

    month = client.get("/api/spending/monthly").json()[-1]
    assert month["spending"] == 40.00 and month["income"] == 0


def test_purchase_is_not_mistaken_for_a_transfer(client):
    new_account(client, "Card", [(this_month, "PIZZA PALACE", -20.00)])  # Dining by keyword
    new_account(client, "Checking", [(this_month, "PAYMENT FROM FRIEND", 20.00)])
    assert categories_by_desc(client)["PIZZA PALACE"] == "Dining"


def test_same_account_and_far_apart_amounts_do_not_pair(client):
    new_account(client, "Checking", [(this_month, "CASH WITHDRAWAL", -300.00),
                                     (this_month, "CASH DEPOSIT", 300.00)])
    new_account(client, "Savings", [(this_month + timedelta(days=20), "SOMETHING ELSE", 300.00)])
    assert "Transfers" not in categories_by_desc(client).values()


def test_credit_union_share_wording(client):
    new_account(client, "Savings", [(this_month, "To Share 70", -100.00)])
    assert categories_by_desc(client)["To Share 70"] == "Transfers"


def test_merchants_page_does_not_count_transfers_as_spent(client):
    new_account(client, "Savings", [(this_month, "MOVE FUNDS REF 8812", -5000.00)])
    new_account(client, "Checking", [(this_month, "INCOMING REF 8812", 5000.00)])
    spent = {m["example"]: m["spent"] for m in client.get("/api/merchants").json()}
    assert spent["MOVE FUNDS REF 8812"] == 0


def test_yearly_totals_match_each_year(client):
    new_account(client, "Checking", [(date(2024, 3, 5), "PAYROLL ACME", 1000.00), (date(2024, 3, 6), "PINECREST GROCERY", -40.00),
                                     (date(2025, 7, 1), "PAYROLL ACME", 1200.00), (date(2025, 7, 2), "PINECREST GROCERY", -60.00)])
    years = client.get("/api/spending/yearly").json()
    assert years == [{"year": "2024", "spending": 40.0, "income": 1000.0},
                     {"year": "2025", "spending": 60.0, "income": 1200.0}]


def test_investing_is_neither_spending_nor_income(client):
    new_account(client, "Savings", [(this_month, "Withdrawal ACH VANGUARD BUY INVESTMENT CO: VANGUARD", -6000.00),
                                    (this_month, "Withdrawal ACH BETTERMENT TYPE: DEPOSIT CO: BETTERMENT", -100.00),
                                    (this_month, "Deposit ACH WEALTHFRONT TYPE: CREDITS CO: WEALTHFRONT", 100.00),
                                    (this_month, "PINECREST GROCERY", -40.00)])
    cats = categories_by_desc(client)
    assert {cats[d] for d in cats if "GROCERY" not in d} == {"Investments"}
    month = client.get("/api/spending/monthly").json()[-1]
    assert month["spending"] == 40.00 and month["income"] == 0


def test_card_payment_pairs_as_a_card_payment(client):
    # Only the checking side names the card; the card side's wording means nothing on its own.
    new_account(client, "Checking", [(this_month, "Withdrawal ACH CAPITAL ONE TYPE: PAYMENT", -2480.00)])
    new_account(client, "Card", [(this_month, "ONLINE/MOBILE RECURRING", 2480.00)])
    cats = categories_by_desc(client)
    assert cats["ONLINE/MOBILE RECURRING"] == "Credit Card Payments"
    month = client.get("/api/spending/monthly").json()[-1]
    assert month["spending"] == 0 and month["income"] == 0


def test_money_arriving_on_a_card_is_never_income(client):
    def card(rows):
        text = "Date,Description,Amount\n" + "".join(f"{d:%m/%d/%Y},{desc},{amt}\n" for d, desc, amt in rows)
        mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
        mapping["flip"] = False
        client.post("/api/import", json={"csv": text, "mapping": mapping,
                                         "new_account": {"name": "Visa", "balance": -2500}})
    card([(this_month, "PENDING PAYMENT", 1890.00),           # a bank's pending card payment
          (this_month, "FERRY TICKETS REFUND", 74.25),   # a refund
          (this_month, "FERRY TICKETS", -150.00)])
    client.put("/api/merchant-rules", json={"merchant": "ferry tickets", "category": "Travel"})
    cats = categories_by_desc(client)
    assert cats["PENDING PAYMENT"] == "Credit Card Payments"
    assert cats["FERRY TICKETS REFUND"] != "Income"
    month = client.get("/api/spending/monthly").json()[-1]
    assert month["income"] == 0


def test_bank_of_america_autopay_on_the_card_is_a_card_payment(client):
    """Some banks' autopay arrives as "ONLINE/MOBILE RECURRING" with no payment word,
    often days before the checking side shows up to pair it with."""
    text = "Date,Description,Amount\n" + f"{this_month:%m/%d/%Y},ONLINE/MOBILE RECURRING,1890.00\n" \
           + f"{this_month:%m/%d/%Y},PINECREST GROCERY,-80.00\n"
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    mapping["flip"] = False
    client.post("/api/import", json={"csv": text, "mapping": mapping,
                                     "new_account": {"name": "Visa", "balance": -2500}})
    assert categories_by_desc(client)["ONLINE/MOBILE RECURRING"] == "Credit Card Payments"
    month = client.get("/api/spending/monthly").json()[-1]
    assert month["spending"] == 80.00


def test_venmo_is_income_until_marked_a_reimbursement(client):
    """Venmo money counts as income by default. Given the purchase's category, it
    lowers that spending; as a Reimbursement, it's neither income nor spending."""
    new_account(client, "Checking", [(this_month, "PINECREST GROCERY", -100.00),
                                     (this_month + timedelta(days=1),
                                      "Deposit ACH VENMO TYPE: CASHOUT CO: VENMOACH Trace 091000010137860", 50.00)])
    period = f"{this_month:%Y-%m}"
    b = client.get("/api/spending/breakdown", params={"period": period}).json()
    assert (b["income"], b["total"]) == (50.00, 100.00)

    venmo = next(t for t in client.get("/api/transactions").json() if "VENMO" in t["description"])
    assert venmo["category"] == "Income"
    client.put("/api/transaction-category", json={"id": venmo["id"], "category": "Reimbursements"})
    b = client.get("/api/spending/breakdown", params={"period": period}).json()
    assert (b["income"], b["total"], b["income_sources"]) == (0, 100.00, [])
    client.put("/api/transaction-category", json={"id": venmo["id"], "category": "Groceries"})
    b = client.get("/api/spending/breakdown", params={"period": period}).json()
    assert (b["income"], b["total"]) == (0, 50.00)
    assert client.delete("/api/categories", params={"name": "Reimbursements"}).status_code == 400


def test_chart_and_breakdown_always_agree(client):
    """Money back that outweighs a category's spending lowers both totals the same way."""
    new_account(client, "Checking", [(this_month, "PINECREST GROCERY", -100.00),
                                     (this_month, "FERRY TICKETS", -50.00),
                                     (this_month, "FERRY TICKETS REFUND", 200.00)])
    client.put("/api/merchant-rules", json={"merchant": "ferry tickets", "category": "Travel"})
    period = f"{this_month:%Y-%m}"
    chart = [m for m in client.get("/api/spending/monthly").json() if m["month"] == period][0]["spending"]
    b = client.get("/api/spending/breakdown", params={"period": period}).json()
    assert b["total"] == chart
    assert [c["category"] for c in b["credits"]] == ["Travel"]


def test_card_payment_posted_days_apart_does_not_bump_net_worth(client):
    """The card shows the payment on day 1, checking takes it out on day 3. Net
    worth must not count that money twice in between."""
    import time
    from app import db
    today = date.today()
    day = lambda n: (today - timedelta(days=n)).isoformat()
    noon = lambda n: int(time.mktime((today - timedelta(days=n)).timetuple())) + 12 * 3600
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO accounts (id, org_name, name, balance) VALUES ('chk', 'Westfield Credit Union', 'Checking', 1000)")
        conn.execute("INSERT INTO accounts (id, org_name, name, balance) VALUES ('card', 'Summit Bank', 'Visa', 0)")
        for tid, acct, n, amt in (("pay-card", "card", 4, 3000), ("pay-chk", "chk", 2, -3000)):
            conn.execute("INSERT INTO transactions (id, account_id, posted, amount, description, merchant, category) "
                         "VALUES (?, ?, ?, ?, 'PAYMENT', 'payment', 'Credit Card Payments')", (tid, acct, noon(n), amt))
        # A bank's balance shows a transaction the day after its date.
        for n, chk, card in ((5, 4000, -3000), (4, 4000, -3000), (3, 4000, 0), (2, 4000, 0), (1, 1000, 0), (0, 1000, 0)):
            conn.executemany("INSERT INTO balance_snapshots (account_id, day, balance) VALUES (?, ?, ?)",
                             [("chk", day(n), chk), ("card", day(n), card)])
    history = client.get("/api/accounts").json()["history"]
    assert {h["day"]: h["net_worth"] for h in history if h["day"] >= day(5)} == {day(n): 1000 for n in range(6)}


def test_transfer_from_an_app_is_not_your_own_transfer(client):
    """"Transfer from Venmo" and "Transfer from Account 10" are different merchants,
    so naming one doesn't rename the other."""
    from app.categorize import normalize_merchant
    assert normalize_merchant("DEPOSIT ACH VENMO", "Transfer from Venmo") == "venmo"
    assert normalize_merchant("From Share 10", "Transfer from Account 10") == "transfer from"
    assert normalize_merchant("To Share 70", "Transfer to Share 70") == "transfer to"


def test_older_shared_merchant_keys_are_split(client):
    from app import db, sync
    client.get("/api/status")
    conn = db.connect()
    with conn:
        conn.execute("INSERT INTO accounts (id, org_name, name, balance) VALUES ('chk', 'Westfield Credit Union', 'Checking', 0)")
        for tid, desc, payee in (("v", "DEPOSIT ACH VENMO CASHOUT", "Transfer from Venmo"),
                                 ("s", "From Share 10", "Transfer from Account 10")):
            conn.execute("INSERT INTO transactions (id, account_id, posted, amount, description, payee, merchant, category) "
                         "VALUES (?, 'chk', 1, 50, ?, ?, 'transfer from', 'Other')", (tid, desc, payee))
    sync.rekey_shared_merchants(conn)
    keys = dict(conn.execute("SELECT id, merchant FROM transactions").fetchall())
    assert keys == {"v": "venmo", "s": "transfer from"}


def test_rules_page_lists_every_rule_and_flags_mixed_merchants(client):
    new_account(client, "Savings", [(this_month, "MOVE FUNDS REF 8812", -5000.00)])
    new_account(client, "Checking", [(this_month + timedelta(days=1), "INCOMING REF 8812", 5000.00),
                                     (this_month, "INCOMING VENMO CASHOUT", 40.00),
                                     (this_month, "PINECREST GROCERY", -40.00)])
    by_desc = {t["description"]: t for t in client.get("/api/transactions").json()}
    # Put a transfer and a Venmo payment under one name, like two merchants mixed up.
    client.post("/api/merchants/combine", json={"merchant": by_desc["INCOMING VENMO CASHOUT"]["merchant"],
                                                "others": [by_desc["INCOMING REF 8812"]["merchant"]]})
    venmo = by_desc["INCOMING VENMO CASHOUT"]["merchant"]
    client.put("/api/merchants/name", json={"merchant": venmo, "name": "Venmo"})
    client.put("/api/merchant-rules", json={"merchant": "pinecrest grocery", "category": "Groceries"})
    client.put("/api/transaction-category", json={"id": by_desc["PINECREST GROCERY"]["id"], "category": "Dining"})

    r = client.get("/api/rules").json()
    [name] = r["names"]
    assert (name["rule_name"], name["count"], name["mixed"]) == ("Venmo", 2, True)
    assert {c["category"] for c in name["auto_categories"]} == {"Transfers", "Income"}
    [rule] = r["merchant_categories"]
    assert (rule["rule_category"], rule["mixed"]) == ("Groceries", False)
    assert len(r["combined"]) == 1 and r["combined"][0]["mixed"]
    [one] = r["transaction_categories"]
    assert (one["category"], one["default_category"]) == ("Dining", "Groceries")


def test_reimbursements_to_sort_and_splitting_one(client):
    """A $1,000 Venmo cash-out: $800 paid back rent, $200 was pay."""
    new_account(client, "Checking", [(this_month, "JUNIPER APARTMENTS RENT", -1600.00),
                                     (this_month + timedelta(days=2), "Deposit ACH VENMO TYPE: CASHOUT CO: VENMO", 1000.00),
                                     (this_month + timedelta(days=3), "Deposit ACH VENMO TYPE: CASHOUT CO: VENMO REF 2", 40.00)])
    period = f"{this_month:%Y-%m}"
    totals = lambda: (lambda b: (b["income"], b["total"]))(client.get("/api/spending/breakdown", params={"period": period}).json())
    r = client.get("/api/reimbursements").json()
    assert [t["amount"] for t in r["items"]] == [40.00, 1000.00] and r["total"] == 1040.00
    big = r["items"][1]
    assert big["hints"][0]["amount"] == -1600.00  # the rent, to jog your memory
    assert totals() == (1040.00, 1600.00)  # income until sorted

    # Parts must add up.
    bad = client.put("/api/transaction-split", json={"id": big["id"], "parts": [
        {"amount": 800, "category": "Housing"}, {"amount": 150, "category": "Income"}]})
    assert bad.status_code == 400 and "Add $50.00" in bad.json()["detail"]
    assert client.put("/api/transaction-split", json={"id": big["id"], "parts": [
        {"amount": 1000, "category": "Housing"}]}).status_code == 400
    assert client.put("/api/transaction-split", json={"id": big["id"], "parts": [
        {"amount": 800, "category": "Housing"}, {"amount": 200, "category": "Income"}]}).status_code == 200
    assert totals() == (240.00, 800.00)  # rent's back half lowers Housing; the rest is pay
    # Both parts show, and it's sorted and checked.
    parts = [t for t in client.get("/api/transactions").json() if t["tx_id"] == big["id"]]
    assert sorted((t["amount"], t["category"]) for t in parts) == [(200.0, "Income"), (800.0, "Housing")]
    assert all(t["reviewed"] for t in parts)
    assert [t["amount"] for t in client.get("/api/reimbursements").json()["items"]] == [40.00]
    assert client.put("/api/transaction-category", json={"id": parts[0]["id"], "category": "Dining"}).status_code == 400
    [split] = client.get("/api/rules").json()["splits"]
    assert len(split["parts"]) == 2

    # The small one was dinner; renaming Housing reaches the split too.
    small = client.get("/api/reimbursements").json()["items"][0]
    client.put("/api/transaction-category", json={"id": small["id"], "category": "Dining"})
    assert client.get("/api/reimbursements").json()["items"] == []
    client.put("/api/categories", json={"name": "Housing", "new_name": "Home"})
    assert {t["category"] for t in client.get("/api/transactions").json() if t["tx_id"] == big["id"]} == {"Home", "Income"}

    # Undo puts it back together.
    client.delete("/api/transaction-split", params={"id": big["id"]})
    assert totals() == (1000.00, 1560.00)
    assert [t["amount"] for t in client.get("/api/reimbursements").json()["items"]] == [1000.00]
