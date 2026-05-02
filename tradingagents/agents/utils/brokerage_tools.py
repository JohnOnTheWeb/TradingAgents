"""LangChain ``@tool`` wrappers over the brokerage-mcp sidecar.

The sidecar is deployed as an internal Fargate service + ALB; its URL is
injected into the AgentCore runtime + Fargate task as ``BROKERAGE_MCP_URL``.
When the env var is unset (local dev / no brokerage config) every tool
returns a structured "unavailable" stub so the analyst chain keeps running.

All responses are the sidecar's envelope:
    {"data": ..., "sources": {"schwab": "...", "tastytrade": "..."}}
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import tool

_logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = float(os.environ.get("BROKERAGE_MCP_TIMEOUT", "20"))
_cached_shared_secret: Optional[str] = None


def _url() -> Optional[str]:
    url = os.environ.get("BROKERAGE_MCP_URL", "").strip()
    return url or None


def _shared_secret() -> Optional[str]:
    """Resolve the MCP shared-secret header value.

    Order:
      1. BROKERAGE_SHARED_SECRET env (local/dev).
      2. Secrets Manager at BROKERAGE_SHARED_SECRET_ID, JSON key 'secret'.
    """
    global _cached_shared_secret
    if _cached_shared_secret:
        return _cached_shared_secret
    inline = (os.environ.get("BROKERAGE_SHARED_SECRET") or "").strip()
    if inline:
        _cached_shared_secret = inline
        return inline
    secret_id = (os.environ.get("BROKERAGE_SHARED_SECRET_ID") or "").strip()
    if not secret_id:
        return None
    try:
        import boto3  # lazy
        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        resp = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=secret_id
        )
        raw = resp.get("SecretString") or ""
        try:
            parsed = json.loads(raw)
            secret = str(parsed.get("secret") or raw).strip()
        except json.JSONDecodeError:
            secret = raw.strip()
    except Exception as err:  # noqa: BLE001
        _logger.warning("Failed to resolve BROKERAGE_SHARED_SECRET_ID=%s: %s", secret_id, err)
        return None
    if secret:
        _cached_shared_secret = secret
    return secret or None


def _unavailable(reason: str) -> Dict[str, Any]:
    return {
        "data": None,
        "sources": {"schwab": "skipped", "tastytrade": "skipped"},
        "error": reason,
    }


def _call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    url = _url()
    if not url:
        return _unavailable("BROKERAGE_MCP_URL not configured")
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = _shared_secret()
    if secret:
        headers["X-Brokerage-Secret"] = secret
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")[:200]
        _logger.warning("brokerage-mcp HTTP %s for %s: %s", err.code, tool_name, detail)
        return _unavailable(f"HTTP {err.code}")
    except urllib.error.URLError as err:
        _logger.warning("brokerage-mcp unreachable for %s: %s", tool_name, err.reason)
        return _unavailable(f"unreachable: {err.reason}")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _unavailable("non-JSON response")
    if "error" in parsed:
        return _unavailable(f"rpc error: {parsed['error']}")
    result = parsed.get("result") or {}
    content = result.get("content") or []
    if content and content[0].get("type") == "json":
        return content[0].get("json", {})
    return {"data": result, "sources": {"schwab": "skipped", "tastytrade": "skipped"}}


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
