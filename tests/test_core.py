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
    assert categorize("ONLINE PAYMENT TO VISA", -500) == "Transfers"
    assert categorize("ACME CORP PAYROLL", 2000) == "Income"
    assert categorize("CURRENT BANK MISC", -10) == "Other"  # "rent" inside "current" must not match


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
