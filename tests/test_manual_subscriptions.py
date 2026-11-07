from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.subscriptions import charge_dates


def test_charge_dates_step_by_schedule():
    today = date(2026, 11, 7)
    assert charge_dates(date(2026, 11, 20), "monthly", today) == (None, date(2026, 11, 20))  # not started yet
    assert charge_dates(date(2026, 1, 7), "monthly", today) == (date(2026, 10, 7), date(2026, 11, 7))  # due today
    assert charge_dates(date(2026, 1, 31), "monthly", today) == (date(2026, 10, 31), date(2026, 11, 30))  # month end
    assert charge_dates(date(2025, 12, 1), "quarterly", today) == (date(2026, 9, 1), date(2026, 12, 1))
    assert charge_dates(date(2024, 2, 29), "yearly", today) == (date(2026, 2, 28), date(2027, 2, 28))
    assert charge_dates(date(2026, 10, 30), "every 2 weeks", today) == (date(2026, 10, 30), date(2026, 11, 13))
    assert charge_dates(date(2026, 11, 1), "weekly", today) == (date(2026, 11, 1), date(2026, 11, 8))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    c.post("/api/demo")
    return c


def entry(**changes):
    soon = (date.today() + timedelta(days=5)).isoformat()
    return {"name": "Cloud Storage", "amount": 2.99, "cadence": "monthly", "next_date": soon,
            "category": "Subscriptions", "kind": "subscription"} | changes


def manual_items(client):
    return [s for s in client.get("/api/subscriptions").json()["items"] if s["manual_id"]]


def test_add_counts_in_totals_then_edit_and_delete(client):
    before = client.get("/api/subscriptions").json()
    r = client.post("/api/subscriptions/manual", json=entry(name="  Cloud   Storage "))
    item_id = r.json()["id"]
    after = client.get("/api/subscriptions").json()
    assert round(after["subscriptions_monthly"] - before["subscriptions_monthly"], 2) == 2.99
    assert round(after["yearly_total"] - before["yearly_total"], 2) == round(2.99 * 365.25 / 30.44, 2)
    [s] = manual_items(client)
    assert (s["name"], s["key"], s["active"], s["kind"]) == ("Cloud Storage", f"manual:{item_id}", True, "subscription")
    assert s["next_date"] == entry()["next_date"]

    # A yearly bill instead: it moves to Bills and costs a twelfth a month.
    client.put(f"/api/subscriptions/manual/{item_id}", json=entry(amount=120, cadence="yearly", kind="bill",
                                                                   category="Utilities & Bills"))
    [s] = manual_items(client)
    assert (s["kind"], s["cadence"], s["monthly_cost"]) == ("bill", "yearly", round(120 * 30.44 / 365.25, 2))

    client.delete(f"/api/subscriptions/manual/{item_id}")
    assert manual_items(client) == []
    assert client.put(f"/api/subscriptions/manual/{item_id}", json=entry()).status_code == 400


def test_rejects_bad_entries(client):
    for bad in (entry(name=" "), entry(amount=0), entry(cadence="daily"), entry(category="Nope")):
        assert client.post("/api/subscriptions/manual", json=bad).status_code == 400
    assert manual_items(client) == []


def test_follows_category_rename_and_delete(client):
    client.post("/api/categories", json={"name": "Apps"})
    client.post("/api/subscriptions/manual", json=entry(category="Apps"))
    client.put("/api/categories", json={"name": "Apps", "new_name": "Phone apps"})
    assert manual_items(client)[0]["category"] == "Phone apps"
    client.delete("/api/categories", params={"name": "Phone apps", "move_to": "Entertainment"})
    assert manual_items(client)[0]["category"] == "Entertainment"
