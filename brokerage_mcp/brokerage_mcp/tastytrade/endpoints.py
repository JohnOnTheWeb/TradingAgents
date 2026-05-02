"""Normalized Tastytrade endpoint wrappers.

Every function returns a plain dict in a stable shape — if Tastytrade renames a
field upstream, it's patched here, not in consumers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from brokerage_mcp.tastytrade.client import TastytradeClient, TastytradeError

_logger = logging.getLogger(__name__)


def _first_item(resp: Dict[str, Any]) -> Dict[str, Any]:
    items = (resp.get("data") or {}).get("items") or []
    return items[0] if items else {}


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def get_market_metrics(client: TastytradeClient, ticker: str) -> Dict[str, Any]:
    resp = await client.get("/market-metrics", params={"symbols": ticker})
    return _first_item(resp)


async def get_vol_regime(client: TastytradeClient, ticker: str) -> Dict[str, Any]:
    m = await get_market_metrics(client, ticker)
    return {
        "ticker": ticker,
        "iv_index": _to_float(m.get("implied-volatility-index")),
        "iv_index_5d_change": _to_float(m.get("implied-volatility-index-5-day-change")),
        "iv_rank": _to_float(m.get("implied-volatility-index-rank")),
        "iv_percentile": _to_float(m.get("implied-volatility-percentile")),
        "iv_30d": _to_float(m.get("implied-volatility-30-day")),
        "hv_30d": _to_float(m.get("historical-volatility-30-day")),
        "hv_60d": _to_float(m.get("historical-volatility-60-day")),
        "hv_90d": _to_float(m.get("historical-volatility-90-day")),
        "iv_hv_30d_spread": _to_float(m.get("iv-hv-30-day-difference")),
        "beta": _to_float(m.get("beta")),
        "corr_spy_3m": _to_float(m.get("corr-spy-3month")),
        "put_call_ratio": _to_float(m.get("option-volume") and m["option-volume"].get("put-call-ratio")),
    }


async def get_term_structure(client: TastytradeClient, ticker: str) -> List[Dict[str, Any]]:
    m = await get_market_metrics(client, ticker)
    raw = m.get("option-expiration-implied-volatilities") or []
    out: List[Dict[str, Any]] = []
    for row in raw:
        out.append(
            {
                "expiration": row.get("expiration-date"),
                "dte": row.get("settlement-type") and row.get("dte"),
                "iv": _to_float(row.get("implied-volatility")),
            }
        )
    return out


async def get_liquidity(client: TastytradeClient, ticker: str) -> Dict[str, Any]:
    m = await get_market_metrics(client, ticker)
    return {
        "ticker": ticker,
        "liquidity_rating": m.get("liquidity-rating"),
        "liquidity_rank": _to_float(m.get("liquidity-rank")),
        "liquidity_value": _to_float(m.get("liquidity-value")),
        "borrow_rate": _to_float(m.get("borrow-rate")),
        "lendability": m.get("lendability"),
        "listed_market": m.get("listed-market"),
    }


async def get_historical_vol(
    client: TastytradeClient, ticker: str, windows: Optional[List[int]] = None
) -> Dict[str, Any]:
    m = await get_market_metrics(client, ticker)
    all_hv = {
        30: _to_float(m.get("historical-volatility-30-day")),
        60: _to_float(m.get("historical-volatility-60-day")),
        90: _to_float(m.get("historical-volatility-90-day")),
    }
    picked = windows or [30, 60, 90]
    return {
        "ticker": ticker,
        "hv": {str(w): all_hv.get(w) for w in picked},
    }


async def get_earnings_context(client: TastytradeClient, ticker: str) -> Dict[str, Any]:
    m = await get_market_metrics(client, ticker)
    earnings = m.get("earnings") or {}
    out: Dict[str, Any] = {
        "ticker": ticker,
        "next_date": earnings.get("expected-report-date"),
        "time_of_day": earnings.get("time-of-day"),
        "confirmed": earnings.get("actual-eps") is None and bool(earnings.get("expected-report-date")),
    }
    # Historical EPS surprises — separate endpoint, best-effort.
    try:
        history = await client.get(
            f"/market-metrics/historic-corporate-events/earnings-reports/{ticker}"
        )
        items = (history.get("data") or {}).get("items") or []
        out["recent_reports"] = [
            {"date": r.get("occurred-date"), "eps": _to_float(r.get("eps"))}
            for r in items[:8]
        ]
    except TastytradeError as err:
        _logger.warning("earnings-reports history failed for %s: %s", ticker, err)
        out["recent_reports"] = []
    return out


async def get_corporate_events(client: TastytradeClient, ticker: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ticker": ticker, "dividends": [], "earnings": []}
    try:
        divs = await client.get(
            f"/market-metrics/historic-corporate-events/dividends/{ticker}"
        )
        items = (divs.get("data") or {}).get("items") or []
        out["dividends"] = [
            {"date": r.get("occurred-date"), "amount": _to_float(r.get("amount"))}
            for r in items[:12]
        ]
    except TastytradeError as err:
        _logger.warning("dividends history failed for %s: %s", ticker, err)
    try:
        er = await client.get(
            f"/market-metrics/historic-corporate-events/earnings-reports/{ticker}"
        )
        items = (er.get("data") or {}).get("items") or []
        out["earnings"] = [
            {"date": r.get("occurred-date"), "eps": _to_float(r.get("eps"))}
            for r in items[:12]
        ]
    except TastytradeError as err:
        _logger.warning("earnings-reports history failed for %s: %s", ticker, err)
    return out


async def get_options_chain(
    client: TastytradeClient,
    ticker: str,
    dte_target: int = 30,
    strikes_width: int = 10,
) -> Dict[str, Any]:
    """Pick the expiration closest to ``dte_target`` and return ATM±width strikes."""
    nested = await client.get(f"/option-chains/{ticker}/nested")
    expirations = (nested.get("data") or {}).get("items") or []
    if not expirations:
        return {"ticker": ticker, "expiration": None, "strikes": []}
    root = expirations[0]
    buckets = root.get("expirations") or []
    if not buckets:
        return {"ticker": ticker, "expiration": None, "strikes": []}

    # Find expiration closest to dte_target.
    def _dte(b: Dict[str, Any]) -> int:
        try:
            return int(b.get("days-to-expiration") or 0)
        except (TypeError, ValueError):
            return 0

    chosen = min(buckets, key=lambda b: abs(_dte(b) - dte_target))

    # Underlying mark — pull from market-metrics for ATM reference.
    m = await get_market_metrics(client, ticker)
    underlying_mark = _to_float(m.get("mark")) or _to_float(m.get("last"))

    strikes_raw = chosen.get("strikes") or []
    if underlying_mark is not None and strikes_raw:
        strikes_raw.sort(
            key=lambda s: abs(_to_float(s.get("strike-price")) or 0 - underlying_mark)
        )
        trimmed = strikes_raw[: max(strikes_width, 1) * 2]
        # Re-sort by strike for readable output.
        trimmed.sort(key=lambda s: _to_float(s.get("strike-price")) or 0)
    else:
        trimmed = strikes_raw[: max(strikes_width, 1) * 2]

    rows: List[Dict[str, Any]] = []
    for s in trimmed:
        rows.append(
            {
                "strike": _to_float(s.get("strike-price")),
                "call_streamer_symbol": s.get("call-streamer-symbol"),
                "put_streamer_symbol": s.get("put-streamer-symbol"),
                # Greeks / IV / volume / OI are not in the nested REST shape —
                # consumers needing them must subscribe via DXLink. Exposing
                # streaming greeks from an HTTP MCP is out of scope for v1.
            }
        )
    return {
        "ticker": ticker,
        "expiration": chosen.get("expiration-date"),
        "dte": _dte(chosen),
        "underlying_mark": underlying_mark,
        "strikes": rows,
    }


async def search_instruments(
    client: TastytradeClient, query: str
) -> List[Dict[str, Any]]:
    resp = await client.get(f"/symbols/search/{query}")
    items = (resp.get("data") or {}).get("items") or []
    return [
        {
            "symbol": r.get("symbol"),
            "description": r.get("description"),
            "asset_type": r.get("instrument-type"),
        }
        for r in items[:25]
    ]


async def get_quote(client: TastytradeClient, ticker: str) -> Dict[str, Any]:
    """Fallback for Schwab — market-metrics carries a last/mark field only."""
    m = await get_market_metrics(client, ticker)
    return {
        "ticker": ticker,
        "bid": None,
        "ask": None,
        "mid": _to_float(m.get("mark")),
        "last": _to_float(m.get("last")),
        "volume": _to_float(m.get("volume")),
        "day_high": _to_float(m.get("high")),
        "day_low": _to_float(m.get("low")),
        "spread_bps": None,
    }
