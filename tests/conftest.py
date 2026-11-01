import pytest


@pytest.fixture(autouse=True)
def isolated_backups(tmp_path, monkeypatch):
    """Keep every test's backups in a temp folder, never the real one."""
    from app import backup
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")


@pytest.fixture(autouse=True)
def test_client_is_this_mac(monkeypatch):
    """Starlette's TestClient says requests come from "testclient"; count that as
    this Mac. (Real requests always carry an IP address.) test_remote.py sends
    requests as another device on purpose."""
    from app import main
    monkeypatch.setattr(main, "LOCAL_CLIENTS", main.LOCAL_CLIENTS | {"testclient"})
