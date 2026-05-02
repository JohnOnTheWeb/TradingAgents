"""Tool registry — definitions, JSON schemas, dispatch.

Each tool returns the MCP envelope ``{"data": ..., "sources": {...}}``. Schwab
calls that fail are caught and surfaced as ``sources.schwab = "failed"`` so
callers never 500 on a Schwab outage — the pipeline keeps running on
Tastytrade-only data.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from brokerage_mcp import cache as cache_layer
from brokerage_mcp.schwab import endpoints as schwab_ep
from brokerage_mcp.schwab.client import SchwabClient, SchwabError
from brokerage_mcp.tastytrade import endpoints as tt_ep
from brokerage_mcp.tastytrade.client import TastytradeClient, TastytradeError

_logger = logging.getLogger(__name__)

# Per-tool JSON schemas consumed by MCP tools/list and by the Gateway target's
# InlinePayload config.
TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "get_vol_regime": {
        "description": "Implied vs realized volatility regime: IV rank, IV percentile, IV-HV spread, beta, correlation, put/call ratio.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker"}},
            "required": ["ticker"],
        },
    },
    "get_term_structure": {
        "description": "Implied volatility per expiration (term structure).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "get_options_chain": {
        "description": "Options chain around ATM at the expiration closest to dte_target. Tastytrade first (strikes + streamer symbols), Schwab fallback (Greeks + OI + volume).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "dte_target": {"type": "integer", "default": 30},
                "strikes_width": {"type": "integer", "default": 10},
            },
            "required": ["ticker"],
        },
    },
    "get_earnings_context": {
        "description": "Next earnings date, time-of-day, and recent EPS history.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "get_liquidity": {
        "description": "Liquidity rating, liquidity rank, borrow rate, lendability.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "get_historical_vol": {
        "description": "Historical (realized) volatility for the given windows in days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "windows": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "default": [30, 60, 90],
                },
            },
            "required": ["ticker"],
        },
    },
    "get_corporate_events": {
        "description": "Recent dividend and earnings report history.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "get_quote": {
        "description": "Level 1 quote: bid, ask, mid, last, spread (bps), day hi/lo. Schwab first, Tastytrade fallback.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    "get_movers": {
        "description": "Top movers for a market index. Schwab only; returns [] if Schwab unavailable.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "string",
                    "default": "$SPX",
                    "enum": ["$DJI", "$COMPX", "$SPX", "NYSE", "NASDAQ", "OTCBB", "INDEX_ALL", "EQUITY_ALL", "OPTION_ALL", "OPTION_PUT", "OPTION_CALL"],
                },
                "sort": {
                    "type": "string",
                    "default": "PERCENT_CHANGE_UP",
                    "enum": ["VOLUME", "TRADES", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"],
                },
            },
        },
    },
    "search_instruments": {
        "description": "Search instruments by ticker or description. Schwab first, Tastytrade fallback.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


class Broker:
    """Container for per-process, shared broker clients.

    Clients are lazy: the first call to ``schwab`` / ``tastytrade`` creates the
    httpx client and loads creds. If creds are missing we store a sentinel so
    later calls know to fail-open.
    """

    def __init__(self) -> None:
        self._schwab: Optional[SchwabClient] = None
        self._schwab_unavailable: bool = False
        self._tastytrade: Optional[TastytradeClient] = None
        self._tastytrade_unavailable: bool = False

    def schwab(self) -> Optional[SchwabClient]:
        if self._schwab_unavailable:
            return None
        if self._schwab is None:
            try:
                self._schwab = SchwabClient()
            except SchwabError as err:
                _logger.warning("Schwab client unavailable: %s", err)
                self._schwab_unavailable = True
                return None
        return self._schwab

    def tastytrade(self) -> Optional[TastytradeClient]:
        if self._tastytrade_unavailable:
            return None
        if self._tastytrade is None:
            try:
                self._tastytrade = TastytradeClient()
            except TastytradeError as err:
                _logger.warning("Tastytrade client unavailable: %s", err)
                self._tastytrade_unavailable = True
                return None
        return self._tastytrade

    async def aclose(self) -> None:
        if self._schwab:
            await self._schwab.aclose()
        if self._tastytrade:
            await self._tastytrade.aclose()


def _envelope(
    data: Any, schwab: str = "skipped", tastytrade: str = "skipped"
) -> Dict[str, Any]:
    return {"data": data, "sources": {"schwab": schwab, "tastytrade": tastytrade}}


# --- tool implementations ----------------------------------------------------

async def _tool_get_vol_regime(broker: Broker, ticker: str) -> Dict[str, Any]:
    tt = broker.tastytrade()
    if tt is None:
        return _envelope({"ticker": ticker}, tastytrade="failed")
    try:
        data = await tt_ep.get_vol_regime(tt, ticker)
        return _envelope(data, tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("get_vol_regime failed: %s", err)
        return _envelope({"ticker": ticker}, tastytrade="failed")


async def _tool_get_term_structure(broker: Broker, ticker: str) -> Dict[str, Any]:
    tt = broker.tastytrade()
    if tt is None:
        return _envelope([], tastytrade="failed")
    try:
        data = await tt_ep.get_term_structure(tt, ticker)
        return _envelope(data, tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("get_term_structure failed: %s", err)
        return _envelope([], tastytrade="failed")


async def _tool_get_options_chain(
    broker: Broker, ticker: str, dte_target: int = 30, strikes_width: int = 10
) -> Dict[str, Any]:
    tt = broker.tastytrade()
    tastytrade_status = "skipped"
    data: Dict[str, Any] = {}
    if tt is not None:
        try:
            data = await tt_ep.get_options_chain(tt, ticker, dte_target, strikes_width)
            tastytrade_status = "ok"
        except TastytradeError as err:
            _logger.warning("tastytrade chain failed: %s", err)
            tastytrade_status = "failed"
    else:
        tastytrade_status = "failed"

    # Tastytrade REST chain lacks Greeks/OI — enrich from Schwab when possible.
    sw = broker.schwab()
    schwab_status = "skipped"
    if sw is not None:
        try:
            sw_data = await schwab_ep.get_options_chain(sw, ticker, dte_target, strikes_width)
            schwab_status = "ok"
            if not data or not data.get("strikes"):
                data = sw_data
            else:
                # Attach Greeks/OI from Schwab rows when strike matches.
                enriched: Dict[Any, Dict[str, Any]] = {}
                for row in sw_data.get("strikes") or []:
                    enriched[(row["strike"], row["side"])] = row
                merged_rows = []
                for row in data.get("strikes") or []:
                    for side in ("CALL", "PUT"):
                        merged_rows.append({**row, "side": side, **enriched.get((row["strike"], side), {})})
                data["strikes"] = merged_rows
                data["underlying_mark"] = data.get("underlying_mark") or sw_data.get("underlying_mark")
        except SchwabError as err:
            _logger.warning("schwab chain failed: %s", err)
            schwab_status = "failed"
    if not data:
        data = {"ticker": ticker, "expiration": None, "strikes": []}
    return _envelope(data, schwab=schwab_status, tastytrade=tastytrade_status)


async def _tool_get_earnings_context(broker: Broker, ticker: str) -> Dict[str, Any]:
    tt = broker.tastytrade()
    if tt is None:
        return _envelope({"ticker": ticker}, tastytrade="failed")
    try:
        data = await tt_ep.get_earnings_context(tt, ticker)
        return _envelope(data, tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("get_earnings_context failed: %s", err)
        return _envelope({"ticker": ticker}, tastytrade="failed")


async def _tool_get_liquidity(broker: Broker, ticker: str) -> Dict[str, Any]:
    tt = broker.tastytrade()
    if tt is None:
        return _envelope({"ticker": ticker}, tastytrade="failed")
    try:
        data = await tt_ep.get_liquidity(tt, ticker)
        return _envelope(data, tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("get_liquidity failed: %s", err)
        return _envelope({"ticker": ticker}, tastytrade="failed")


async def _tool_get_historical_vol(
    broker: Broker, ticker: str, windows: Optional[list] = None
) -> Dict[str, Any]:
    tt = broker.tastytrade()
    if tt is None:
        return _envelope({"ticker": ticker, "hv": {}}, tastytrade="failed")
    try:
        data = await tt_ep.get_historical_vol(tt, ticker, windows)
        return _envelope(data, tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("get_historical_vol failed: %s", err)
        return _envelope({"ticker": ticker, "hv": {}}, tastytrade="failed")


async def _tool_get_corporate_events(broker: Broker, ticker: str) -> Dict[str, Any]:
    tt = broker.tastytrade()
    if tt is None:
        return _envelope({"ticker": ticker, "dividends": [], "earnings": []}, tastytrade="failed")
    try:
        data = await tt_ep.get_corporate_events(tt, ticker)
        return _envelope(data, tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("get_corporate_events failed: %s", err)
        return _envelope({"ticker": ticker, "dividends": [], "earnings": []}, tastytrade="failed")


async def _tool_get_quote(broker: Broker, ticker: str) -> Dict[str, Any]:
    sw = broker.schwab()
    if sw is not None:
        try:
            data = await schwab_ep.get_quote(sw, ticker)
            return _envelope(data, schwab="ok")
        except SchwabError as err:
            _logger.warning("schwab quote failed, falling back to tastytrade: %s", err)
    # Fallback to Tastytrade.
    tt = broker.tastytrade()
    if tt is None:
        return _envelope({"ticker": ticker}, schwab="failed", tastytrade="failed")
    try:
        data = await tt_ep.get_quote(tt, ticker)
        return _envelope(data, schwab="failed", tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("tastytrade quote failed: %s", err)
        return _envelope({"ticker": ticker}, schwab="failed", tastytrade="failed")


async def _tool_get_movers(
    broker: Broker, index: str = "$SPX", sort: str = "PERCENT_CHANGE_UP"
) -> Dict[str, Any]:
    sw = broker.schwab()
    if sw is None:
        return _envelope([], schwab="failed")
    try:
        data = await schwab_ep.get_movers(sw, index, sort)
        return _envelope(data, schwab="ok")
    except SchwabError as err:
        _logger.warning("get_movers failed: %s", err)
        return _envelope([], schwab="failed")


async def _tool_search_instruments(broker: Broker, query: str) -> Dict[str, Any]:
    sw = broker.schwab()
    if sw is not None:
        try:
            data = await schwab_ep.search_instruments(sw, query)
            return _envelope(data, schwab="ok")
        except SchwabError as err:
            _logger.warning("schwab search failed, falling back: %s", err)
    tt = broker.tastytrade()
    if tt is None:
        return _envelope([], schwab="failed", tastytrade="failed")
    try:
        data = await tt_ep.search_instruments(tt, query)
        return _envelope(data, schwab="failed", tastytrade="ok")
    except TastytradeError as err:
        _logger.warning("tastytrade search failed: %s", err)
        return _envelope([], schwab="failed", tastytrade="failed")


DISPATCH: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {
    "get_vol_regime": _tool_get_vol_regime,
    "get_term_structure": _tool_get_term_structure,
    "get_options_chain": _tool_get_options_chain,
    "get_earnings_context": _tool_get_earnings_context,
    "get_liquidity": _tool_get_liquidity,
    "get_historical_vol": _tool_get_historical_vol,
    "get_corporate_events": _tool_get_corporate_events,
    "get_quote": _tool_get_quote,
    "get_movers": _tool_get_movers,
    "search_instruments": _tool_search_instruments,
}


async def call_tool(
    broker: Broker, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    if tool not in DISPATCH:
        raise KeyError(f"Unknown tool: {tool}")
    cached = cache_layer.get(tool, arguments)
    if cached is not None:
        return cached
    fn = DISPATCH[tool]
    result = await fn(broker, **arguments)
    cache_layer.put(tool, arguments, result)
    return result
