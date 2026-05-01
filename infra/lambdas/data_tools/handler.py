"""AgentCore Gateway MCP-target Lambda: market data tools.

Gateway invokes this Lambda with ``event = {"tool_name": "...", "tool_arguments": {...}}``
(or ``event["__name"]`` on older Gateway releases). We dispatch to the
appropriate vendor-abstracted helper in ``tradingagents.dataflows.interface``.

The Lambda image bundles the full ``tradingagents`` package so every data
helper is available without duplicating code here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _tool_dispatch() -> Dict[str, Callable[..., Any]]:
    # Import lazily so handler cold start stays fast on invocations that
    # error before dispatch (e.g. bad tool name).
    from tradingagents.agents.utils.agent_utils import (
        get_balance_sheet,
        get_cashflow,
        get_fundamentals,
        get_global_news,
        get_income_statement,
        get_indicators,
        get_insider_transactions,
        get_news,
        get_stock_data,
    )
    return {
        "get_stock_data": get_stock_data,
        "get_indicators": get_indicators,
        "get_fundamentals": get_fundamentals,
        "get_balance_sheet": get_balance_sheet,
        "get_cashflow": get_cashflow,
        "get_income_statement": get_income_statement,
        "get_news": get_news,
        "get_insider_transactions": get_insider_transactions,
        "get_global_news": get_global_news,
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    tool_name = event.get("tool_name") or event.get("__name") or ""
    args = event.get("tool_arguments") or event.get("arguments") or {}
    if not tool_name:
        raise ValueError("tool_name is required")

    dispatch = _tool_dispatch()
    fn = dispatch.get(tool_name)
    if fn is None:
        raise ValueError(
            f"Unknown tool: {tool_name!r}. "
            f"Known tools: {sorted(dispatch)}"
        )

    # The agent_utils functions are LangChain @tool-decorated callables; they
    # expose `.invoke(dict)` for structured input.
    try:
        result = fn.invoke(args) if hasattr(fn, "invoke") else fn(**args)
    except Exception as err:  # noqa: BLE001
        logger.error("tool %s failed: %s", tool_name, err, exc_info=True)
        raise

    if not isinstance(result, (dict, list, str, int, float, bool)) and result is not None:
        result = str(result)
    return {"tool_name": tool_name, "result": result}
