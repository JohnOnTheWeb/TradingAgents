"""AgentCore Gateway MCP-target Lambda: market-data tools.

This handler is the *server* side of the MCP contract. It MUST NOT import
the agent-facing ``@tool`` wrappers in ``tradingagents.agents.utils.*`` —
those now route back through the Gateway, which would create an infinite
loop. Instead we dispatch straight to the vendor-abstracted implementations
in ``tradingagents.dataflows.interface``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _resolve_alpha_vantage_key() -> None:
    """Populate ALPHA_VANTAGE_API_KEY from Secrets Manager at cold start.

    Reads the secret id from ALPHA_VANTAGE_SECRET_ID and sets the env var
    once so downstream code in tradingagents.dataflows.alpha_vantage_common
    can call os.getenv without additional plumbing. Silent no-op when the
    secret id isn't set or the key is already present.
    """
    if os.environ.get("ALPHA_VANTAGE_API_KEY"):
        return
    secret_id = (os.environ.get("ALPHA_VANTAGE_SECRET_ID") or "").strip()
    if not secret_id:
        return
    try:
        import boto3
        raw = boto3.client("secretsmanager").get_secret_value(
            SecretId=secret_id,
        ).get("SecretString") or ""
        # Allow either raw-string or {"api_key": "..."} shape.
        try:
            parsed = json.loads(raw)
            key = str(parsed.get("api_key") or parsed.get("ALPHA_VANTAGE_API_KEY") or raw).strip()
        except json.JSONDecodeError:
            key = raw.strip()
        if key:
            os.environ["ALPHA_VANTAGE_API_KEY"] = key
    except Exception as err:  # noqa: BLE001
        logger.warning("failed to resolve ALPHA_VANTAGE_API_KEY from %s: %s", secret_id, err)


_resolve_alpha_vantage_key()


def _split_indicators(indicator: str) -> list[str]:
    """LLMs sometimes pass comma-separated indicator lists; normalise them."""
    return [i.strip().lower() for i in str(indicator).split(",") if i.strip()]


def _get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)


def _get_indicators(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int = 30,
) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    indicators = _split_indicators(indicator)
    if not indicators:
        return ""
    results = []
    for ind in indicators:
        try:
            results.append(
                route_to_vendor("get_indicators", symbol, ind, curr_date, look_back_days)
            )
        except ValueError as err:
            results.append(str(err))
    return "\n\n".join(results)


def _get_fundamentals(ticker: str, curr_date: str) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_fundamentals", ticker, curr_date)


def _get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: Any = None) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_balance_sheet", ticker, freq, curr_date)


def _get_cashflow(ticker: str, freq: str = "quarterly", curr_date: Any = None) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_cashflow", ticker, freq, curr_date)


def _get_income_statement(ticker: str, freq: str = "quarterly", curr_date: Any = None) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_income_statement", ticker, freq, curr_date)


def _get_news(ticker: str, start_date: str, end_date: str) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_news", ticker, start_date, end_date)


def _get_insider_transactions(ticker: str) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_insider_transactions", ticker)


def _get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 5) -> str:
    from tradingagents.dataflows.interface import route_to_vendor
    return route_to_vendor("get_global_news", curr_date, look_back_days, limit)


def _get_returns(ticker: str, trade_date: str, holding_days: int = 5) -> Dict[str, Any]:
    """Realised raw + SPY-alpha returns over a holding window.

    Returns ``{"raw_return", "alpha_return", "actual_holding_days", "note"?}``.
    When no data is available, returns a dict with a ``note`` key and zero
    returns so callers can degrade gracefully.
    """
    import yfinance as yf
    from datetime import datetime, timedelta

    try:
        anchor = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
    except ValueError as err:
        raise ValueError(f"trade_date must be YYYY-MM-DD, got {trade_date!r}: {err}") from err

    lookahead = int(holding_days) + 7
    end = anchor + timedelta(days=lookahead)

    def _history(sym: str):
        return yf.Ticker(sym).history(start=str(anchor), end=str(end), auto_adjust=True)

    hist = _history(ticker)
    spy = _history("SPY")
    if hist.empty or spy.empty:
        return {
            "raw_return": 0.0,
            "alpha_return": 0.0,
            "actual_holding_days": 0,
            "note": "no market data available for window",
        }

    actual = min(int(holding_days), len(hist) - 1, len(spy) - 1)
    if actual <= 0:
        return {
            "raw_return": 0.0,
            "alpha_return": 0.0,
            "actual_holding_days": 0,
            "note": "insufficient trading days in window",
        }

    raw = float(hist["Close"].iloc[actual] / hist["Close"].iloc[0] - 1.0)
    bench = float(spy["Close"].iloc[actual] / spy["Close"].iloc[0] - 1.0)
    return {
        "raw_return": raw,
        "alpha_return": raw - bench,
        "actual_holding_days": actual,
    }


_DISPATCH: Dict[str, Callable[..., Any]] = {
    "get_stock_data": _get_stock_data,
    "get_indicators": _get_indicators,
    "get_fundamentals": _get_fundamentals,
    "get_balance_sheet": _get_balance_sheet,
    "get_cashflow": _get_cashflow,
    "get_income_statement": _get_income_statement,
    "get_news": _get_news,
    "get_insider_transactions": _get_insider_transactions,
    "get_global_news": _get_global_news,
    "get_returns": _get_returns,
}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    # Gateway invokes Lambda with the tool NAME in the `BEDROCK_AGENTCORE_TOOL_NAME`
    # context client context or in the event; the tool ARGUMENTS are splatted at
    # the top level of the event. Support both the older explicit shape and the
    # Gateway's actual shape.
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
        # Treat the event itself as the args dict, minus the meta keys.
        args = {k: v for k, v in event.items() if k not in ("tool_name", "__name")}
    if not tool_name:
        logger.warning("tool_name missing. event keys=%s", list(event.keys()))
        raise ValueError("tool_name is required")

    # Gateway delivers the fully-qualified name (e.g. "data-tools___get_stock_data")
    # but dispatch keys are bare tool names ("get_stock_data").
    if "___" in tool_name:
        tool_name = tool_name.rsplit("___", 1)[-1]

    fn = _DISPATCH.get(tool_name)
    if fn is None:
        raise ValueError(
            f"Unknown tool: {tool_name!r}. Known tools: {sorted(_DISPATCH)}"
        )

    try:
        result = fn(**args) if isinstance(args, dict) else fn(*args)
    except Exception as err:  # noqa: BLE001
        logger.error("tool %s failed: %s", tool_name, err, exc_info=True)
        raise

    if not isinstance(result, (dict, list, str, int, float, bool)) and result is not None:
        result = str(result)
    return {"tool_name": tool_name, "result": result}
