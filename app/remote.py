"""Use Penny on your iPhone or other devices, privately, over NordVPN Meshnet or Tailscale.

Penny normally listens only on this Mac (127.0.0.1). With sharing turned on in
Settings, it also listens on one more address: this Mac's address on a private
network of your own devices (NordVPN Meshnet or Tailscale). Nothing else:

- Those networks only connect devices you've linked to your own account, and
  encrypt everything between them. Their addresses (100.64.0.0/10) can't be
  reached from the internet or from your Wi-Fi network.
- You confirm the address against what the NordVPN (or Tailscale) app shows.
  Penny doesn't pick one on its own, since an ordinary VPN tunnel can use a
  similar-looking address.
- Penny listens on it only while it's really on this Mac; if Meshnet is off,
  sharing waits quietly and nothing else about Penny changes.
- If Tailscale Funnel (which publishes things to the internet) is on, Penny
  won't share.
- Every request from another device needs you to sign in with your passcode
  (auth.py). Requests from this Mac never do.

Sharing is off unless you turn it on, and Penny on this Mac works the same either way.
"""
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from . import db

log = logging.getLogger("penny.remote")

PORT = int(os.environ.get("PENNY_REMOTE_PORT", "8787"))  # fixed, so a phone's home-screen icon keeps working
CHECK_EVERY = 30  # seconds between checks that the address is still here
PRIVATE_RANGE = ipaddress.ip_network("100.64.0.0/10")  # used by Meshnet and Tailscale; never on the internet


def private_addresses() -> list[str]:
    """This Mac's addresses on Meshnet/Tailscale-style tunnels (utun interfaces, 100.64.0.0/10)."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found, iface = [], ""
    for line in out.splitlines():
        if m := re.match(r"^(\S+):", line):
            iface = m[1]
        elif iface.startswith("utun") and (m := re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)):
            if ipaddress.ip_address(m[1]) in PRIVATE_RANGE:
                found.append(m[1])
    return found


def funnel_on() -> bool:
    """True if Tailscale Funnel is publishing anything from this Mac to the internet.
    Only Tailscale has Funnel; without Tailscale installed there's nothing to check."""
    cli = next((p for p in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                            "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale")
                if os.access(p, os.X_OK)), None) or shutil.which("tailscale")
    if not cli:
        return False
    try:
        out = subprocess.run([cli, "funnel", "status", "--json"], capture_output=True, text=True, timeout=4)
        config = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return True  # can't tell, so assume it might be
    return bool(config.get("AllowFunnel")) and any(config["AllowFunnel"].values())


# ---------- the listener ----------

_state: dict = {"server": None, "thread": None, "ip": None, "error": None, "awake": None, "app": None}
_lock = threading.Lock()


def address(conn) -> str | None:
    return db.get_setting(conn, "remote_address")


def enabled(conn) -> bool:
    return (db.get_setting(conn, "remote_enabled") == "1" and address(conn) is not None
            and db.get_setting(conn, "remote_passcode") is not None)


def serving_ip() -> str | None:
    return _state["ip"] if _state["server"] else None


def allowed_hosts() -> set[str]:
    """The only Host a phone may use to reach Penny: the shared address."""
    ip = serving_ip()
    return {ip} if ip else set()


def url() -> str | None:
    ip = serving_ip()
    return f"http://{ip}:{PORT}" if ip else None


def status(conn) -> dict:
    return {
        "enabled": db.get_setting(conn, "remote_enabled") == "1",
        "address": address(conn),
        "addresses": private_addresses(),
        "has_passcode": db.get_setting(conn, "remote_passcode") is not None,
        "listening": _state["server"] is not None,
        "url": url(),
        "error": _state["error"],
        "keep_awake": db.get_setting(conn, "remote_keep_awake") != "0",
        "devices": conn.execute("SELECT COUNT(*) FROM remote_sessions").fetchone()[0],
        "port": PORT,
    }


def _stop_server():
    server, thread = _state["server"], _state["thread"]
    if server:
        server.should_exit = True
        thread.join(timeout=5)
        log.info("Stopped sharing Penny")
    _state.update(server=None, thread=None, ip=None)


def _keep_awake(on: bool):
    """While sharing, stop this Mac from sleeping while it's plugged in (caffeinate -s),
    so the phone can reach it. On battery it sleeps as usual."""
    proc = _state["awake"]
    if on and (proc is None or proc.poll() is not None):
        try:
            _state["awake"] = subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
        except OSError:
            _state["awake"] = None
    elif not on and proc is not None:
        proc.terminate()
        _state["awake"] = None


def refresh(app=None):
    """Start, stop or move the listener to match the settings and the network."""
    from uvicorn import Config, Server
    app = app or _state["app"]
    conn = db.connect()
    try:
        want, ip = enabled(conn), address(conn)
        keep_awake = db.get_setting(conn, "remote_keep_awake") != "0"
    finally:
        conn.close()
    with _lock:
        if not want and _state["server"] is None:
            _state["error"] = None
            _keep_awake(False)
            return  # sharing is off: nothing to check, nothing running
        here = ip in private_addresses()
        funnel = funnel_on() if want else False
        _state["error"] = None
        if want and not here:
            _state["error"] = f"Waiting for {ip}. Is Meshnet (or Tailscale) on?"
        elif want and funnel:
            _state["error"] = "Tailscale Funnel is on, which can publish things to the internet. Turn it off to share Penny."
        ok = want and here and not funnel
        if not ok or _state["ip"] != ip:
            _stop_server()
        if ok and _state["server"] is None:
            config = Config(app, host=ip, port=PORT, loop="asyncio", http="h11", ws="none",
                            lifespan="off", log_config=None, access_log=False)
            server = Server(config)
            thread = threading.Thread(target=server.run, daemon=True, name="penny-remote")
            thread.start()
            for _ in range(50):
                if server.started or not thread.is_alive():
                    break
                time.sleep(0.1)
            if server.started:
                _state.update(server=server, thread=thread, ip=ip)
                log.info("Sharing Penny at %s", url())
            else:
                server.should_exit = True
                _state["error"] = f"Couldn't use port {PORT} on {ip}. Is another app using it?"
        _keep_awake(bool(_state["server"] and keep_awake))


def start_watcher(app):
    """Check every 30 seconds, so sharing follows Meshnet going on and off. When
    sharing is off, each check is just a settings lookup."""
    _state["app"] = app

    def loop():
        while True:
            try:
                refresh(app)
            except Exception:
                log.exception("Checking the sharing address failed")
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, daemon=True, name="penny-remote-watch").start()


def stop():
    with _lock:
        _stop_server()
        _keep_awake(False)
