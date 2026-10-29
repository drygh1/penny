from datetime import date, datetime, timedelta

from app.categorize import categorize, normalize_merchant
from app.subscriptions import detect


def tx(merchant, d: date, amount, category="Subscriptions"):
    return {"merchant": merchant, "posted": int(datetime(d.year, d.month, d.day, 12).timestamp()),
            "amount": amount, "category": category}


TODAY = date(2026, 10, 28)


def test_normalize_merchant():
    assert normalize_merchant("HULU.COM 877-824-4858 CA") == "hulu"
    assert normalize_merchant("SQ *LARKSPUR COFFEE 0423") == "larkspur coffee"
    assert normalize_merchant("PRIME VIDEO*2K4L32 AMZN.COM/BILL WA") == "prime video"


def test_categorize():
    assert categorize("HULU.COM", -15.49) == "Subscriptions"
    assert categorize("ONLINE PAYMENT TO SAVINGS", -500) == "Transfers"
    assert categorize("ONLINE PAYMENT TO VISA", -500) == "Credit Card Payments"
    assert categorize("ACME CORP PAYROLL", 2000) == "Income"
    assert categorize("CURRENT BANK MISC", -10) == "Other"  # "rent" inside "current" must not match


def test_credit_card_payments():
    # Sent from checking: an issuer and a payment word.
    for d in ("Withdrawal ACH CAPITAL ONE TYPE: ONLINE PMT CO: CAPITAL ONE",
              "Withdrawal ACH CITI CARD TYPE: E-PAYMENT DATA: CC PYMNTS",
              "Withdrawal ACH BARCLAYCARD EPAYMENT TYPE: ACH PMT"):
        assert categorize(d, -250) == "Credit Card Payments", d
    # Arriving on the card.
    assert categorize("ELECTRONIC PAYMENT RECEIVED", 250) == "Credit Card Payments"
    assert categorize("PAYMENT THANK YOU", 250) == "Credit Card Payments"
    # Card purchases and other payments aren't.
    assert categorize("Withdrawal Credit/Debit Card PURCHASE HULU.COM/BILL", -9.99) == "Subscriptions"
    assert categorize("CAPITAL ONE AUTO LOAN PAYMENT", -1800) != "Credit Card Payments"
    assert categorize("Withdrawal ACH ZELLE TYPE: PAYMENT", -40) != "Credit Card Payments"


def test_monthly_subscription_with_price_increase():
    txs = [tx("hulu", TODAY - timedelta(days=30 * i), -15.49 if i >= 2 else -17.99) for i in range(8)]
    [s] = detect(txs, TODAY)
    assert s.cadence == "monthly" and s.active
    assert s.amount == 17.99 and s.previous_amount == 15.49


def test_cancelled_subscription_is_inactive():
    txs = [tx("hulu", TODAY - timedelta(days=150 + 30 * i), -7.99) for i in range(6)]
    [s] = detect(txs, TODAY)
    assert not s.active


def test_variable_amounts_are_not_subscriptions():
    amounts = [-45, -180, -92, -130, -61, -155]
    txs = [tx("kroger", TODAY - timedelta(days=7 * i), a, "Groceries") for i, a in enumerate(amounts)]
    assert detect(txs, TODAY) == []


def test_yearly_needs_only_two_charges():
    txs = [tx("nytimes", TODAY - timedelta(days=d), -139) for d in (370, 5)]
    [s] = detect(txs, TODAY)
    assert s.cadence == "yearly" and round(s.monthly_cost, 2) == 11.58


def test_several_subscriptions_at_one_merchant():
    # One company billing a monthly plan, a yearly one, and some one-off purchases.
    txs = [tx("northwind", TODAY - timedelta(days=30 * i + 3), -9.99) for i in range(6)]
    txs += [tx("northwind", TODAY - timedelta(days=d), -19.99) for d in (10, 375, 740)]
    txs += [tx("northwind", TODAY - timedelta(days=d), a) for d, a in ((20, -4.99), (95, -0.99))]
    found = {s.key: s for s in detect(txs, TODAY, overrides={"northwind|19.99": "ignored"})}
    assert set(found) == {"northwind|9.99", "northwind|19.99"}
    assert found["northwind|9.99"].cadence == "monthly" and not found["northwind|9.99"].ignored
    assert found["northwind|19.99"].cadence == "yearly" and found["northwind|19.99"].ignored


def test_one_merchant_with_a_price_change_stays_one_subscription():
    txs = [tx("dropbox", TODAY - timedelta(days=30 * i), -10.99 if i >= 4 else -12.99) for i in range(10)]
    [s] = detect(txs, TODAY)
    assert s.key == "dropbox" and s.amount == 12.99


def test_bills_and_subscriptions():
    rent = [tx("maple court apts", TODAY - timedelta(days=30 * i), -1200, "Housing") for i in range(4)]
    hulu = [tx("hulu", TODAY - timedelta(days=30 * i), -15.49) for i in range(4)]
    kinds = {s.key: s.kind for s in detect(rent + hulu, TODAY)}
    assert kinds == {"maple court apts": "bill", "hulu": "subscription"}
    [s] = detect(hulu, TODAY, kinds={"hulu": "bill"})
    assert s.kind == "bill"


def test_page_always_loads_current_code():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    page = c.get("/")
    assert page.headers["cache-control"] == "no-cache"
    assert "/static/app.js?v=" in page.text and "/static/style.css?v=" in page.text
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
