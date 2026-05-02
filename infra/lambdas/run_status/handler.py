"""Web API → DescribeExecution wrapper.

Fronted by API Gateway HTTP API with AWS_IAM authorizer. Route:
``GET /runs/{executionArn}`` — the ARN is URL-encoded in the path.

Response:

    {
      "status": "RUNNING" | "SUCCEEDED" | "FAILED" | "TIMED_OUT" | "ABORTED",
      "start_date": "...", "stop_date": null | "...",
      "run_id": "<uuid>",
      "output": {...}    // present only when status=SUCCEEDED
    }
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime
from typing import Any, Dict

import boto3

_sfn = boto3.client("stepfunctions")


def _not_found(msg: str) -> Dict[str, Any]:
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": msg}),
    }


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
        "body": json.dumps(body, default=str),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    path_params = event.get("pathParameters") or {}
    arn_encoded = path_params.get("executionArn")
    if not arn_encoded:
        return _bad_request("missing executionArn path parameter")

    execution_arn = urllib.parse.unquote(arn_encoded)

    try:
        resp = _sfn.describe_execution(executionArn=execution_arn)
    except _sfn.exceptions.ExecutionDoesNotExist:
        return _not_found(f"execution not found: {execution_arn}")

    body: Dict[str, Any] = {
        "executionArn": resp["executionArn"],
        "status": resp["status"],
        "start_date": _iso(resp.get("startDate")),
        "stop_date": _iso(resp.get("stopDate")),
    }

    raw_input = resp.get("input")
    if raw_input:
        try:
            parsed = json.loads(raw_input)
            body["run_id"] = parsed.get("run_id")
            body["trade_date"] = parsed.get("trade_date")
            tickers = parsed.get("tickers") or []
            body["ticker_count"] = len(tickers)
        except json.JSONDecodeError:
            pass

    if resp.get("output"):
        try:
            body["output"] = json.loads(resp["output"])
        except json.JSONDecodeError:
            body["output"] = resp["output"]

    if resp.get("error"):
        body["error"] = resp["error"]
        body["cause"] = resp.get("cause")

    return _ok(body)
