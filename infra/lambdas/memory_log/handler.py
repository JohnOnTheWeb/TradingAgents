"""AgentCore Gateway MCP-target Lambda: memory-log tools.

Self-contained DynamoDB access so the Lambda does not import any
agent-facing ``@tool`` or ``TradingMemoryLog``/``DynamoDBMemoryLog`` class
from the ``tradingagents`` package — those now route back through the
Gateway and would cause an infinite loop.

Exposed tools (MCP names):
  * ``get_past_context(ticker, n_same=5, n_cross=3)`` -> str
  * ``store_decision(ticker, trade_date, final_trade_decision)`` -> None
  * ``batch_update_with_outcomes(updates)`` -> None
  * ``get_pending_entries()`` -> list[dict]
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_GSI = "status-index"
_TABLE_ENV = "TRADINGAGENTS_MEMORY_TABLE"

_rating_re = re.compile(r"final\s+rating\s*[:\-]\s*([a-z\s]+)", re.IGNORECASE)

_RATINGS = ("strong buy", "buy", "hold", "sell", "strong sell")


def _parse_rating(decision: str) -> str:
    m = _rating_re.search(decision or "")
    if not m:
        return "Hold"
    candidate = m.group(1).strip().lower()
    for r in _RATINGS:
        if candidate.startswith(r):
            return r.title()
    return "Hold"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_pct(value: Optional[float]) -> Optional[str]:
    return f"{value:+.1%}" if isinstance(value, (int, float)) else None


_table_handle = None


def _table():
    global _table_handle
    if _table_handle is None:
        name = os.environ.get(_TABLE_ENV)
        if not name:
            raise RuntimeError(f"{_TABLE_ENV} env var is required")
        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        _table_handle = boto3.resource("dynamodb", region_name=region).Table(name)
    return _table_handle


def _normalize(item: Dict[str, Any]) -> Dict[str, Any]:
    status = item.get("status")
    raw_return = item.get("raw_return")
    alpha_return = item.get("alpha_return")
    return {
        "ticker": item.get("ticker"),
        "date": item.get("trade_date"),
        "rating": item.get("rating", "Hold"),
        "pending": status == "pending",
        "raw": _fmt_pct(float(raw_return)) if raw_return is not None else None,
        "alpha": _fmt_pct(float(alpha_return)) if alpha_return is not None else None,
        "holding": f"{int(item['holding_days'])}d" if "holding_days" in item else None,
        "decision": item.get("decision", ""),
        "reflection": item.get("reflection", ""),
    }


def _format_full(e: Dict[str, Any]) -> str:
    raw = e.get("raw") or "n/a"
    alpha = e.get("alpha") or "n/a"
    holding = e.get("holding") or "n/a"
    tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
    parts = [tag, f"DECISION:\n{e['decision']}"]
    if e.get("reflection"):
        parts.append(f"REFLECTION:\n{e['reflection']}")
    return "\n\n".join(parts)


def _format_reflection_only(e: Dict[str, Any]) -> str:
    tag = (
        f"[{e['date']} | {e['ticker']} | {e['rating']} | "
        f"{e.get('raw') or 'n/a'}]"
    )
    if e.get("reflection"):
        return f"{tag}\n{e['reflection']}"
    decision = e.get("decision", "") or ""
    text = decision[:300]
    suffix = "..." if len(decision) > 300 else ""
    return f"{tag}\n{text}{suffix}"


# --- tool implementations ---------------------------------------------------


def _store_decision(
    ticker: str,
    trade_date: str,
    final_trade_decision: str,
) -> None:
    rating = _parse_rating(final_trade_decision)
    try:
        _table().put_item(
            Item={
                "ticker": ticker,
                "trade_date": trade_date,
                "status": "pending",
                "rating": rating,
                "decision": final_trade_decision,
                "created_at": _now(),
            },
            ConditionExpression=(
                "attribute_not_exists(ticker) AND attribute_not_exists(trade_date)"
            ),
        )
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return
        raise


def _batch_update_with_outcomes(updates: List[Dict[str, Any]]) -> None:
    tbl = _table()
    for u in updates:
        tbl.update_item(
            Key={"ticker": u["ticker"], "trade_date": u["trade_date"]},
            UpdateExpression=(
                "SET #s = :resolved, "
                "raw_return = :raw, "
                "alpha_return = :alpha, "
                "holding_days = :days, "
                "reflection = :refl, "
                "resolved_at = :ts"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":resolved": "resolved",
                ":raw": Decimal(str(u["raw_return"])),
                ":alpha": Decimal(str(u["alpha_return"])),
                ":days": int(u["holding_days"]),
                ":refl": u["reflection"],
                ":ts": _now(),
            },
        )


def _get_pending_entries() -> List[Dict[str, Any]]:
    kwargs: Dict[str, Any] = {
        "IndexName": _GSI,
        "KeyConditionExpression": "#s = :pending",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":pending": "pending"},
    }
    items: List[Dict[str, Any]] = []
    tbl = _table()
    while True:
        resp = tbl.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [_normalize(i) for i in items]


def _get_past_context(ticker: str, n_same: int = 5, n_cross: int = 3) -> str:
    tbl = _table()
    n_same = int(n_same)
    n_cross = int(n_cross)

    same_resp = tbl.query(
        KeyConditionExpression="ticker = :t",
        FilterExpression="#s = :resolved",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":t": ticker, ":resolved": "resolved"},
        ScanIndexForward=False,
        Limit=n_same * 3,
    )
    same = [_normalize(i) for i in same_resp.get("Items", [])][:n_same]

    cross_resp = tbl.query(
        IndexName=_GSI,
        KeyConditionExpression="#s = :resolved",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":resolved": "resolved"},
        ScanIndexForward=False,
        Limit=n_cross * 3,
    )
    cross = [
        _normalize(i) for i in cross_resp.get("Items", []) if i.get("ticker") != ticker
    ][:n_cross]

    if not same and not cross:
        return ""

    parts: List[str] = []
    if same:
        parts.append(f"Past analyses of {ticker} (most recent first):")
        parts.extend(_format_full(e) for e in same)
    if cross:
        parts.append("Recent cross-ticker lessons:")
        parts.extend(_format_reflection_only(e) for e in cross)
    return "\n\n".join(parts)


_DISPATCH: Dict[str, Callable[..., Any]] = {
    "get_past_context": _get_past_context,
    "store_decision": _store_decision,
    "batch_update_with_outcomes": _batch_update_with_outcomes,
    "get_pending_entries": _get_pending_entries,
}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    tool_name = (
        event.get("tool_name")
        or event.get("__name")
        or (context.client_context.custom.get("bedrockAgentCoreToolName")
            if getattr(context, "client_context", None) and context.client_context
            and getattr(context.client_context, "custom", None) else None)
        or ""
    )
    args = event.get("tool_arguments") or event.get("arguments")
    if args is None:
        args = {k: v for k, v in event.items() if k not in ("tool_name", "__name")}
    if not tool_name:
        logger.warning("tool_name missing. event keys=%s", list(event.keys()))
        raise ValueError("tool_name is required")

    fn = _DISPATCH.get(tool_name)
    if fn is None:
        raise ValueError(
            f"Unknown memory-log tool: {tool_name!r}. Known: {sorted(_DISPATCH)}"
        )

    try:
        if isinstance(args, dict):
            result = fn(**args)
        else:
            result = fn(*args)
    except Exception as err:  # noqa: BLE001
        logger.error("memory tool %s failed: %s", tool_name, err, exc_info=True)
        raise

    return {"tool_name": tool_name, "result": result}
