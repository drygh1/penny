from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    days = [3, 10, 25, 45, 200]  # three in the last 30 days, two before
    text = "Date,Description,Amount\n" + "".join(
        f"{date.today() - timedelta(days=d):%m/%d/%Y},PINECREST GROCERY {d},-{d}.00\n" for d in days)
    mapping = c.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    c.post("/api/import", json={"csv": text, "mapping": mapping, "new_account": {"name": "Checking", "balance": 500}})
    return c


def unchecked(client):
    return client.get("/api/review").json()


def test_starts_with_the_last_30_days(client):
    r = unchecked(client)
    assert r["remaining"] == 3 and len(r["items"]) == 3
    assert [t["amount"] for t in r["items"]] == [-25, -10, -3]  # oldest first, to work forward
    assert client.get("/api/status").json()["to_review"] == 3
    recent = client.get("/api/review", params={"show": "recent"}).json()["items"]
    assert {t["amount"]: t["reviewed"] for t in recent} == {-3: False, -10: False, -25: False, -45: True}


def test_check_uncheck_and_change(client):
    first, second, third = unchecked(client)["items"]
    client.post("/api/review", json={"ids": [first["id"]]})
    assert unchecked(client)["remaining"] == 2

    # Choosing a category checks it, and changes only that purchase.
    client.put("/api/transaction-category", json={"id": second["id"], "category": "Dining"})
    r = unchecked(client)
    assert [t["id"] for t in r["items"]] == [third["id"]]
    cats = {t["id"]: t["category"] for t in client.get("/api/transactions").json()}
    assert cats[second["id"]] == "Dining" and cats[third["id"]] == "Groceries"

    # Undo, even for something older than the starting line.
    old = [t for t in client.get("/api/review", params={"show": "recent"}).json()["items"] if t["amount"] == -45][0]
    client.post("/api/review", json={"ids": [first["id"], old["id"]], "reviewed": False})
    assert {t["id"] for t in unchecked(client)["items"]} == {first["id"], third["id"], old["id"]}

    client.post("/api/review", json={"all_unchecked": True})
    assert unchecked(client) == {"remaining": 0, "items": []}


def test_checks_survive_syncs_and_pending_charges_posting(tmp_path, monkeypatch):
    """Each sync drops pending rows and re-adds them, and a pending charge posts
    under a new ID, sometimes with a tip added. Checks and one-purchase
    categories must follow the transaction, not the row."""
    import time
    from app import db, simplefin, sync
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    now = int(time.time())
    day = 86400

    def bank(*txs):
        account = {"id": "card", "name": "Card", "currency": "USD", "balance": "-100", "balance-date": now,
                   "org": {"name": "Bank"}, "transactions": [
                       {"id": i, "posted": p, "amount": a, "description": d, "pending": pend}
                       for i, p, a, d, pend in txs]}
        monkeypatch.setattr(simplefin, "fetch_accounts", lambda *args: {"accounts": [account], "errors": []})
        conn = db.connect()
        with conn:
            db.set_setting(conn, "simplefin_access_url", "https://example.invalid")
        sync.sync(conn)

    bank(("p1", now - 2 * day, -18.40, "PINECREST BISTRO", True),       # dinner, tip not in yet
         ("p2", now - 1 * day, -31.20, "MEADOWBROOK GROCERY", True),
         ("p3", now - 1 * day, -40.00, "VALERO", True))
    ids = {t["description"]: t["id"] for t in client.get("/api/review").json()["items"]}
    client.post("/api/review", json={"ids": [ids["PINECREST BISTRO"], ids["MEADOWBROOK GROCERY"]]})
    client.put("/api/transaction-category", json={"id": ids["VALERO"], "category": "Travel"})
    assert client.get("/api/review").json()["remaining"] == 0

    # Same pending charges again: nothing comes back unchecked.
    bank(("p1", now - 2 * day, -18.40, "PINECREST BISTRO", True),
         ("p2", now - 1 * day, -31.20, "MEADOWBROOK GROCERY", True),
         ("p3", now - 1 * day, -40.00, "VALERO", True))
    assert client.get("/api/review").json()["remaining"] == 0

    # They post under new IDs; the dinner with a tip added. A new purchase arrives too.
    bank(("t1", now - 2 * day, -22.08, "PINECREST BISTRO", False),
         ("t2", now - 1 * day, -31.20, "MEADOWBROOK GROCERY", False),
         ("t3", now - 1 * day, -40.00, "VALERO", False),
         ("t4", now, -5.25, "COFFEE CART", True))
    r = client.get("/api/review").json()
    assert [t["description"] for t in r["items"]] == ["COFFEE CART"]
    gas = [t for t in client.get("/api/transactions").json() if t["description"] == "VALERO"]
    assert len(gas) == 1 and gas[0]["category"] == "Travel" and gas[0]["edited"]
