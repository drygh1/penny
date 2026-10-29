import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    c.post("/api/demo")
    return c


def cats(client):
    return {c["name"]: c for c in client.get("/api/categories").json()}


def test_every_category_has_its_own_color(client):
    slots = [c["color_slot"] for c in cats(client).values()]
    assert None not in slots and len(set(slots)) == len(slots)


def test_add_category_takes_free_color_after_delete(client):
    assert client.post("/api/categories", json={"name": "Kids"}).status_code == 200
    assert cats(client)["Kids"]["color_slot"] == 18  # the 17 starting categories use 1-17
    for name in ("Pets", "Gifts"):  # slots 19-20
        client.post("/api/categories", json={"name": name})
    client.post("/api/categories", json={"name": "Garden"})
    assert cats(client)["Garden"]["color_slot"] is None  # all 20 colors in use

    # Deleting a category hands its color to the one left gray.
    client.delete("/api/categories", params={"name": "Entertainment", "move_to": "Other"})
    assert cats(client)["Garden"]["color_slot"] == 8


def test_gray_categories_from_before_get_colors(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    conn = db.connect()
    with conn:  # what an older Penny left behind: some categories without a color
        conn.execute("UPDATE categories SET color_slot = NULL WHERE name IN ('Health', 'Travel')")
    conn.close()
    slots = dict(db.connect().execute("SELECT name, color_slot FROM categories").fetchall())
    assert None not in slots.values() and len(set(slots.values())) == len(slots)


def test_rejects_duplicates_and_locked(client):
    assert client.post("/api/categories", json={"name": "dining"}).status_code == 400
    assert client.delete("/api/categories", params={"name": "Transfers"}).status_code == 400
    assert client.delete("/api/categories", params={"name": "Travel", "move_to": "Travel"}).status_code == 400


def test_delete_moves_transactions_rules_and_future_syncs(client):
    client.post("/api/categories", json={"name": "Vacation"})
    client.put("/api/merchant-rules", json={"merchant": "ikea", "category": "Travel"})
    travel_count = cats(client)["Travel"]["transactions"]
    assert travel_count > 0

    client.delete("/api/categories", params={"name": "Travel", "move_to": "Vacation"})
    after = cats(client)
    assert "Travel" not in after
    assert after["Vacation"]["transactions"] == travel_count  # airline charges + IKEA rule
    ikea = next(m for m in client.get("/api/merchants").json() if m["merchant"] == "ikea")
    assert ikea["rule_category"] == "Vacation"

    # Reloading data runs every transaction through categorization again.
    client.post("/api/demo")
    assert "Travel" not in {t["category"] for t in client.get("/api/transactions", params={"limit": 5000}).json()}
    assert cats(client)["Vacation"]["transactions"] == after["Vacation"]["transactions"]


def test_other_websites_cannot_change_data(client):
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/disconnect", headers=evil).status_code == 403
    assert client.post("/api/shutdown", headers=evil).status_code == 403
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    # Penny's own page is allowed.
    assert client.post("/api/demo", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


def test_atm_withdrawals_are_cash_but_atm_fees_are_fees():
    from app.categorize import categorize
    assert categorize("Withdrawal ATM #000000000111 WESTFIELD FEDERAL CU 100 MAIN ST SPRINGFIELD IL", -100) == "Cash"
    assert categorize("Withdrawal ATM #000000000385 MAPLETON CAFE -000123 SPRINGFIELD IL", -60) == "Cash"  # not Dining
    assert categorize("WITHDRAWAL AT ATM #000000000532ATMBANCO 2 FAIRVIEW CENTRO", -80.00) == "Cash"
    assert categorize("Withdrawal Fee ATM Fee Int'l Transaction Fee Assessed by Visa", -1.19) == "Fees"
    assert categorize("WITHDRAWAL ATM FeeInt'l Transaction Fee Assessed by Visa", -1.19) == "Fees"


def test_cash_is_added_once_to_older_databases(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    conn = db.connect()
    with conn:  # an older Penny: seeded before Cash existed
        conn.execute("DELETE FROM categories WHERE name = 'Cash'")
        db.set_setting(conn, "category_added:Cash", None)
    conn.close()
    names = db.category_names(db.connect())
    assert names.index("Cash") == names.index("Fees") - 1

    conn = db.connect()
    with conn:  # deleting it later sticks
        conn.execute("DELETE FROM categories WHERE name = 'Cash'")
    assert "Cash" not in db.category_names(db.connect())


def test_one_purchase_can_differ_from_its_merchant(client):
    txs = client.get("/api/transactions").json()
    merchant = max({t["merchant"] for t in txs}, key=lambda m: sum(t["merchant"] == m for t in txs))
    first, *rest = [t for t in txs if t["merchant"] == merchant]
    assert rest
    client.put("/api/merchant-rules", json={"merchant": merchant, "category": "Shopping"})
    assert client.put("/api/transaction-category", json={"id": first["id"], "category": "Health"}).status_code == 200

    by_id = {t["id"]: t for t in client.get("/api/transactions", params={"merchant": merchant}).json()}
    assert by_id[first["id"]]["category"] == "Health" and by_id[first["id"]]["edited"]
    assert by_id[first["id"]]["default_category"] == "Shopping"
    assert all(by_id[t["id"]]["category"] == "Shopping" and not by_id[t["id"]]["edited"] for t in rest)

    # Changing the merchant's default leaves the edited purchase alone.
    client.put("/api/merchant-rules", json={"merchant": merchant, "category": "Travel"})
    by_id = {t["id"]: t for t in client.get("/api/transactions", params={"merchant": merchant}).json()}
    assert by_id[first["id"]]["category"] == "Health"

    # Deleting its category moves it along with everything else.
    client.delete("/api/categories", params={"name": "Health", "move_to": "Fees"})
    by_id = {t["id"]: t for t in client.get("/api/transactions", params={"merchant": merchant}).json()}
    assert by_id[first["id"]]["category"] == "Fees"

    client.delete("/api/transaction-category", params={"id": first["id"]})
    by_id = {t["id"]: t for t in client.get("/api/transactions", params={"merchant": merchant}).json()}
    assert by_id[first["id"]]["category"] == "Travel" and not by_id[first["id"]]["edited"]
    assert client.put("/api/transaction-category", json={"id": "nope", "category": "Fees"}).status_code == 400
