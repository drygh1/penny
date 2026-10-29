import pytest


@pytest.fixture(autouse=True)
def isolated_backups(tmp_path, monkeypatch):
    """Keep every test's backups in a temp folder, never the real one."""
    from app import backup
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
