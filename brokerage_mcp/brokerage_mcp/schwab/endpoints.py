"""Normalized Schwab endpoint wrappers. Production path: /marketdata/v1."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from brokerage_mcp.schwab.client import SchwabClient, SchwabError  # noqa: F401


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def get_quote(client: SchwabClient, ticker: str) -> Dict[str, Any]:
    resp = await client.get(f"/marketdata/v1/{ticker}/quotes")
    q = (resp.get(ticker) or {}).get("quote") or {}
    bid = _to_float(q.get("bidPrice"))
    ask = _to_float(q.get("askPrice"))
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    spread_bps = None
    if bid and ask and mid:
        spread_bps = ((ask - bid) / mid) * 10_000
    return {
        "ticker": ticker,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": _to_float(q.get("lastPrice")),
        "volume": _to_float(q.get("totalVolume")),
        "day_high": _to_float(q.get("highPrice")),
        "day_low": _to_float(q.get("lowPrice")),
        "spread_bps": spread_bps,
    }


async def get_movers(
    client: SchwabClient,
    index: str = "$SPX",
    sort: str = "PERCENT_CHANGE_UP",
    frequency: int = 0,
) -> List[Dict[str, Any]]:
    resp = await client.get(
        f"/marketdata/v1/movers/{index}",
        params={"sort": sort, "frequency": frequency},
    )
    screeners = resp.get("screeners") or []
    return [
        {
            "symbol": s.get("symbol"),
            "description": s.get("description"),
            "last": _to_float(s.get("last")),
            "net_change": _to_float(s.get("netChange")),
            "net_percent_change": _to_float(s.get("netPercentChange")),
            "volume": _to_float(s.get("volume")),
        }
        for s in screeners
    ]


async def search_instruments(
    client: SchwabClient, query: str, projection: str = "symbol-search"
) -> List[Dict[str, Any]]:
    resp = await client.get(
        "/marketdata/v1/instruments",
        params={"symbol": query, "projection": projection},
    )
    items = resp.get("instruments") or []
    return [
        {
            "symbol": r.get("symbol"),
            "description": r.get("description"),
            "cusip": r.get("cusip"),
            "asset_type": r.get("assetType"),
            "exchange": r.get("exchange"),
        }
        for r in items[:25]
    ]


async def get_options_chain(
    client: SchwabClient, ticker: str, dte_target: int = 30, strikes_width: int = 10
) -> Dict[str, Any]:
    """Fallback for Tastytrade chain — returns ATM±width strikes with Greeks."""
    resp = await client.get(
        "/marketdata/v1/chains",
        params={
            "symbol": ticker,
            "contractType": "ALL",
            "strikeCount": max(strikes_width, 1) * 2,
            "includeUnderlyingQuote": "true",
            "daysToExpiration": dte_target,
        },
    )
    underlying = resp.get("underlying") or {}
    underlying_mark = _to_float(underlying.get("mark") or underlying.get("last"))

    strikes_out: List[Dict[str, Any]] = []
    call_map = resp.get("callExpDateMap") or {}
    put_map = resp.get("putExpDateMap") or {}

    def _merge(side_map: Dict[str, Any], side: str) -> None:
        for exp_key, strike_dict in side_map.items():
            for strike, contracts in strike_dict.items():
                c = (contracts or [{}])[0]
                strikes_out.append(
                    {
                        "expiration": exp_key.split(":")[0],
                        "dte": _to_float(c.get("daysToExpiration")),
                        "strike": _to_float(strike),
                        "side": side,
                        "bid": _to_float(c.get("bid")),
                        "ask": _to_float(c.get("ask")),
                        "mark": _to_float(c.get("mark")),
                        "iv": _to_float(c.get("volatility")),
                        "delta": _to_float(c.get("delta")),
                        "gamma": _to_float(c.get("gamma")),
                        "theta": _to_float(c.get("theta")),
                        "vega": _to_float(c.get("vega")),
                        "open_interest": _to_float(c.get("openInterest")),
                        "volume": _to_float(c.get("totalVolume")),
                    }
                )

    _merge(call_map, "CALL")
    _merge(put_map, "PUT")

    # First expiration returned — Schwab picks nearest to dte_target.
    expiration = strikes_out[0]["expiration"] if strikes_out else None
    return {
        "ticker": ticker,
        "expiration": expiration,
        "underlying_mark": underlying_mark,
        "strikes": strikes_out,
    }
