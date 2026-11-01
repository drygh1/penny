from datetime import date

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8000")


def steps(client):
    return {s["id"]: s["done"] for s in client.get("/api/guide").json()["steps"]}


def import_real_account(client):
    text = f"Date,Description,Amount\n{date.today():%m/%d/%Y},PINECREST GROCERY,-40\n"
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"]
    client.post("/api/import", json={"csv": text, "mapping": mapping,
                                     "new_account": {"name": "Checking", "balance": 500}})


def test_checklist_ticks_off_from_what_you_do(client):
    assert steps(client) == {"demo": False, "connect": False, "import": False,
                             "categories": False, "subscriptions": False, "review": False, "phone": False}

    # Poking around the demo only counts for the demo step.
    client.post("/api/demo")
    client.post("/api/guide/seen", json={"view": "subscriptions"})
    client.post("/api/guide/seen", json={"view": "merchants"})
    client.post("/api/guide/seen", json={"view": "review"})
    assert steps(client) == {"demo": True, "connect": False, "import": False,
                             "categories": False, "subscriptions": False, "review": False, "phone": False}

    client.post("/api/delete-all-data")
    import_real_account(client)
    assert steps(client) | {"demo": True} == {"demo": True, "connect": True, "import": True,
                                              "categories": False, "subscriptions": False, "review": False, "phone": False}
    client.post("/api/guide/seen", json={"view": "subscriptions"})
    client.post("/api/guide/seen", json={"view": "review"})
    merchant = client.get("/api/merchants").json()[0]["merchant"]
    client.put("/api/merchant-rules", json={"merchant": merchant, "category": "Groceries"})
    assert all(v for k, v in steps(client).items() if k != "phone")  # the phone step is optional


def test_hiding_the_checklist_and_tips(client):
    assert client.post("/api/guide/dismiss", json={"view": "merchants"}).status_code == 200
    assert client.post("/api/guide/dismiss", json={}).status_code == 200
    g = client.get("/api/guide").json()
    assert g["dismissed"] and g["tips_dismissed"] == ["merchants"]
    assert client.post("/api/guide/dismiss", json={"view": "nope"}).status_code == 400


class FakeResponse:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


def test_update_notice(client, monkeypatch):
    from app import main
    assert client.get("/api/update").json()["available"] is False  # not the Mac app: never checks

    monkeypatch.setattr(main, "DESKTOP", True)
    calls = []
    release = {"tag_name": "v1.1", "html_url": "https://example.com/penny/1.1", "body": "Faster syncs"}
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: calls.append(a) or FakeResponse(release))
    u = client.get("/api/update").json()
    assert u["available"] and u["latest"] == "1.1" and u["url"].endswith("/1.1") and u["notes"] == "Faster syncs"
    client.get("/api/update")
    assert len(calls) == 1  # checked at most once a day

    client.post("/api/update", json={"dismiss": "1.1"})
    assert client.get("/api/update").json()["available"] is False

    release["tag_name"] = "v1.2"
    assert client.get("/api/update", params={"force": True}).json()["latest"] == "1.2"

    client.post("/api/update", json={"enabled": False})
    u = client.get("/api/update", params={"force": True}).json()
    assert not u["enabled"] and not u["available"] and len(calls) == 2

    # Offline: no notice, no error.
    client.post("/api/update", json={"enabled": True, "dismiss": "1.2"})
    def offline(*a, **k): raise OSError("no network")
    monkeypatch.setattr(main.httpx, "get", offline)
    assert client.get("/api/update", params={"force": True}).status_code == 200
