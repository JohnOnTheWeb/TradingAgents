"""Step 2 of the state machine: call AgentCore Runtime for a single ticker.

Input (from Step Functions Map iteration):
    {
        "run_id": "<uuid>",
        "trade_date": "2026-04-30",
        "deep_model": "...",
        "quick_model": "...",
        "ticker": {"symbol": "NVDA", "analysts": [...], "debate_rounds": 1, ...}
    }

Output: whatever ``/invocations`` returned in its final ``result`` event,
plus the ticker symbol for joining in the aggregator step.

AgentCore Runtime streams NDJSON back (``application/x-ndjson``); each line
is either a ``heartbeat`` event (discarded) or the final ``result`` event
(kept). Streaming is what keeps AgentCore's 15-min idle timer from firing.

Environment:
    AGENTCORE_RUNTIME_ARN  — ARN of the AgentCore Runtime endpoint
    AGENTCORE_TIMEOUT      — socket read timeout in seconds (default 900)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterable

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_timeout = int(os.environ.get("AGENTCORE_TIMEOUT", "900"))
_client = boto3.client(
    "bedrock-agentcore",
    config=Config(
        read_timeout=_timeout,
        connect_timeout=10,
        retries={"max_attempts": 2, "mode": "standard"},
    ),
)


def _iter_ndjson_lines(body: Any) -> Iterable[Dict[str, Any]]:
    """Yield parsed JSON objects from a streaming NDJSON body.

    botocore returns the ``payload`` / ``response`` field as a
    StreamingBody. It supports ``.iter_lines()`` when the server sends
    chunked responses, which is what our FastAPI StreamingResponse does.
    Fall back to reading everything if iter_lines isn't available.
    """
    if hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping non-JSON line: %r", line[:200])
        return

    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping non-JSON line: %r", line[:200])


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
            accept="application/x-ndjson",
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

    body_stream = resp.get("response") or resp.get("payload")

    result_event: Dict[str, Any] | None = None
    heartbeat_count = 0
    try:
        for event in _iter_ndjson_lines(body_stream):
            event_type = event.get("type")
            if event_type == "heartbeat":
                heartbeat_count += 1
                # Log every 6th heartbeat (~1 per minute) to keep CloudWatch tidy.
                if heartbeat_count % 6 == 0:
                    logger.info(
                        "agent still running ticker=%s elapsed=%ss",
                        symbol, event.get("elapsed"),
                    )
                continue
            if event_type == "result":
                result_event = event
                # Keep reading in case the server sent trailing data, but
                # there shouldn't be any more events after ``result``.
                continue
            logger.warning("unknown stream event type=%r keys=%s",
                           event_type, list(event.keys()))
    except Exception as err:  # noqa: BLE001
        duration = time.monotonic() - started
        return {
            "ticker": symbol,
            "status": "stream_read_failed",
            "duration_seconds": round(duration, 2),
            "error": f"{type(err).__name__}: {err}",
            "cost_usd": 0.0,
            "token_usage": [],
        }

    if result_event is None:
        duration = time.monotonic() - started
        return {
            "ticker": symbol,
            "status": "no_result_event",
            "duration_seconds": round(duration, 2),
            "error": (
                "Agent stream ended without emitting a result event "
                f"(heartbeats received: {heartbeat_count})"
            ),
            "cost_usd": 0.0,
            "token_usage": [],
        }

    # Drop the ``type`` wrapper so the aggregator sees the flat per-ticker fields.
    result_event.pop("type", None)
    result_event.setdefault("ticker", symbol)
    result_event.setdefault("status", "success")
    result_event.setdefault("token_usage", [])
    result_event.setdefault("cost_usd", 0.0)
    return result_event
