"""Fargate task that invokes AgentCore Runtime and persists the result to S3.

Lambda's 15-minute hard cap made it impossible to host the NDJSON stream
reader for a single deep-research ticker (4 analysts + 2 debate rounds
can blow past 20 minutes). This module runs as an ECS Fargate task
instead, which has effectively no wall-clock limit and is launched per
ticker by Step Functions' Map state (EcsRunTask.sync).

The entry point is ``main()``, invoked by ``python -m
tradingagents.agentcore.task_runner``. All configuration comes from
environment variables so the Fargate task definition can stay minimal
and the per-run fields are passed via ``containerOverrides`` on the
RunTask call.

Expected env vars:

* ``TA_RUN_ID``        — Step Functions execution UUID
* ``TA_TICKER``        — symbol (e.g. "NVDA")
* ``TA_TRADE_DATE``    — "YYYY-MM-DD"
* ``TA_ANALYSTS``      — JSON list (optional; defaults to all four)
* ``TA_DEBATE_ROUNDS`` — int as string (optional)
* ``TA_DEEP_MODEL`` / ``TA_QUICK_MODEL`` — Bedrock inference-profile IDs (optional)
* ``AGENTCORE_RUNTIME_ARN`` — target runtime
* ``TA_CONFIG_BUCKET`` — S3 bucket where the per-ticker result JSON is written
* ``TA_RESULT_KEY_PREFIX`` — defaults to ``runs/``
* ``AGENTCORE_TIMEOUT`` — boto3 read timeout (default 3600s)

The per-ticker result JSON matches the old Lambda's return shape
(``{ticker, status, cost_usd, token_usage, report_key, decision,
duration_seconds, run_id, trade_date, error}``), so the aggregator can
load ``s3://<config_bucket>/runs/<run_id>/<ticker>.json`` and produce
the summary without any other plumbing changes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _require(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _parse_analysts() -> Optional[List[str]]:
    raw = _env("TA_ANALYSTS")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Accept CSV as a friendly fallback.
        return [a.strip() for a in raw.split(",") if a.strip()]
    if isinstance(parsed, list):
        return [str(a) for a in parsed]
    raise ValueError(f"TA_ANALYSTS must be a JSON list, got {raw!r}")


def _iter_ndjson(body: Any) -> Iterable[Dict[str, Any]]:
    if hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping non-JSON stream line: %r", line[:200])
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
            logger.warning("skipping non-JSON stream line: %r", line[:200])


def _write_result(bucket: str, key: str, result: Dict[str, Any]) -> None:
    body = json.dumps(result).encode("utf-8")
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    logger.info("wrote per-ticker result to s3://%s/%s (%d bytes)", bucket, key, len(body))


def run() -> Dict[str, Any]:
    run_id = _require("TA_RUN_ID")
    ticker = _require("TA_TICKER").upper()
    trade_date = _require("TA_TRADE_DATE")
    runtime_arn = _require("AGENTCORE_RUNTIME_ARN")
    config_bucket = _require("TA_CONFIG_BUCKET")
    prefix = _env("TA_RESULT_KEY_PREFIX", "runs/") or "runs/"
    result_key = f"{prefix}{run_id}/{ticker}.json"

    invoke_payload: Dict[str, Any] = {
        "ticker": ticker,
        "trade_date": trade_date,
        "run_id": run_id,
        "debate_rounds": int(_env("TA_DEBATE_ROUNDS", "1") or "1"),
    }
    deep = _env("TA_DEEP_MODEL")
    quick = _env("TA_QUICK_MODEL")
    if deep:
        invoke_payload["deep_model"] = deep
    if quick:
        invoke_payload["quick_model"] = quick
    analysts = _parse_analysts()
    if analysts:
        invoke_payload["analysts"] = analysts

    client = boto3.client(
        "bedrock-agentcore",
        config=Config(
            read_timeout=int(_env("AGENTCORE_TIMEOUT", "3600") or "3600"),
            connect_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )

    started = time.monotonic()
    logger.info("invoking AgentCore Runtime for ticker=%s run_id=%s", ticker, run_id)
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            payload=json.dumps(invoke_payload).encode("utf-8"),
            contentType="application/json",
            accept="application/x-ndjson",
        )
    except Exception as err:  # noqa: BLE001
        duration = time.monotonic() - started
        failure = {
            "ticker": ticker,
            "run_id": run_id,
            "trade_date": trade_date,
            "status": "invoke_failed",
            "duration_seconds": round(duration, 2),
            "error": f"{type(err).__name__}: {err}",
            "cost_usd": 0.0,
            "token_usage": [],
        }
        _write_result(config_bucket, result_key, failure)
        return failure

    body = resp.get("response") or resp.get("payload")

    result_event: Optional[Dict[str, Any]] = None
    heartbeats = 0
    try:
        for event in _iter_ndjson(body):
            etype = event.get("type")
            if etype == "heartbeat":
                heartbeats += 1
                if heartbeats % 6 == 0:
                    logger.info(
                        "agent still running ticker=%s elapsed=%ss",
                        ticker, event.get("elapsed"),
                    )
                continue
            if etype == "result":
                result_event = event
                continue
            logger.warning(
                "unknown stream event ticker=%s type=%r keys=%s",
                ticker, etype, list(event.keys()),
            )
    except Exception as err:  # noqa: BLE001
        duration = time.monotonic() - started
        failure = {
            "ticker": ticker,
            "run_id": run_id,
            "trade_date": trade_date,
            "status": "stream_read_failed",
            "duration_seconds": round(duration, 2),
            "error": f"{type(err).__name__}: {err}",
            "cost_usd": 0.0,
            "token_usage": [],
        }
        _write_result(config_bucket, result_key, failure)
        return failure

    if result_event is None:
        duration = time.monotonic() - started
        failure = {
            "ticker": ticker,
            "run_id": run_id,
            "trade_date": trade_date,
            "status": "no_result_event",
            "duration_seconds": round(duration, 2),
            "error": (
                "Agent stream ended without emitting a result event "
                f"(heartbeats received: {heartbeats})"
            ),
            "cost_usd": 0.0,
            "token_usage": [],
        }
        _write_result(config_bucket, result_key, failure)
        return failure

    result_event.pop("type", None)
    result_event.setdefault("ticker", ticker)
    result_event.setdefault("run_id", run_id)
    result_event.setdefault("trade_date", trade_date)
    result_event.setdefault("status", "success")
    result_event.setdefault("cost_usd", 0.0)
    result_event.setdefault("token_usage", [])
    _write_result(config_bucket, result_key, result_event)
    return result_event


def main() -> int:
    try:
        result = run()
    except Exception as err:  # noqa: BLE001
        logger.exception("task_runner fatal: %s", err)
        return 2
    # Exit non-zero on known failure statuses so Step Functions' EcsRunTask.sync
    # catch branch fires and routes to the error notifier.
    status = str(result.get("status", "")).lower()
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
