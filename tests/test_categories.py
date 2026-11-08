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


def test_income_sources_keep_their_color_across_periods(client):
    periods = client.get("/api/periods").json()["months"]
    slots = {}
    for p in periods:
        for s in client.get("/api/spending/breakdown", params={"period": p}).json()["income_sources"]:
            assert slots.setdefault(s["merchant"], s["color_slot"]) == s["color_slot"]
    assert 1 in slots.values()
    assert all(v is None or 1 <= v <= 20 for v in slots.values())
    # Every source gets its own color, so the dashboard has no gray income slice.
    assert None not in slots.values()


def test_combining_merchants_and_separating_them_again(client):
    merchants = client.get("/api/merchants").json()
    keep, other = merchants[0]["merchant"], merchants[1]["merchant"]
    counts = {m["merchant"]: m["count"] for m in merchants}
    client.put("/api/merchant-rules", json={"merchant": other, "category": "Travel"})

    assert client.post("/api/merchants/combine", json={"merchant": keep, "others": [other]}).status_code == 200
    after = {m["merchant"]: m for m in client.get("/api/merchants").json()}
    assert other not in after and after[keep]["count"] == counts[keep] + counts[other]
    assert after[keep]["combined"] == [{"merchant": other, "name": other.title()}]
    assert after[keep]["rule_category"] == "Travel"  # its rule came along, since keep had none
    assert {t["merchant"] for t in client.get("/api/transactions", params={"merchant": keep, "limit": 1000}).json()} == {keep}

    # Combining into a merchant that was itself combined lands on the one that stays.
    third = merchants[2]["merchant"]
    client.post("/api/merchants/combine", json={"merchant": other, "others": [third]})
    assert third not in {m["merchant"] for m in client.get("/api/merchants").json()}

    client.delete("/api/merchants/combine", params={"alias": other})
    after = {m["merchant"]: m for m in client.get("/api/merchants").json()}
    assert after[other]["count"] == counts[other]
    assert client.post("/api/merchants/combine", json={"merchant": keep, "others": [keep]}).status_code == 400


def test_rename_moves_everything_and_future_transactions(client):
    from app import db, sync
    txs = client.get("/api/transactions", params={"category": "Groceries", "limit": 1000}).json()
    count, slot = len(txs), cats(client)["Groceries"]["color_slot"]
    merchant = txs[0]["merchant"]
    client.put("/api/merchant-rules", json={"merchant": "ikea", "category": "Groceries"})
    client.put("/api/description-rules", json={"merchant": merchant, "pattern": "kroger", "category": "Groceries"})
    other = client.get("/api/transactions", params={"category": "Dining"}).json()[0]
    client.put("/api/transaction-category", json={"id": other["id"], "category": "Groceries"})
    before = cats(client)["Groceries"]["transactions"]

    assert client.put("/api/categories", json={"name": "Groceries", "new_name": "  Food   at home "}).json()["name"] == "Food at home"
    after = cats(client)
    assert "Groceries" not in after and after["Food at home"]["transactions"] == before
    assert after["Food at home"]["color_slot"] == slot
    assert next(m for m in client.get("/api/merchants").json() if m["merchant"] == "ikea")["rule_category"] == "Food at home"
    assert client.get("/api/merchant-descriptions", params={"merchant": merchant}).json()["rules"][0]["category"] == "Food at home"
    assert next(t for t in client.get("/api/transactions", params={"limit": 5000}).json()
                if t["id"] == other["id"])["category"] == "Food at home"

    # Keywords still say "Groceries": re-running them, and new transactions, land in the new name.
    conn = db.connect()
    sync.recategorize_all(conn)
    assert db.resolve_category(conn, "Groceries") == "Food at home"
    assert "Groceries" not in {t["category"] for t in client.get("/api/transactions", params={"limit": 5000}).json()}

    # Renaming again, and back again, leaves no loop behind.
    client.put("/api/categories", json={"name": "Food at home", "new_name": "Food"})
    assert db.resolve_category(conn, "Groceries") == "Food"
    client.put("/api/categories", json={"name": "Food", "new_name": "Groceries"})
    assert db.resolve_category(conn, "Groceries") == "Groceries" and db.resolve_category(conn, "Food") == "Groceries"


def test_rename_guard_rails(client):
    for fixed in ("Income", "Transfers", "Other", "Credit Card Payments", "Investments"):
        assert client.put("/api/categories", json={"name": fixed, "new_name": "Something"}).status_code == 400
        assert cats(client)[fixed]["fixed_name"]
    assert client.put("/api/categories", json={"name": "Dining", "new_name": "groceries"}).status_code == 400
    assert client.put("/api/categories", json={"name": "Dining", "new_name": "Income"}).status_code == 400
    assert client.put("/api/categories", json={"name": "Dining", "new_name": "   "}).status_code == 400
    assert client.put("/api/categories", json={"name": "Nope", "new_name": "Fine"}).status_code == 400
    assert client.put("/api/categories", json={"name": "Dining", "new_name": "DINING"}).status_code == 200
    assert "DINING" in cats(client)


def test_renamed_bill_category_still_counts_as_bills(client):
    def kinds():
        return {s["category"]: s["kind"] for s in client.get("/api/subscriptions").json()["items"]}
    assert kinds()["Housing"] == "bill"
    client.put("/api/categories", json={"name": "Housing", "new_name": "Rent"})
    assert kinds()["Rent"] == "bill"
