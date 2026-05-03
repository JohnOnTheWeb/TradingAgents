"""DynamoDB-backed tool-result cache for the data-tools Lambda.

Cache key shape: ``<tool>::<args_hash>::<date_bucket>``. The date bucket is
determined per tool — for tools whose output is a function of today's date
(OHLCV for today, quotes, vol regime), the bucket is a quarter-hour slot;
for tools whose output is fully historical (OHLCV end_date < today,
get_returns), the bucket is "all" so cache hits span days.

TTL is stored as the DynamoDB-native ``ttl`` attribute (epoch seconds) so
rows expire server-side without a sweeper.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, date, timezone
from typing import Any, Callable, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ.get("TOOL_CACHE_TABLE", "")
_table = None
_cloudwatch = None
_METRIC_NS = os.environ.get("TOOL_CACHE_METRIC_NAMESPACE", "TradingAgents/ToolCache")


def _emit_metric(tool: str, hit: bool) -> None:
    """Best-effort hit/miss metric. Silent on any failure."""
    global _cloudwatch
    try:
        if _cloudwatch is None:
            _cloudwatch = boto3.client("cloudwatch")
        _cloudwatch.put_metric_data(
            Namespace=_METRIC_NS,
            MetricData=[
                {
                    "MetricName": "Hit" if hit else "Miss",
                    "Dimensions": [{"Name": "Tool", "Value": tool}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:  # noqa: BLE001
        pass

# Per-tool TTL in seconds. Choose conservative values: err on short side
# rather than serving stale data. Override via env TOOL_CACHE_TTL_<TOOL>.
_TTL_DEFAULTS: Dict[str, int] = {
    # Historical OHLCV: end_date < today → immutable. The handler rewrites
    # the bucket to "all" for this case so cache hits are permanent until
    # TTL expiry (30 days is just a cleanup guardrail).
    "get_stock_data": 30 * 24 * 3600,
    "get_indicators": 30 * 24 * 3600,
    # Fundamentals: change quarterly with earnings. One day TTL is safe.
    "get_fundamentals": 24 * 3600,
    "get_balance_sheet": 24 * 3600,
    "get_cashflow": 24 * 3600,
    "get_income_statement": 24 * 3600,
    # Insider filings: daily updates are fine.
    "get_insider_transactions": 24 * 3600,
    # News: staleness is tolerable for research, not for trading decisions
    # that are themselves made once-per-day.
    "get_news": 4 * 3600,
    "get_global_news": 4 * 3600,
    # Realised returns: purely historical once actual_holding_days elapse.
    "get_returns": 7 * 24 * 3600,
}


def _get_table():
    global _table
    if _table is None and _TABLE_NAME:
        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        _table = boto3.resource("dynamodb", region_name=region).Table(_TABLE_NAME)
    return _table


def _ttl_seconds(tool: str) -> int:
    override = os.environ.get(f"TOOL_CACHE_TTL_{tool.upper()}")
    if override and override.isdigit():
        return int(override)
    return _TTL_DEFAULTS.get(tool, 6 * 3600)


def _date_bucket(tool: str, args: Dict[str, Any]) -> str:
    """Bucket key that lets caches span days when the output is historical.

    Returns "all" when the window is fully historical (end_date < today's
    UTC date), else today's UTC date. A 15-minute slot within today's
    bucket limits intraday staleness for live-data tools.
    """
    today = date.today().isoformat()

    if tool == "get_stock_data":
        end = args.get("end_date")
        if _is_past(end, today):
            return "all"
        return f"{today}_q{_quarter_hour_slot()}"

    if tool == "get_indicators":
        curr = args.get("curr_date")
        if _is_past(curr, today):
            return "all"
        return f"{today}_q{_quarter_hour_slot()}"

    if tool == "get_news":
        end = args.get("end_date")
        if _is_past(end, today):
            return "all"
        return f"{today}_q{_quarter_hour_slot()}"

    if tool == "get_global_news":
        curr = args.get("curr_date")
        if _is_past(curr, today):
            return "all"
        return f"{today}_q{_quarter_hour_slot()}"

    if tool == "get_returns":
        # Always historical once the holding window has elapsed; bucket on
        # trade_date so re-asking for the same resolution is free.
        td = args.get("trade_date") or today
        return f"td={td}"

    if tool in ("get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"):
        return today

    if tool == "get_insider_transactions":
        return today

    return today


def _is_past(iso: Optional[str], today_iso: str) -> bool:
    if not iso or not isinstance(iso, str):
        return False
    try:
        return iso < today_iso  # lexicographic on YYYY-MM-DD works
    except Exception:  # noqa: BLE001
        return False


def _quarter_hour_slot() -> int:
    now = datetime.now(timezone.utc)
    return (now.hour * 4) + (now.minute // 15)


def _args_hash(args: Dict[str, Any]) -> str:
    # Canonical JSON for stable hashing. Sort keys; None/default stripped
    # so absent-vs-explicit-default map to the same key.
    canonical = {k: v for k, v in args.items() if v is not None}
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _cache_key(tool: str, args: Dict[str, Any]) -> str:
    return f"{tool}::{_args_hash(args)}::{_date_bucket(tool, args)}"


def cached_call(
    tool: str,
    args: Dict[str, Any],
    producer: Callable[[], Any],
) -> Any:
    """Look up ``tool(**args)`` in the cache; call ``producer()`` on miss.

    When the cache table is unavailable or misconfigured, the producer is
    called directly — caching is best-effort and must not block tool calls.
    """
    table = _get_table()
    if table is None:
        return producer()

    key = _cache_key(tool, args)

    try:
        resp = table.get_item(Key={"cache_key": key}, ConsistentRead=False)
        item = resp.get("Item")
        if item:
            logger.info("cache HIT tool=%s key=%s", tool, key)
            _emit_metric(tool, hit=True)
            payload = item.get("payload")
            if payload is not None:
                try:
                    return json.loads(payload)
                except (TypeError, ValueError):
                    return payload
    except ClientError as err:
        logger.warning("cache get_item error for %s: %s", tool, err)

    logger.info("cache MISS tool=%s key=%s", tool, key)
    _emit_metric(tool, hit=False)
    result = producer()

    # Don't cache degraded sentinel strings. Callers return shapes like
    # "[<tool> unavailable: ...]" when all vendors failed; caching them
    # would freeze the failure.
    if isinstance(result, str) and result.startswith("[") and "unavailable" in result[:60]:
        return result

    try:
        ttl_epoch = int(time.time()) + _ttl_seconds(tool)
        table.put_item(
            Item={
                "cache_key": key,
                "payload": json.dumps(result, default=str),
                "cached_at": int(time.time()),
                "ttl": ttl_epoch,
                "tool": tool,
            },
        )
    except ClientError as err:
        logger.warning("cache put_item error for %s: %s", tool, err)
    except (TypeError, ValueError) as err:
        logger.warning("cache serialization error for %s: %s", tool, err)

    return result
