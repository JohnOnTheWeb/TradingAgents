"""Step 1 of the state machine: resolve the run config.

Two accepted input shapes:

* Inline (from the web API Lambda):
    ``{"tickers": [{"symbol": "NVDA", ...}, ...],
       "trade_date": "2026-05-02", "deep_model": "...", "quick_model": "...",
       "run_id": "<uuid>"}``

* S3-backed (from EventBridge Scheduler):
    ``{"config_key": "watchlist.json"}``

Output: {
    "run_id":     "<uuid>",
    "trade_date": "2026-04-30",
    "deep_model": "us.anthropic.claude-opus-4-7",
    "quick_model":"us.anthropic.claude-sonnet-4-5-...",
    "tickers": [
        {"symbol": "NVDA", "analysts": [...], "debate_rounds": 1},
        ...
    ]
}

Environment:
    TRADINGAGENTS_CONFIG_BUCKET  — S3 bucket holding watchlist.json
    DEFAULT_DEEP_MODEL           — fallback if config omits it
    DEFAULT_QUICK_MODEL          — fallback if config omits it
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List

import boto3

_s3 = boto3.client("s3")


def _resolve_date(value: Any) -> str:
    if not value or value == "today":
        return date.today().isoformat()
    if isinstance(value, str):
        # Validate ISO format; raise clearly if malformed
        datetime.strptime(value, "%Y-%m-%d")
        return value
    raise ValueError(f"Unsupported trade_date: {value!r}")


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    if event.get("tickers"):
        config = {
            "tickers": event["tickers"],
            "date": event.get("trade_date"),
            "deep_model": event.get("deep_model"),
            "quick_model": event.get("quick_model"),
        }
    else:
        bucket = os.environ["TRADINGAGENTS_CONFIG_BUCKET"]
        key = event.get("config_key") or "watchlist.json"
        obj = _s3.get_object(Bucket=bucket, Key=key)
        config = json.loads(obj["Body"].read().decode("utf-8"))

    tickers_raw = config.get("tickers") or []
    if not tickers_raw:
        raise ValueError("no tickers provided (inline or in S3 watchlist)")

    deep = config.get("deep_model") or os.environ.get(
        "DEFAULT_DEEP_MODEL", "us.anthropic.claude-opus-4-7"
    )
    quick = config.get("quick_model") or os.environ.get(
        "DEFAULT_QUICK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )

    trade_date = _resolve_date(config.get("date") or event.get("trade_date"))
    run_id = event.get("run_id") or str(uuid.uuid4())

    tickers: List[Dict[str, Any]] = []
    for raw in tickers_raw:
        if isinstance(raw, str):
            tickers.append({"symbol": raw.upper()})
            continue
        sym = raw.get("symbol") or raw.get("ticker")
        if not sym:
            raise ValueError(f"Ticker entry missing symbol: {raw!r}")
        entry = {"symbol": str(sym).upper()}
        if "analysts" in raw:
            entry["analysts"] = list(raw["analysts"])
        if "debate_rounds" in raw:
            entry["debate_rounds"] = int(raw["debate_rounds"])
        if "deep_model" in raw:
            entry["deep_model"] = raw["deep_model"]
        if "quick_model" in raw:
            entry["quick_model"] = raw["quick_model"]
        tickers.append(entry)

    return {
        "run_id": run_id,
        "trade_date": trade_date,
        "deep_model": deep,
        "quick_model": quick,
        "tickers": tickers,
        "emitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
