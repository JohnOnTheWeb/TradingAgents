"""LangChain ``@tool`` wrappers over the brokerage target on the AgentCore Gateway.

All calls flow through :mod:`tradingagents.gateway_client`; there is no
direct-to-sidecar fallback. When the Gateway is unreachable every tool
returns a structured "unavailable" stub so the analyst chain keeps running.

Return envelope (unchanged from pre-Gateway contract):
    {"data": ..., "sources": {"schwab": "...", "tastytrade": "..."}}
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import tool

from tradingagents.gateway_client import GatewayError, call

_TARGET = "brokerage"


def _unavailable(reason: str) -> Dict[str, Any]:
    return {
        "data": None,
        "sources": {"schwab": "skipped", "tastytrade": "skipped"},
        "error": reason,
    }


def _call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = call(f"{_TARGET}___{tool_name}", arguments)
    except GatewayError as err:
        return _unavailable(str(err))
    if isinstance(result, dict):
        return result
    return {"data": result, "sources": {"schwab": "unknown", "tastytrade": "unknown"}}


# --- tool surface ------------------------------------------------------------


@tool
def get_vol_regime(ticker: Annotated[str, "Ticker symbol"]) -> Dict[str, Any]:
    """Implied-vs-realized volatility regime: IV rank and percentile (0-100 scale over the
    trailing 252 sessions), IV-HV 30d spread (positive = options rich vs realized),
    historical volatility at 30/60/90 days, beta, 3-month SPY correlation, and
    put/call ratio. IV rank under 20 signals vol is cheap; above 80 signals blow-off.
    Term structure backwardation (front > back) indicates acute stress."""
    return _call("get_vol_regime", {"ticker": ticker})


@tool
def get_term_structure(ticker: Annotated[str, "Ticker symbol"]) -> Dict[str, Any]:
    """Implied volatility per option expiration — the term structure. Front-month >
    back-month (backwardation) is a stress signal; normal contango = calm."""
    return _call("get_term_structure", {"ticker": ticker})


@tool
def get_options_chain(
    ticker: Annotated[str, "Ticker symbol"],
    dte_target: Annotated[int, "Target days to expiration; closest expiry is returned"] = 30,
    strikes_width: Annotated[int, "Number of strikes on each side of ATM"] = 10,
) -> Dict[str, Any]:
    """Options chain around ATM at the expiration closest to ``dte_target``.
    Schwab provides Greeks (delta/gamma/theta/vega), IV, open interest, volume;
    Tastytrade provides streamer symbols for live subscription. If only one
    source is available, partial data is returned and marked in ``sources``."""
    return _call(
        "get_options_chain",
        {"ticker": ticker, "dte_target": dte_target, "strikes_width": strikes_width},
    )


@tool
def get_earnings_context(ticker: Annotated[str, "Ticker symbol"]) -> Dict[str, Any]:
    """Next earnings date, time-of-day (BMO/AMC), confirmation status, and recent
    EPS surprise history. Use to frame news and size positions ahead of prints."""
    return _call("get_earnings_context", {"ticker": ticker})


@tool
def get_liquidity(ticker: Annotated[str, "Ticker symbol"]) -> Dict[str, Any]:
    """Liquidity rating/rank, borrow rate, lendability. Rising borrow rate or
    hard-to-borrow status often indicates institutional short pressure."""
    return _call("get_liquidity", {"ticker": ticker})


@tool
def get_historical_vol(
    ticker: Annotated[str, "Ticker symbol"],
    windows: Annotated[Optional[List[int]], "Lookback windows in days"] = None,
) -> Dict[str, Any]:
    """Realized (historical) volatility for given lookback windows. Default: [30,60,90]."""
    args: Dict[str, Any] = {"ticker": ticker}
    if windows:
        args["windows"] = windows
    return _call("get_historical_vol", args)


@tool
def get_corporate_events(ticker: Annotated[str, "Ticker symbol"]) -> Dict[str, Any]:
    """Recent dividend payments and earnings reports history."""
    return _call("get_corporate_events", {"ticker": ticker})


@tool
def get_brokerage_quote(ticker: Annotated[str, "Ticker symbol"]) -> Dict[str, Any]:
    """Level-1 quote from the brokerage feed: bid, ask, mid, last, spread in bps,
    day high/low, volume. Wide spread_bps indicates thin liquidity."""
    return _call("get_quote", {"ticker": ticker})


@tool
def get_movers(
    index: Annotated[str, "Market index: $SPX, $DJI, $COMPX, NYSE, NASDAQ, etc."] = "$SPX",
    sort: Annotated[str, "VOLUME | TRADES | PERCENT_CHANGE_UP | PERCENT_CHANGE_DOWN"] = "PERCENT_CHANGE_UP",
) -> Dict[str, Any]:
    """Top movers for a market index. Returns empty list when Schwab is unavailable."""
    return _call("get_movers", {"index": index, "sort": sort})


@tool
def search_instruments(query: Annotated[str, "Ticker or description fragment"]) -> Dict[str, Any]:
    """Search instruments by ticker or description; returns up to 25 matches
    with symbol, description, CUSIP (Schwab), and asset type."""
    return _call("search_instruments", {"query": query})
