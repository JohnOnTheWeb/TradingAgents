"""Token loading — Secrets Manager in prod, .brokerage-tokens.json locally.

Secrets shape (both Schwab and Tastytrade):
    {"refresh_token": "...", "client_id": "...", "client_secret": "..."}

Production secret IDs (override via BROKERAGE_SCHWAB_SECRET / ..._TASTYTRADE_SECRET env):
    brokerage/schwab-oauth
    brokerage/tastytrade-oauth

Local fallback: ``./.brokerage-tokens.json`` with top-level keys ``schwab`` / ``tastytrade``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, TypedDict

_logger = logging.getLogger(__name__)


class BrokerCreds(TypedDict):
    refresh_token: str
    client_id: str
    client_secret: str


def _from_secrets_manager(secret_id: str) -> Optional[BrokerCreds]:
    try:
        import boto3
    except ImportError:
        return None
    try:
        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_id)
    except Exception as err:  # noqa: BLE001
        _logger.warning("Secrets Manager lookup for %s failed: %s", secret_id, err)
        return None
    raw = resp.get("SecretString") or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _logger.error("Secret %s is not JSON", secret_id)
        return None
    if not isinstance(parsed, dict):
        return None
    required = {"refresh_token", "client_id", "client_secret"}
    if not required.issubset(parsed):
        _logger.error("Secret %s missing required keys: have %s", secret_id, list(parsed))
        return None
    return BrokerCreds(
        refresh_token=str(parsed["refresh_token"]),
        client_id=str(parsed["client_id"]),
        client_secret=str(parsed["client_secret"]),
    )


def _from_local_file(broker: str) -> Optional[BrokerCreds]:
    path = Path(os.environ.get("BROKERAGE_TOKENS_FILE", ".brokerage-tokens.json"))
    if not path.exists():
        return None
    try:
        with path.open() as fh:
            parsed = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        _logger.error("Failed to read %s: %s", path, err)
        return None
    entry = parsed.get(broker)
    if not isinstance(entry, dict):
        return None
    required = {"refresh_token", "client_id", "client_secret"}
    if not required.issubset(entry):
        return None
    return BrokerCreds(
        refresh_token=str(entry["refresh_token"]),
        client_id=str(entry["client_id"]),
        client_secret=str(entry["client_secret"]),
    )


def load_schwab_creds() -> Optional[BrokerCreds]:
    secret_id = os.environ.get("BROKERAGE_SCHWAB_SECRET", "brokerage/schwab-oauth")
    return _from_secrets_manager(secret_id) or _from_local_file("schwab")


def load_tastytrade_creds() -> Optional[BrokerCreds]:
    secret_id = os.environ.get("BROKERAGE_TASTYTRADE_SECRET", "brokerage/tastytrade-oauth")
    return _from_secrets_manager(secret_id) or _from_local_file("tastytrade")
