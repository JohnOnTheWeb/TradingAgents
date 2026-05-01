"""Persist TradingAgents reports to the md-store MCP server.

md-store is an HTTP-based MCP server that fronts an S3 bucket. We call its
``write_file`` tool via a single JSON-RPC POST. The bearer token is read
from AWS Secrets Manager at cold start (cached for the life of the process).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://jjjtiltcja.execute-api.us-east-1.amazonaws.com/prod/mcp/v2"
_DEFAULT_AGENT_ID = "tauric-traders"
_REPORT_PREFIX = "TauricTraders/"

_cached_bearer: Optional[str] = None


class ReportWriteError(RuntimeError):
    """Raised when md-store rejects a write or the HTTP call fails."""


def _load_bearer_token() -> str:
    """Resolve the md-store bearer token.

    Order:
    1. ``MD_STORE_BEARER`` env var (useful for local testing).
    2. ``MD_STORE_SECRET_ID`` (+ optional ``MD_STORE_SECRET_JSON_KEY``) —
       fetched from AWS Secrets Manager.  This is how AgentCore supplies it.
    """
    global _cached_bearer
    if _cached_bearer:
        return _cached_bearer

    inline = os.environ.get("MD_STORE_BEARER", "").strip()
    if inline:
        _cached_bearer = inline
        return inline

    secret_id = os.environ.get("MD_STORE_SECRET_ID", "").strip()
    if not secret_id:
        raise ReportWriteError(
            "md-store bearer not configured: set MD_STORE_BEARER or "
            "MD_STORE_SECRET_ID"
        )

    try:
        import boto3  # lazy import so local runs without boto3 still work
    except ImportError as err:
        raise ReportWriteError(
            "boto3 is required to resolve MD_STORE_SECRET_ID"
        ) from err

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString") or ""

    # If the secret is JSON, pull out the field indicated by MD_STORE_SECRET_JSON_KEY
    # (default "bearer"); otherwise treat the whole SecretString as the token.
    json_key = os.environ.get("MD_STORE_SECRET_JSON_KEY", "bearer")
    token = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and json_key in parsed:
            token = str(parsed[json_key])
    except json.JSONDecodeError:
        pass

    token = token.strip()
    if not token:
        raise ReportWriteError(f"Secret {secret_id} did not contain a bearer token")
    _cached_bearer = token
    return token


def _endpoint() -> str:
    return os.environ.get("MD_STORE_ENDPOINT", _DEFAULT_ENDPOINT)


def _agent_id() -> str:
    return os.environ.get("MD_STORE_AGENT_ID", _DEFAULT_AGENT_ID)


def write_report(filename: str, content: str, *, overwrite: bool = True) -> str:
    """Write ``content`` to ``TauricTraders/<filename>`` in md-store.

    Returns the full md-store key. Overwrites unconditionally by default
    (no optimistic-concurrency etag).
    """
    if "/" in filename or filename.startswith("."):
        raise ValueError(f"Illegal report filename: {filename!r}")
    key = f"{_REPORT_PREFIX}{filename}"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "write_file",
            "arguments": {"key": key, "content": content},
        },
    }
    # Only include if_match_etag when the caller explicitly opts into
    # optimistic concurrency. Default (overwrite=True) omits the field so
    # the server takes the unconditional-overwrite path.
    if not overwrite:
        raise ValueError(
            "Non-overwriting writes are not supported by this helper"
        )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_load_bearer_token()}",
        "X-Agent-Id": _agent_id(),
    }
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise ReportWriteError(
            f"md-store write_file failed: HTTP {err.code} {detail}"
        ) from err
    except urllib.error.URLError as err:
        raise ReportWriteError(f"md-store unreachable: {err.reason}") from err

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as err:
        raise ReportWriteError(f"md-store returned non-JSON: {body[:200]}") from err

    if "error" in parsed:
        raise ReportWriteError(f"md-store rpc error: {parsed['error']}")

    logger.info("Wrote md-store key %s (%d bytes)", key, len(content))
    return key


def report_filename(ticker: str, trade_date: str) -> str:
    """Convention: ``<TICKER>_<YYYY-MM-DD>.md``."""
    return f"{ticker.upper()}_{trade_date}.md"


def summary_filename(trade_date: str) -> str:
    """Convention: ``_summary_<YYYY-MM-DD>.md`` (leading underscore sorts it first)."""
    return f"_summary_{trade_date}.md"
