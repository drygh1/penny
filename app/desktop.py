"""Penny as a Mac app: the server runs inside the app and shows in its own window.

PyInstaller builds this into Penny.app (see scripts/build_mac_app.sh). Quitting
the app stops the server, so there's no separate "Stop Penny" step.

Data lives in ~/Library/Application Support/Penny/data, next to the backups,
because an app can't keep data inside itself. The first time the app runs, it
copies the database from an earlier Penny folder if it finds one; the original
is left where it was.
"""
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

SUPPORT = Path.home() / "Library" / "Application Support" / "Penny"
DATA_DIR = Path(os.environ.get("PENNY_DATA_DIR", SUPPORT / "data"))
# Set before app.db is imported, which reads it.
os.environ["PENNY_DATA_DIR"] = str(DATA_DIR)
os.environ["PENNY_DESKTOP"] = "1"

log = logging.getLogger("penny")


def _earlier_data_dirs() -> list[Path]:
    """Where a Penny run from its folder (./run.sh or the old launcher app) kept its data."""
    found = []
    # The old launcher made by scripts/make_app.sh names the folder it starts.
    launcher = Path.home() / "Applications" / "Penny.app" / "Contents" / "MacOS" / "Penny"
    try:
        if m := re.search(r'exec "(.+)/scripts/launch\.sh"', launcher.read_text()):
            found.append(Path(m[1]) / "data")
    except (OSError, UnicodeDecodeError):
        pass  # not there, or it's this app's own binary
    if not getattr(sys, "frozen", False):  # running from source: this checkout's own data
        found.append(Path(__file__).resolve().parent.parent / "data")
    found += [Path.home() / "penny" / "data", Path.home() / "Projects" / "penny" / "data"]
    return [d for d in dict.fromkeys(found) if (d / "penny.db").is_file() and d.resolve() != DATA_DIR.resolve()]


def copy_earlier_data():
    """Bring over the database from an earlier Penny, once. Copies, never moves."""
    target = DATA_DIR / "penny.db"
    if target.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    for source in _earlier_data_dirs():
        # SQLite's backup API gives a consistent copy even if the old Penny is still running.
        src = sqlite3.connect(f"file:{source / 'penny.db'}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        os.chmod(target, 0o600)
        log.info("Copied your data from %s", source)
        return


def _dark_mode() -> bool:
    """So the window doesn't flash white before the page draws (colors match style.css)."""
    try:
        from AppKit import NSApplication, NSAppearanceNameAqua, NSAppearanceNameDarkAqua
        appearance = NSApplication.sharedApplication().effectiveAppearance()
        return appearance.bestMatchFromAppearancesWithNames_([NSAppearanceNameAqua, NSAppearanceNameDarkAqua]) \
            == NSAppearanceNameDarkAqua
    except Exception:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA_DIR / "penny.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    copy_earlier_data()

    import uvicorn
    import webview
    from app.main import app

    port = free_port()
    # Plain asyncio and h11, so the bundle needs none of uvicorn's optional extras.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", http="h11",
                                           ws="none", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):  # wait up to 10 seconds for the server
        try:
            urllib.request.urlopen(f"{url}/api/status", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    # text_select: let people copy amounts and descriptions, as in a browser.
    webview.create_window("Penny", url, width=1280, height=860, min_size=(900, 600), text_select=True,
                          background_color="#1a1a19" if _dark_mode() else "#fcfcfb")
    # Not private, so the page's small preferences (like the chart view) last between launches.
    webview.start(private_mode=False, storage_path=str(SUPPORT / "webview"))
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
