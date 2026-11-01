"""Copies of Penny for friends, checked so none of your data can go along.

Your data never lives inside Penny.app (it's in ~/Library/Application Support/Penny),
but a copy for a friend is still checked file by file before it's saved:
no database, no log, nothing named like your data, and none of your own
details anywhere in any file (your account names and IDs, your SimpleFIN
key). If anything turns up, the copy isn't made.

    python -m app.share <path>   checks an app or folder (the build script does this)
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, db

SQLITE_HEADER = b"SQLite format 3\x00"
RISKY_NAMES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log", ".csv", ".pdf")
MIN_MARKER = 6  # shorter account names ("Visa") are too common to mean anything


def your_details() -> list[bytes]:
    """Things only your data has: account IDs, account names, the SimpleFIN key."""
    path = db.DB_PATH
    if not path.exists():
        return []
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found = set()
        for row in conn.execute("SELECT id, name, nickname FROM accounts"):
            found.update(v for v in row if v and len(v) >= MIN_MARKER)
        key = conn.execute("SELECT value FROM settings WHERE key = 'simplefin_access_url'").fetchone()
        if key and key[0]:
            found.add(key[0])
            # The user:password part of the access URL on its own, too.
            if "@" in key[0]:
                found.add(key[0].split("//", 1)[-1].split("@", 1)[0])
    finally:
        conn.close()
    return sorted(s.encode() for s in found)


def problems(root: Path, details: list[bytes] | None = None) -> list[str]:
    """Anything in `root` that looks like it's yours. Empty means it's safe to share."""
    details = your_details() if details is None else details
    found = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root)
        if path.name.lower().endswith(RISKY_NAMES) or "penny/data" in str(path).lower():
            found.append(f"{rel}: a data, log or document file")
            continue
        data = path.read_bytes()
        if data.startswith(SQLITE_HEADER):
            found.append(f"{rel}: a database")
        elif any(d in data for d in details):
            found.append(f"{rel}: contains one of your account details")
    return found


def this_app() -> Path | None:
    """The Penny.app this is running from, if it's the Mac app."""
    if not getattr(sys, "frozen", False):
        return None
    app = Path(sys.executable).resolve().parents[2]
    return app if app.suffix == ".app" else None


def chip() -> tuple[str, str]:
    return ("apple-silicon", "Macs with Apple chips (M1, M2, M3 and later)") if platform.machine() == "arm64" \
        else ("intel", "Macs with Intel processors")


def make_friend_copy() -> Path:
    """A .dmg of this app on the Desktop, for a friend. Checked before it's saved."""
    app = this_app()
    if app is None:
        raise RuntimeError("Only the Penny app can make a copy. From the code, run scripts/build_mac_app.sh.")
    label, macs = chip()
    out = Path.home() / "Desktop" / f"Penny {__version__} for a friend ({label}).dmg"
    with tempfile.TemporaryDirectory(prefix="penny-share-") as tmp:
        stage = Path(tmp) / "Penny"
        stage.mkdir()
        subprocess.run(["ditto", str(app), str(stage / "Penny.app")], check=True)  # keeps the signature
        found = problems(stage)
        if found:
            raise RuntimeError("Stopped: this copy has something of yours in it, so it wasn't made. "
                               + "; ".join(found[:5]))
        os.symlink("/Applications", stage / "Applications")
        (stage / "How to install Penny.txt").write_text(install_note(macs))
        if out.exists():
            out.unlink()
        subprocess.run(["hdiutil", "create", "-quiet", "-volname", "Penny", "-srcfolder", str(stage),
                        "-format", "UDZO", str(out)], check=True)
    return out


def install_note(macs: str) -> str:
    return f"""How to install Penny
====================

1. Drag Penny onto the Applications folder in this window.
2. Open Penny from Applications (or Launchpad, or Spotlight: Command-Space, type Penny).

If macOS says Penny can't be opened, or that Apple couldn't check it for malware
--------------------------------------------------------------------------------
That's because Penny isn't signed with a paid Apple developer account. It's a one-time step:

  a. Click Done (not Move to Trash).
  b. Open System Settings, then Privacy & Security.
  c. Scroll down to Security and click Open Anyway next to Penny, then confirm.

Penny opens normally from then on.

Is this the right version for your Mac?
---------------------------------------
This copy is for {macs}.
To check yours: Apple menu, then About This Mac. "Chip: Apple M..." means Apple chip;
"Processor: ... Intel" means Intel. If it doesn't match, ask for the other version.

Once Penny is open, it walks you through everything else: trying it with demo data,
connecting your bank, and the rest. There's also a Help button at the top of the window.

This copy has none of the sender's data in it. Your data stays on your Mac. Penny {__version__}.
"""


if __name__ == "__main__":
    # Used by scripts/build_mac_app.sh: exit 1 if anything of yours is in the given app or folder.
    target = Path(sys.argv[1])
    # Check against the Mac app's data (the copy in use), whatever folder this runs from.
    db.DB_PATH = Path(os.environ.get("PENNY_DATA_DIR",
                                     Path.home() / "Library" / "Application Support" / "Penny" / "data")) / "penny.db"
    issues = problems(target)
    for issue in issues:
        print("  " + issue)
    print(f"{target.name}: {'NOT SAFE TO SHARE' if issues else 'checked, none of your data in it'}")
    sys.exit(1 if issues else 0)
