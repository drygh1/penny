"""Automatic local backups of Penny's database.

Backups live outside the project folder (by default in
~/Library/Application Support/Penny/backups), so they survive the project
being moved or deleted, and they aren't synced to any cloud service. Time
Machine picks them up if you use it.

Kept: one backup per day for the last 14 days, the first backup of each of
the last 12 months, and the last 20 "safety" backups taken right before
anything destructive (deleting data, undoing an import, restoring).
"""
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

BACKUP_DIR = Path(os.environ.get(
    "PENNY_BACKUP_DIR", Path.home() / "Library" / "Application Support" / "Penny" / "backups"))
KEEP_DAILY = 14
KEEP_MONTHLY = 12
KEEP_SAFETY = 20

_NAME = re.compile(r"^penny-(\d{4}-\d{2}-\d{2})_(\d{6})-([a-z]+(?:-[a-z]+)*)(?:-\d+)?\.db$")


def _ensure_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)  # bank data: readable only by you


def create(conn, reason: str = "daily") -> Path:
    """Copy the live database to a new backup file. Safe while Penny is running."""
    _ensure_dir()
    stamp = datetime.now()
    path = BACKUP_DIR / f"penny-{stamp:%Y-%m-%d_%H%M%S}-{reason}.db"
    n = 1
    while path.exists():  # two backups in the same second
        path = BACKUP_DIR / f"penny-{stamp:%Y-%m-%d_%H%M%S}-{reason}-{n}.db"
        n += 1
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(path, 0o600)
    prune()
    return path


def daily_if_needed(conn) -> Path | None:
    """Make today's backup if there isn't one yet."""
    today = f"{datetime.now():%Y-%m-%d}"
    if any(b["day"] == today and b["reason"] == "daily" for b in list_backups()):
        return None
    return create(conn, "daily")


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in BACKUP_DIR.iterdir():
        m = _NAME.match(p.name)
        if m:
            out.append({"name": p.name, "day": m[1], "time": m[2], "reason": m[3], "bytes": p.stat().st_size})
    return sorted(out, key=lambda b: (b["day"], b["time"]), reverse=True)


def prune():
    backups = list_backups()  # newest first
    daily = [b for b in backups if b["reason"] == "daily"]
    keep = {b["name"] for b in daily[:KEEP_DAILY]}
    first_of_month = {}
    for b in reversed(daily):  # oldest first, so each month maps to its first backup
        first_of_month.setdefault(b["day"][:7], b["name"])
    for month in sorted(first_of_month, reverse=True)[:KEEP_MONTHLY]:
        keep.add(first_of_month[month])
    keep |= {b["name"] for b in [b for b in backups if b["reason"] != "daily"][:KEEP_SAFETY]}
    for b in backups:
        if b["name"] not in keep:
            (BACKUP_DIR / b["name"]).unlink(missing_ok=True)


def restore(conn, name: str):
    """Replace the live database with a backup, after backing up the current state."""
    if not _NAME.match(name) or "/" in name:
        raise ValueError("Unknown backup.")
    path = BACKUP_DIR / name
    if not path.exists():
        raise ValueError("That backup no longer exists.")
    create(conn, "before-restore")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        src.backup(conn)
    finally:
        src.close()
