"""Error-handler Lambda: publishes SNS alert when any Step Functions state fails.

Invoked from every Catch block in the state machine. Input looks like:

    {
        "run_id":     "<uuid>" (when available),
        "trade_date": "2026-04-30" (when available),
        "stage":      "invoke_agent" | "get_config" | "aggregate",
        "ticker":     "NVDA" (if applicable),
        "error":      { "Error": "...", "Cause": "..." }  (Step Functions error shape)
    }
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import boto3

_sns = boto3.client("sns")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    topic = os.environ["SNS_NOTIFICATIONS_TOPIC"]
    stage = event.get("stage", "unknown")
    ticker = event.get("ticker")
    run_id = event.get("run_id", "unknown")
    trade_date = event.get("trade_date", "unknown")
    err = event.get("error") or {}

    err_name = err.get("Error", "UnknownError")
    cause_raw = err.get("Cause", "")
    try:
        cause = json.loads(cause_raw)
    except (TypeError, json.JSONDecodeError):
        cause = {"Cause": cause_raw}

    log_group = os.environ.get("LOG_GROUP_NAME", "")
    log_link = ""
    if log_group and context is not None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        stream = getattr(context, "log_stream_name", "")
        # CloudWatch Logs console deep-link
        log_link = (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#logsV2:log-groups/log-group/"
            f"{log_group.replace('/', '$252F')}"
            f"/log-events/{stream.replace('/', '$252F')}"
        )

    subject = f"TradingAgents FAILED — {stage} — {ticker or run_id}"[:100]
    lines = [
        f"TradingAgents run {run_id} failed at stage: {stage}",
        f"Date: {trade_date}",
    ]
    if ticker:
        lines.append(f"Ticker: {ticker}")
    lines += [
        "",
        f"Error: {err_name}",
        "Cause:",
        json.dumps(cause, indent=2)[:3000],
    ]
    if log_link:
        lines += ["", f"Logs: {log_link}"]

    _sns.publish(TopicArn=topic, Subject=subject, Message="\n".join(lines))
    return {"notified": True, "stage": stage, "ticker": ticker}
