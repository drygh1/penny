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
