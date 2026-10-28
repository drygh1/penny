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


def test_add_category_takes_free_color_after_delete(client):
    assert client.post("/api/categories", json={"name": "Kids"}).status_code == 200
    assert cats(client)["Kids"]["color_slot"] is None  # all 8 colors in use

    client.delete("/api/categories", params={"name": "Entertainment", "move_to": "Other"})
    client.post("/api/categories", json={"name": "  Pets  "})
    assert cats(client)["Pets"]["color_slot"] == 8  # Entertainment's freed color


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
