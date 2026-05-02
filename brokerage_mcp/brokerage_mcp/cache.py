"""Tiny TTL cache wrapper around cachetools — one cache per tool name.

Each entry is keyed on a JSON-serializable args dict; values are the full
MCP response envelope ({"data": ..., "sources": ...}).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from cachetools import TTLCache

_CACHES: Dict[str, TTLCache] = {}
_LOCK = threading.Lock()

DEFAULT_TTLS = {
    "get_quote": 5,
    "get_movers": 30,
    "get_options_chain": 30,
    "get_vol_regime": 60,
    "get_term_structure": 60,
    "get_liquidity": 60,
    "get_historical_vol": 300,
    "get_earnings_context": 3600,
    "get_corporate_events": 3600,
    "search_instruments": 3600,
}


def _cache_for(tool: str) -> TTLCache:
    with _LOCK:
        if tool not in _CACHES:
            _CACHES[tool] = TTLCache(maxsize=512, ttl=DEFAULT_TTLS.get(tool, 60))
        return _CACHES[tool]


def _key(args: Dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, default=str)


def get(tool: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return _cache_for(tool).get(_key(args))


def put(tool: str, args: Dict[str, Any], value: Dict[str, Any]) -> None:
    _cache_for(tool)[_key(args)] = value


def clock() -> float:
    return time.monotonic()
