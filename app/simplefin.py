"""Minimal SimpleFIN client. Protocol: https://www.simplefin.org/protocol.html

Flow:
  1. You create a Setup Token at https://beta-bridge.simplefin.org (or bridge.simplefin.org).
  2. We base64-decode it into a one-time claim URL and POST to it.
  3. The response is a long-lived Access URL with credentials embedded
     (https://user:pass@host/path). We store that and use it for every sync.
"""
import base64
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx


class SimpleFINError(Exception):
    pass


def claim_setup_token(token: str) -> str:
    try:
        claim_url = base64.b64decode(token.strip(), validate=True).decode()
    except Exception:
        raise SimpleFINError("That doesn't look like a SimpleFIN setup token.")
    if not claim_url.startswith("https://"):
        raise SimpleFINError("That doesn't look like a SimpleFIN setup token.")

    resp = httpx.post(claim_url, headers={"Content-Length": "0"}, timeout=30)
    if resp.status_code == 403:
        raise SimpleFINError(
            "SimpleFIN rejected this token. It may already have been used. "
            "Create a new setup token and try again."
        )
    resp.raise_for_status()
    access_url = resp.text.strip()
    if not access_url.startswith("https://"):
        raise SimpleFINError("SimpleFIN returned an unexpected response.")
    return access_url


def fetch_accounts(access_url: str, start: int, end: int | None = None) -> dict:
    """GET /accounts for a date window. Returns the parsed JSON body."""
    parts = urlsplit(access_url)
    auth = (unquote(parts.username or ""), unquote(parts.password or ""))
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    base = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))

    params = {"start-date": start, "pending": 1}
    if end is not None:
        params["end-date"] = end

    resp = httpx.get(f"{base}/accounts", params=params, auth=auth, timeout=60)
    if resp.status_code == 402:
        raise SimpleFINError("Your SimpleFIN subscription needs payment.")
    if resp.status_code == 403:
        raise SimpleFINError("SimpleFIN access was revoked. Reconnect with a new setup token.")
    resp.raise_for_status()
    return resp.json()
