import base64
import struct
import zlib

import pytest
from fastapi.testclient import TestClient


def tiny_png(rgb=(42, 120, 214)) -> bytes:
    """A valid 4x4 solid-color PNG, built without any imaging library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + bytes(rgb) * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    from app.main import app
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    c.post("/api/demo")
    return c


def merchant(client, key):
    return next(m for m in client.get("/api/merchants").json() if m["merchant"] == key)


def test_rule_overrides_and_reverts_category(client):
    assert merchant(client, "valero springfield")["auto_category"] == "Transportation"
    client.put("/api/merchant-rules", json={"merchant": "valero springfield", "category": "Travel"})
    m = merchant(client, "valero springfield")
    assert m["rule_category"] == "Travel"
    txs = client.get("/api/transactions", params={"merchant": "valero springfield"}).json()
    assert txs and all(t["category"] == "Travel" for t in txs)

    client.delete("/api/merchant-rules", params={"merchant": "valero springfield"})
    assert merchant(client, "valero springfield")["rule_category"] is None
    txs = client.get("/api/transactions", params={"merchant": "valero springfield"}).json()
    assert all(t["category"] == "Transportation" for t in txs)


def test_logo_upload_serve_and_delete(client):
    png = tiny_png()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    assert client.put("/api/merchant-logo", json={"merchant": "disney plus", "image": data_url}).status_code == 200
    assert "disney plus" in client.get("/api/merchant-logos").json()

    r = client.get("/api/merchant-logo", params={"merchant": "disney plus"})
    assert r.status_code == 200 and r.content == png and r.headers["content-type"] == "image/png"

    client.delete("/api/merchant-logo", params={"merchant": "disney plus"})
    assert client.get("/api/merchant-logo", params={"merchant": "disney plus"}).status_code == 404


def test_logo_rejects_svg(client):
    svg = "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload='alert(1)'/>").decode()
    assert client.put("/api/merchant-logo", json={"merchant": "disney plus", "image": svg}).status_code == 400


def test_rename_shows_everywhere_and_reverts(client):
    client.put("/api/merchants/name", json={"merchant": "starbucks", "name": "  Corner   Coffee "})
    m = merchant(client, "starbucks")
    assert (m["name"], m["bank_name"]) == ("Corner Coffee", "Starbucks")
    txs = client.get("/api/transactions", params={"merchant": "starbucks"}).json()
    assert txs and {t["name"] for t in txs} == {"Corner Coffee"}
    found = client.get("/api/transactions", params={"q": "corner coff"}).json()  # search knows your name
    assert found and {t["merchant"] for t in found} == {"starbucks"}
    month = txs[0]["month"]
    top = client.get("/api/spending/breakdown", params={"period": month}).json()["merchants"]
    assert "Corner Coffee" in {t["name"] for t in top}

    client.put("/api/merchants/name", json={"merchant": "disney plus", "name": "Movie night"})
    subs = {s["merchant"]: s["name"] for s in client.get("/api/subscriptions").json()["items"]}
    assert subs["disney plus"] == "Movie night"

    client.put("/api/merchants/name", json={"merchant": "starbucks", "name": ""})
    assert merchant(client, "starbucks")["name"] == "Starbucks"
    r = client.put("/api/merchants/name", json={"merchant": "starbucks", "name": "x" * 61})
    assert r.status_code == 400


def test_combining_keeps_a_name(client):
    client.put("/api/merchants/name", json={"merchant": "larkspur coffee", "name": "Larkspur"})
    client.post("/api/merchants/combine", json={"merchant": "starbucks", "others": ["larkspur coffee"]})
    assert merchant(client, "starbucks")["name"] == "Larkspur"  # starbucks had no name of its own
