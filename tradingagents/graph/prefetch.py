"""Ticker pre-fetch: run the high-frequency data-tools in parallel at the
start of each ticker's run and drop the results into the graph state so
analyst nodes can read them directly instead of calling ToolNodes.

Each ToolNode still exists for ad-hoc follow-up queries; prefetch just
gives the analyst LLM enough context to skip the common calls.

Implementation notes:
- Uses :func:`tradingagents.gateway_client.call` so everything flows
  through the AgentCore Gateway (same path the ToolNodes use; same
  caching applies).
- Parallel via ``concurrent.futures.ThreadPoolExecutor`` — the Gateway
  + Lambda layer handles concurrency fine. 8 workers is enough for the
  bundle we fetch.
- Any failure is caught and rendered as a short "unavailable: <reason>"
  line; one slow tool doesn't block the rest, and the analyst just sees
  less context rather than a crash.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from tradingagents.gateway_client import GatewayError, call

logger = logging.getLogger(__name__)


def _ymd(d) -> str:
    return d.strftime("%Y-%m-%d") if not isinstance(d, str) else d


def _back(trade_date: str, days: int) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def _build_tasks(ticker: str, trade_date: str) -> List[Tuple[str, str, Dict[str, Any]]]:
    """(label, gateway_tool, args) triples to dispatch in parallel."""
    start_30d = _back(trade_date, 30)
    start_7d = _back(trade_date, 7)
    return [
        (
            "price_history_30d",
            "data-tools___get_stock_data",
            {"symbol": ticker, "start_date": start_30d, "end_date": trade_date},
        ),
        (
            "indicators_core",
            "data-tools___get_indicators",
            {
                "symbol": ticker,
                "indicator": "close_50_sma,close_200_sma,close_10_ema,macd,rsi,boll,atr,vwma",
                "curr_date": trade_date,
                "look_back_days": 30,
            },
        ),
        (
            "fundamentals",
            "data-tools___get_fundamentals",
            {"ticker": ticker, "curr_date": trade_date},
        ),
        (
            "balance_sheet",
            "data-tools___get_balance_sheet",
            {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date},
        ),
        (
            "news_7d",
            "data-tools___get_news",
            {"ticker": ticker, "start_date": start_7d, "end_date": trade_date},
        ),
        (
            "global_news",
            "data-tools___get_global_news",
            {"curr_date": trade_date, "look_back_days": 7, "limit": 5},
        ),
        (
            "insider_transactions",
            "data-tools___get_insider_transactions",
            {"ticker": ticker},
        ),
        (
            "quote",
            "brokerage___get_quote",
            {"ticker": ticker},
        ),
        (
            "vol_regime",
            "brokerage___get_vol_regime",
            {"ticker": ticker},
        ),
    ]


def _one(tool_name: str, args: Dict[str, Any]) -> Any:
    try:
        return call(tool_name, args)
    except GatewayError as err:
        return f"[{tool_name} unavailable: {err}]"


def fetch_bundle(ticker: str, trade_date: str, max_workers: int = 8) -> Dict[str, Any]:
    """Run the prefetch bundle for one ticker. Returns a dict keyed by label."""
    tasks = _build_tasks(ticker, trade_date)
    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_one, tool, args): label for label, tool, args in tasks
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as err:  # noqa: BLE001
                # _one already wraps errors, but add defense in depth.
                logger.warning("prefetch %s raised: %s", label, err)
                results[label] = f"[{label} error: {err}]"
    return results


def render_bundle_for_prompt(bundle: Dict[str, Any]) -> str:
    """Compact markdown rendering of the bundle for prompt injection.

    Large values (price CSVs, fundamentals blobs) are kept verbatim so the
    LLM can read them; brokerage envelopes are flattened to their `data`
    field so the LLM doesn't trip over `sources: {schwab: "ok"}` noise.
    """
    if not bundle:
        return ""
    lines = ["# Pre-fetched context (already retrieved — prefer this over calling tools)\n"]
    for label in [
        "price_history_30d", "indicators_core", "fundamentals", "balance_sheet",
        "news_7d", "global_news", "insider_transactions", "quote", "vol_regime",
    ]:
        if label not in bundle:
            continue
        val = bundle[label]
        rendered = _render_value(val)
        lines.append(f"## {label}\n{rendered}\n")
    return "\n".join(lines)


def _render_value(val: Any) -> str:
    if val is None:
        return "(none)"
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        # Brokerage shape: {"data": ..., "sources": {...}}. Prefer data.
        if "data" in val and isinstance(val["data"], (dict, list)):
            import json as _j
            return _j.dumps(val["data"], default=str, indent=2)
        if "data" in val and val["data"] is None and "error" in val:
            return f"(unavailable: {val.get('error')})"
        import json as _j
        return _j.dumps(val, default=str, indent=2)
    if isinstance(val, list):
        import json as _j
        return _j.dumps(val, default=str, indent=2)
    return str(val)
