"""Web API → StartExecution bridge.

Fronted by API Gateway HTTP API with AWS_IAM authorizer (SigV4).
Caller (the ta-run skill) passes JSON:

    {
      "tickers": [
        {"symbol": "NVDA",
         "analysts": ["market", "news", "fundamentals"],
         "debate_rounds": 2},
        {"symbol": "AAPL", "analysts": ["market"], "debate_rounds": 1}
      ],
      "trade_date": "today",           // optional; defaults to today (UTC)
      "deep_model": "...",              // optional override
      "quick_model": "..."              // optional override
    }

We hand that directly to Step Functions, which passes it to the
updated ``get_config`` Lambda. Response is intentionally minimal —
``{"executionArn": "...", "run_id": "..."}`` — so the caller can poll
``GET /runs/{executionArn}`` (see run_status handler).

Environment:
    STATE_MACHINE_ARN  — tradingagents-run state machine
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime
from typing import Any, Dict, List

import boto3

_sfn = boto3.client("stepfunctions")

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_VALID_ANALYSTS = {"market", "social", "news", "fundamentals"}


def _bad_request(msg: str) -> Dict[str, Any]:
    return {
        "statusCode": 400,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": msg}),
    }


def _ok(body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body")
    if raw is None:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    raise ValueError(f"unsupported body type: {type(raw).__name__}")


def _validate_tickers(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("'tickers' must be a non-empty array")
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"tickers[{i}] must be an object")
        sym = entry.get("symbol") or entry.get("ticker")
        if not sym or not isinstance(sym, str):
            raise ValueError(f"tickers[{i}] missing 'symbol'")
        sym = sym.strip().upper()
        if not _SYMBOL_RE.match(sym):
            raise ValueError(f"tickers[{i}] invalid symbol: {sym!r}")
        item: Dict[str, Any] = {"symbol": sym}

        analysts = entry.get("analysts")
        if analysts is not None:
            if not isinstance(analysts, list) or not analysts:
                raise ValueError(
                    f"tickers[{i}].analysts must be a non-empty array"
                )
            bad = [a for a in analysts if a not in _VALID_ANALYSTS]
            if bad:
                raise ValueError(
                    f"tickers[{i}].analysts has unknown values: {bad}"
                )
            item["analysts"] = list(analysts)

        debate = entry.get("debate_rounds")
        if debate is not None:
            if not isinstance(debate, int) or debate < 0 or debate > 5:
                raise ValueError(
                    f"tickers[{i}].debate_rounds must be int in [0, 5]"
                )
            item["debate_rounds"] = debate

        for opt in ("deep_model", "quick_model"):
            if entry.get(opt):
                item[opt] = str(entry[opt])

        out.append(item)
    return out


def _resolve_date(value: Any) -> str:
    if not value or value == "today":
        return date.today().isoformat()
    if isinstance(value, str):
        datetime.strptime(value, "%Y-%m-%d")
        return value
    raise ValueError(f"unsupported trade_date: {value!r}")


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    state_machine_arn = os.environ["STATE_MACHINE_ARN"]

    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, ValueError) as err:
        return _bad_request(f"invalid JSON body: {err}")

    try:
        tickers = _validate_tickers(body.get("tickers"))
        trade_date = _resolve_date(body.get("trade_date"))
    except ValueError as err:
        return _bad_request(str(err))

    run_id = str(uuid.uuid4())
    sfn_input: Dict[str, Any] = {
        "run_id": run_id,
        "trade_date": trade_date,
        "tickers": tickers,
    }
    for opt in ("deep_model", "quick_model"):
        if body.get(opt):
            sfn_input[opt] = str(body[opt])

    # Execution name must be unique and match [A-Za-z0-9_-]{1,80}.
    execution_name = f"api-{run_id}"

    resp = _sfn.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(sfn_input),
    )

    return _ok({
        "executionArn": resp["executionArn"],
        "run_id": run_id,
        "trade_date": trade_date,
        "ticker_count": len(tickers),
    })
