"""Using Penny from another device must be private: see app/remote.py and app/auth.py."""
import pytest
from fastapi.testclient import TestClient

MESH = "100.101.102.103"  # this Mac's Meshnet address
PHONE = ("100.90.1.2", 50123)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from app import auth, db, main, remote
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny.db")
    monkeypatch.setattr(remote, "private_addresses", lambda: [MESH])
    monkeypatch.setattr(remote, "refresh", lambda app=None: None)  # no real listener in tests
    auth._failures.clear(); auth._all_failures.clear()
    mac = TestClient(main.app, base_url="http://127.0.0.1:8000")
    mac.post("/api/demo")
    phone = TestClient(main.app, base_url=f"http://{MESH}:8787", client=PHONE, follow_redirects=False)
    sharing = lambda on: monkeypatch.setattr(remote, "serving_ip", lambda: MESH if on else None)
    return mac, phone, sharing


def turn_on(mac, sharing, passcode="correct horse"):
    assert mac.post("/api/remote", json={"address": MESH, "passcode": passcode}).status_code == 200
    assert mac.post("/api/remote", json={"enabled": True}).status_code == 200
    sharing(True)


def test_nothing_is_reachable_while_sharing_is_off(setup):
    mac, phone, sharing = setup
    for path in ("/", "/login", "/api/accounts", "/static/app.js"):
        assert phone.get(path).status_code == 403
    turn_on(mac, sharing)
    sharing(False)  # e.g. Meshnet went off
    assert phone.get("/api/accounts").status_code == 403


def test_signed_out_devices_only_get_the_sign_in_page(setup):
    mac, phone, sharing = setup
    turn_on(mac, sharing)
    assert phone.get("/").status_code == 303 and phone.get("/").headers["location"] == "/login"
    assert phone.get("/login").status_code == 200
    for path in ("/api/accounts", "/api/transactions", "/api/status", "/api/review"):
        assert phone.get(path).status_code == 401, path
    assert phone.post("/api/review", json={"all_unchecked": True}).status_code == 401
    # Another name for the Mac (DNS rebinding) or another site's page can't get in either.
    assert phone.get("/api/accounts", headers={"host": "evil.example"}).status_code == 403
    assert phone.post("/api/login", json={"passcode": "correct horse"},
                      headers={"origin": "http://evil.example"}).status_code == 403


def test_sign_in_and_what_a_phone_may_do(setup):
    mac, phone, sharing = setup
    turn_on(mac, sharing)
    assert phone.post("/api/login", json={"passcode": "wrong one"}).status_code == 401
    r = phone.post("/api/login", json={"passcode": "correct horse"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    r = phone.get("/api/accounts")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert phone.get("/api/status").json()["remote"] is True
    assert mac.get("/api/status").json()["remote"] is False

    # Mac-only, even signed in.
    for method, path in (("post", "/api/remote"), ("get", "/api/remote"), ("post", "/api/delete-all-data"),
                         ("post", "/api/simplefin/connect"), ("post", "/api/disconnect"), ("get", "/api/backups"),
                         ("post", "/api/backups/restore"), ("post", "/api/shutdown"), ("post", "/api/demo")):
        r = phone.get(path) if method == "get" else phone.post(path, json={})
        assert r.status_code == 403, path

    phone.post("/api/logout")
    assert phone.get("/api/accounts").status_code == 401


def test_new_passcode_or_sign_out_all_ends_every_session(setup):
    mac, phone, sharing = setup
    turn_on(mac, sharing)
    phone.post("/api/login", json={"passcode": "correct horse"})
    assert phone.get("/api/accounts").status_code == 200
    mac.post("/api/remote", json={"passcode": "a brand new one"})
    assert phone.get("/api/accounts").status_code == 401
    phone.post("/api/login", json={"passcode": "a brand new one"})
    mac.post("/api/remote/sign-out-all")
    assert phone.get("/api/accounts").status_code == 401


def test_wrong_guesses_are_limited(setup):
    mac, phone, sharing = setup
    turn_on(mac, sharing)
    for _ in range(5):
        assert phone.post("/api/login", json={"passcode": "guess"}).status_code == 401
    # Locked out now, even with the right passcode.
    assert phone.post("/api/login", json={"passcode": "correct horse"}).status_code == 429


def test_settings_guard_rails(setup):
    mac, phone, sharing = setup
    assert mac.post("/api/remote", json={"enabled": True}).status_code == 400  # no address or passcode yet
    assert mac.post("/api/remote", json={"address": "10.5.0.2"}).status_code == 400  # not a Meshnet address here
    assert mac.post("/api/remote", json={"passcode": "short"}).status_code == 400
    from app import db
    conn = db.connect()
    assert db.get_setting(conn, "remote_passcode") is None
    # The passcode is only ever stored hashed.
    mac.post("/api/remote", json={"passcode": "correct horse"})
    stored = db.get_setting(conn, "remote_passcode")
    assert stored.startswith("scrypt$") and "correct horse" not in stored


def test_only_tunnel_addresses_count(monkeypatch):
    from app import remote
    out = """lo0: flags=8049<UP,LOOPBACK>
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST>
\tinet 192.168.1.20 netmask 0xffffff00
\tinet 100.70.0.9 netmask 0xffc00000
utun3: flags=8051<UP,POINTOPOINT>
\tinet 100.101.102.103 --> 100.101.102.103 netmask 0xffc00000
utun4: flags=8051<UP,POINTOPOINT>
\tinet 10.5.0.2 --> 10.5.0.1 netmask 0xff000000
"""
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": out})())
    assert remote.private_addresses() == ["100.101.102.103"]  # not Wi-Fi, not the ordinary VPN tunnel
