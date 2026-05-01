"""Step 2 of the state machine: call AgentCore Runtime for a single ticker.

Input (from Step Functions Map iteration):
    {
        "run_id": "<uuid>",
        "trade_date": "2026-04-30",
        "deep_model": "...",
        "quick_model": "...",
        "ticker": {"symbol": "NVDA", "analysts": [...], "debate_rounds": 1, ...}
    }

Output: whatever ``/invocations`` returned plus the ticker symbol for joining
in the aggregator step.

Environment:
    AGENTCORE_RUNTIME_ARN  — ARN of the AgentCore Runtime endpoint
    AGENTCORE_TIMEOUT      — socket read timeout in seconds (default 900)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

import boto3
from botocore.config import Config

_timeout = int(os.environ.get("AGENTCORE_TIMEOUT", "900"))
_client = boto3.client(
    "bedrock-agentcore",
    config=Config(
        read_timeout=_timeout,
        connect_timeout=10,
        retries={"max_attempts": 2, "mode": "standard"},
    ),
)


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    runtime_arn = os.environ["AGENTCORE_RUNTIME_ARN"]

    ticker_cfg = event.get("ticker") or {}
    symbol = ticker_cfg.get("symbol")
    if not symbol:
        raise ValueError("Invoke event missing ticker.symbol")

    payload: Dict[str, Any] = {
        "ticker": symbol,
        "trade_date": event["trade_date"],
        "run_id": event["run_id"],
        "debate_rounds": ticker_cfg.get("debate_rounds", 1),
        "deep_model": ticker_cfg.get("deep_model") or event.get("deep_model"),
        "quick_model": ticker_cfg.get("quick_model") or event.get("quick_model"),
    }
    if ticker_cfg.get("analysts"):
        payload["analysts"] = list(ticker_cfg["analysts"])

    started = time.monotonic()
    try:
        resp = _client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
    except Exception as err:  # noqa: BLE001
        duration = time.monotonic() - started
        return {
            "ticker": symbol,
            "status": "invoke_failed",
            "duration_seconds": round(duration, 2),
            "error": f"{type(err).__name__}: {err}",
            "cost_usd": 0.0,
            "token_usage": [],
        }

    # invoke_agent_runtime returns a streaming body; read it all.
    body_stream = resp.get("response") or resp.get("payload")
    raw = body_stream.read() if hasattr(body_stream, "read") else body_stream
    try:
        result = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except json.JSONDecodeError as err:
        duration = time.monotonic() - started
        return {
            "ticker": symbol,
            "status": "bad_response",
            "duration_seconds": round(duration, 2),
            "error": f"non-JSON response: {err}",
            "cost_usd": 0.0,
            "token_usage": [],
        }

    # Normalize — the agent always includes ticker/status/cost_usd, but be defensive.
    result.setdefault("ticker", symbol)
    result.setdefault("status", "success")
    result.setdefault("token_usage", [])
    result.setdefault("cost_usd", 0.0)
    return result
