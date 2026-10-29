import os
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import backup


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "penny.db")
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8000")


def import_rows(client, rows, file_name="old.csv"):
    text = "Date,Description,Amount\n" + "".join(f"{d},{desc},{amt}\n" for d, desc, amt in rows)
    mapping = client.post("/api/import/preview", json={"csv": text}).json()["mapping"] | {"flip": False}
    return client.post("/api/import", json={"csv": text, "mapping": mapping, "file_name": file_name,
                                            "new_account": {"name": "Checking", "balance": 100}}).json()


def test_disconnect_keeps_data_and_demo_cannot_overwrite_it(client):
    import_rows(client, [("01/05/2025", "PINECREST GROCERY", -40)])
    client.post("/api/disconnect")
    assert client.get("/api/data").json()["transactions"] == 1
    assert client.post("/api/demo").status_code == 400


def test_imported_file_is_kept_as_original(client):
    import_rows(client, [("01/05/2025", "PINECREST GROCERY", -40)])
    import_rows(client, [("01/05/2025", "PINECREST GROCERY", -40)])  # same file again: stored once
    assert client.get("/api/data").json()["originals"] == {"csv": 1}


def test_delete_all_takes_a_safety_backup_and_restore_brings_it_back(client):
    import_rows(client, [("01/05/2025", "PINECREST GROCERY", -40), ("01/06/2025", "PAYROLL", 900)])
    r = client.post("/api/delete-all-data").json()
    assert r["ok"] and client.get("/api/data").json()["transactions"] == 0

    safety = next(b for b in backup.list_backups() if b["reason"] == "before-delete")
    assert client.post("/api/backups/restore", json={"name": safety["name"]}).status_code == 200
    assert client.get("/api/data").json()["transactions"] == 2
    # Restoring also backed up the state it replaced.
    assert any(b["reason"] == "before-restore" for b in backup.list_backups())


def test_restore_rejects_paths_outside_the_backup_folder(client):
    import_rows(client, [("01/05/2025", "PINECREST GROCERY", -40)])
    assert client.post("/api/backups/restore", json={"name": "../data/penny.db"}).status_code == 400


def test_demo_data_is_not_backed_up(client):
    client.post("/api/demo")
    client.post("/api/sync")  # fails (no SimpleFIN), but must not back up demo data either way
    assert backup.list_backups() == []


def test_pruning_keeps_14_days_and_12_monthly(tmp_path):
    backup.BACKUP_DIR.mkdir(parents=True)
    start = datetime(2025, 1, 1, 9)
    for i in range(400):  # a backup every day for 400 days
        d = start + timedelta(days=i)
        (backup.BACKUP_DIR / f"penny-{d:%Y-%m-%d_%H%M%S}-daily.db").write_bytes(b"x")
    backup.prune()
    names = [b["name"] for b in backup.list_backups()]
    newest_days = {(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(386, 400)}
    assert newest_days <= {n[6:16] for n in names}
    monthly = {n[6:13] for n in names if n[14:16] == "01"}
    assert len(monthly) >= 12 and len(names) <= 14 + 12


def test_backup_file_is_a_working_private_copy(tmp_path):
    src = sqlite3.connect(tmp_path / "live.db")
    src.execute("CREATE TABLE t (x)"); src.execute("INSERT INTO t VALUES (42)"); src.commit()
    path = backup.create(src, "manual")
    assert sqlite3.connect(path).execute("SELECT x FROM t").fetchone() == (42,)
    assert oct(os.stat(path).st_mode)[-3:] == "600"
