from datetime import timedelta

from tests.test_transfers import client, new_account, this_month  # noqa: F401  (fixture)


def month_of(d):
    return f"{d:%Y-%m}"


def shift(m, n):
    y, mo = map(int, m.split("-"))
    y, mo = divmod(y * 12 + mo - 1 + n, 12)
    return f"{y:04d}-{mo + 1:02d}"


def cat(r, name):
    return next(c for c in r["categories"] if c["category"] == name)


def test_budget_tracks_spending_and_carries_forward(client):
    last = (this_month - timedelta(days=1)).replace(day=1)
    new_account(client, "Checking", [(last, "PINECREST GROCERY", -300.00),
                                     (this_month, "PINECREST GROCERY", -120.00),
                                     (this_month, "PINECREST GROCERY REFUND", 20.00),
                                     (this_month, "ACME CORP PAYROLL", 3000.00)])
    client.put("/api/merchant-rules", json={"merchant": "pinecrest grocery", "category": "Groceries"})
    client.put("/api/merchant-rules", json={"merchant": "pinecrest grocery refund", "category": "Groceries"})
    now = month_of(this_month)

    r = client.get("/api/budget", params={"month": now}).json()
    names = [c["category"] for c in r["categories"]]
    assert "Income" not in names and "Transfers" not in names  # not spending, no budget
    g = cat(r, "Groceries")
    assert (g["budget"], g["spent"], g["average"]) == (None, 100.00, 300.00)  # refund nets out
    assert r["unbudgeted_spent"] == 100.00 and r["income"] == 3000.00

    assert client.put("/api/budget", json={"category": "Groceries", "month": now, "amount": 400}).status_code == 200
    r = client.get("/api/budget", params={"month": now}).json()
    assert (r["budgeted"], r["spent"], r["left"]) == (400.00, 100.00, 300.00)
    # It carries on to later months, not earlier ones.
    assert cat(client.get("/api/budget", params={"month": shift(now, 5)}).json(), "Groceries")["budget"] == 400
    assert cat(client.get("/api/budget", params={"month": shift(now, -1)}).json(), "Groceries")["budget"] is None

    # December only: the month after goes back to 400.
    dec = shift(now, 2)
    client.put("/api/budget", json={"category": "Groceries", "month": dec, "amount": 650, "only_this_month": True})
    amounts = lambda: [cat(client.get("/api/budget", params={"month": shift(now, n)}).json(), "Groceries")["budget"]
                       for n in range(5)]
    assert amounts() == [400, 400, 650, 400, 400]
    assert cat(client.get("/api/budget", params={"month": dec}).json(), "Groceries")["only_this_month"]

    # A change from next month on replaces everything after it, the one-off too.
    client.put("/api/budget", json={"category": "Groceries", "month": shift(now, 1), "amount": 450})
    assert amounts() == [400, 450, 450, 450, 450]

    # Removing it from this month on.
    client.put("/api/budget", json={"category": "Groceries", "month": now, "amount": None})
    assert amounts() == [None] * 5


def test_budget_follows_category_rename_and_delete(client):
    now = month_of(this_month)
    client.put("/api/budget", json={"category": "Dining", "month": now, "amount": 200})
    client.put("/api/categories", json={"name": "Dining", "new_name": "Eating out"})
    assert cat(client.get("/api/budget", params={"month": now}).json(), "Eating out")["budget"] == 200
    client.delete("/api/categories", params={"name": "Eating out", "move_to": "Other"})
    r = client.get("/api/budget", params={"month": now}).json()
    assert r["budgeted"] == 0 and "Eating out" not in [c["category"] for c in r["categories"]]


def test_budget_refuses_bad_input(client):
    now = month_of(this_month)
    assert client.put("/api/budget", json={"category": "Income", "month": now, "amount": 5}).status_code == 400
    assert client.put("/api/budget", json={"category": "Nope", "month": now, "amount": 5}).status_code == 400
    assert client.put("/api/budget", json={"category": "Dining", "month": "2026-13", "amount": 5}).status_code == 400
    assert client.put("/api/budget", json={"category": "Dining", "month": now, "amount": -5}).status_code == 400
    assert client.get("/api/budget", params={"month": "nope"}).status_code == 400


def test_budget_average_income_over_a_year(client):
    now = month_of(this_month)
    rows = []
    # Paid in each of the last 14 months: 2000 a month, then 3000 for the last 6.
    for n in range(1, 15):
        y, m = map(int, shift(now, -n).split("-"))
        d = this_month.replace(year=y, month=m, day=1)
        rows.append((d, "ACME CORP PAYROLL", 3000.00 if n <= 6 else 2000.00))
    rows.append((this_month, "ACME CORP PAYROLL", 9000.00))  # this month doesn't count
    new_account(client, "Checking", rows)
    r = client.get("/api/budget", params={"month": now}).json()
    assert (r["average_income"], r["income_months"]) == (2500.00, 12)


def test_budget_average_income_skips_partial_first_month(client):
    now = month_of(this_month)
    y, m = map(int, shift(now, -3).split("-"))
    first = this_month.replace(year=y, month=m, day=20)  # history starts late in the month
    rows = [(first, "ACME CORP PAYROLL", 500.00)]
    for n in (1, 2):
        y, m = map(int, shift(now, -n).split("-"))
        rows.append((this_month.replace(year=y, month=m, day=1), "ACME CORP PAYROLL", 2000.00))
    new_account(client, "Checking", rows)
    r = client.get("/api/budget", params={"month": now}).json()
    assert (r["average_income"], r["income_months"]) == (2000.00, 2)
