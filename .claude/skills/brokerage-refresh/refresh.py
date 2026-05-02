#!/usr/bin/env python3
"""Refresh Schwab + Tastytrade OAuth refresh tokens.

Usage:
    python refresh.py schwab
    python refresh.py tastytrade
    python refresh.py both

Reads existing credentials from Secrets Manager (brokerage/schwab-oauth,
brokerage/tastytrade-oauth) OR from env vars when setting up fresh:
    SCHWAB_CLIENT_ID, SCHWAB_CLIENT_SECRET, SCHWAB_REDIRECT_URI
    TASTYTRADE_CLIENT_ID, TASTYTRADE_CLIENT_SECRET

Writes new refresh tokens to both Secrets Manager and .brokerage-tokens.json.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Dict, Optional

SCHWAB_SECRET_ID = os.environ.get("BROKERAGE_SCHWAB_SECRET", "brokerage/schwab-oauth")
TASTYTRADE_SECRET_ID = os.environ.get(
    "BROKERAGE_TASTYTRADE_SECRET", "brokerage/tastytrade-oauth"
)
LOCAL_FILE = Path(
    os.environ.get("BROKERAGE_TOKENS_FILE", ".brokerage-tokens.json")
).resolve()


def _load_secret(secret_id: str) -> Dict[str, str]:
    try:
        import boto3
    except ImportError:
        return {}
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
        raw = resp.get("SecretString") or ""
        return json.loads(raw) if raw else {}
    except Exception as err:  # noqa: BLE001
        print(f"  (no existing {secret_id}: {err})", file=sys.stderr)
        return {}


def _put_secret(secret_id: str, payload: Dict[str, str]) -> None:
    import boto3

    client = boto3.client("secretsmanager")
    try:
        client.put_secret_value(
            SecretId=secret_id, SecretString=json.dumps(payload)
        )
    except client.exceptions.ResourceNotFoundException:
        print(
            f"  Secret {secret_id} not found. Create it first: "
            f"aws secretsmanager create-secret --name {secret_id} --secret-string '{{}}'",
            file=sys.stderr,
        )
        raise


def _write_local(broker: str, payload: Dict[str, str]) -> None:
    existing: Dict[str, Dict[str, str]] = {}
    if LOCAL_FILE.exists():
        try:
            existing = json.loads(LOCAL_FILE.read_text())
        except json.JSONDecodeError:
            pass
    existing[broker] = payload
    LOCAL_FILE.write_text(json.dumps(existing, indent=2))
    print(f"  → wrote {broker} creds to {LOCAL_FILE}")


def refresh_schwab() -> None:
    print("\n=== Schwab ===")
    existing = _load_secret(SCHWAB_SECRET_ID)
    client_id = os.environ.get("SCHWAB_CLIENT_ID") or existing.get("client_id")
    client_secret = os.environ.get("SCHWAB_CLIENT_SECRET") or existing.get("client_secret")
    redirect_uri = os.environ.get("SCHWAB_REDIRECT_URI") or existing.get("redirect_uri")
    if not (client_id and client_secret and redirect_uri):
        sys.exit(
            "Missing Schwab OAuth app config. Set SCHWAB_CLIENT_ID, "
            "SCHWAB_CLIENT_SECRET, SCHWAB_REDIRECT_URI or populate "
            f"{SCHWAB_SECRET_ID} first."
        )

    auth_url = (
        "https://api.schwabapi.com/v1/oauth/authorize"
        f"?response_type=code&client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
    )
    print(f"Open this URL and approve access:\n\n  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    print(
        "After approving, the browser redirects to your callback URL with "
        "?code=... in the query string.\n"
    )
    raw_code = input("Paste the full callback URL (or just the code value): ").strip()
    if raw_code.startswith("http"):
        parsed = urllib.parse.urlparse(raw_code)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0]
    else:
        code = urllib.parse.unquote(raw_code)
    if not code:
        sys.exit("No code captured.")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.schwabapi.com/v1/oauth/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token_body = json.loads(resp.read().decode())

    refresh_token = token_body["refresh_token"]
    payload = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    _put_secret(SCHWAB_SECRET_ID, payload)
    _write_local("schwab", payload)
    # Schwab refresh tokens are 7 days from issuance.
    expires = dt.datetime.utcnow() + dt.timedelta(days=7)
    print(f"  Schwab refresh token good until ~{expires.isoformat(timespec='minutes')}Z")


def refresh_tastytrade() -> None:
    print("\n=== Tastytrade ===")
    existing = _load_secret(TASTYTRADE_SECRET_ID)
    client_id = os.environ.get("TASTYTRADE_CLIENT_ID") or existing.get("client_id")
    client_secret = os.environ.get("TASTYTRADE_CLIENT_SECRET") or existing.get("client_secret")
    refresh_token = os.environ.get("TASTYTRADE_REFRESH_TOKEN") or existing.get("refresh_token")
    if not (client_id and client_secret and refresh_token):
        sys.exit(
            "Missing Tastytrade OAuth config. Set TASTYTRADE_CLIENT_ID, "
            "TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN or populate "
            f"{TASTYTRADE_SECRET_ID} first (run Tastytrade's web OAuth flow once)."
        )

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.tastyworks.com/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token_body = json.loads(resp.read().decode())
    new_refresh = token_body.get("refresh_token") or refresh_token
    payload = {
        "refresh_token": new_refresh,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    _put_secret(TASTYTRADE_SECRET_ID, payload)
    _write_local("tastytrade", payload)
    print("  Tastytrade refresh token minted. Re-run when you want to rotate.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("target", choices=["schwab", "tastytrade", "both"], nargs="?", default="both")
    args = p.parse_args()
    if args.target in ("schwab", "both"):
        refresh_schwab()
    if args.target in ("tastytrade", "both"):
        refresh_tastytrade()
    print("\nDone.")


if __name__ == "__main__":
    main()
