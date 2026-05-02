"""Schwab failure path: ensure fail-open sources tagging and Tastytrade fallback."""

from __future__ import annotations

import pytest

from brokerage_mcp import tools as tools_mod
from brokerage_mcp.schwab.client import SchwabError
from brokerage_mcp.tastytrade.client import TastytradeError
from brokerage_mcp.tools import Broker, call_tool


@pytest.fixture(autouse=True)
def _clear_caches():
    from brokerage_mcp import cache as cache_layer

    cache_layer._CACHES.clear()


class _StubSchwabClient:
    async def aclose(self):
        pass


class _StubTTClient:
    async def aclose(self):
        pass


@pytest.fixture
def broker():
    b = Broker()
    b._schwab = _StubSchwabClient()
    b._tastytrade = _StubTTClient()
    return b


async def test_quote_falls_back_to_tastytrade(broker, monkeypatch):
    async def _schwab_raises(*a, **kw):
        raise SchwabError("503 service unavailable")

    async def _tt_ok(client, ticker):
        return {
            "ticker": ticker, "bid": None, "ask": None, "mid": 101.5, "last": 101.4,
            "volume": 1e6, "day_high": 102, "day_low": 100, "spread_bps": None,
        }

    monkeypatch.setattr(tools_mod.schwab_ep, "get_quote", _schwab_raises)
    monkeypatch.setattr(tools_mod.tt_ep, "get_quote", _tt_ok)

    out = await call_tool(broker, "get_quote", {"ticker": "AAPL"})
    assert out["sources"]["schwab"] == "failed"
    assert out["sources"]["tastytrade"] == "ok"
    assert out["data"]["last"] == 101.4


async def test_movers_degrades_to_empty_on_schwab_failure(broker, monkeypatch):
    async def _raises(*a, **kw):
        raise SchwabError("401 unauthorized")

    monkeypatch.setattr(tools_mod.schwab_ep, "get_movers", _raises)

    out = await call_tool(broker, "get_movers", {"index": "$SPX"})
    assert out["sources"]["schwab"] == "failed"
    assert out["data"] == []


async def test_vol_regime_uses_tastytrade_only(broker, monkeypatch):
    async def _tt_ok(client, ticker):
        return {"ticker": ticker, "iv_rank": 25.5, "iv_percentile": 18.2}

    monkeypatch.setattr(tools_mod.tt_ep, "get_vol_regime", _tt_ok)
    out = await call_tool(broker, "get_vol_regime", {"ticker": "AAPL"})
    assert out["sources"]["tastytrade"] == "ok"
    assert out["sources"]["schwab"] == "skipped"
    assert out["data"]["iv_rank"] == 25.5


async def test_vol_regime_tastytrade_failure(broker, monkeypatch):
    async def _tt_fail(*a, **kw):
        raise TastytradeError("429")

    monkeypatch.setattr(tools_mod.tt_ep, "get_vol_regime", _tt_fail)
    out = await call_tool(broker, "get_vol_regime", {"ticker": "AAPL"})
    assert out["sources"]["tastytrade"] == "failed"
    assert out["data"] == {"ticker": "AAPL"}


async def test_unknown_tool_raises(broker):
    with pytest.raises(KeyError):
        await call_tool(broker, "place_order", {"ticker": "AAPL"})
