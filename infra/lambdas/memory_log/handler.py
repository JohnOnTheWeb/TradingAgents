"""AgentCore Gateway MCP-target Lambda: memory-log tools.

Exposes three tools backed by the DynamoDB memory log:

* ``get_past_context(ticker, n_same, n_cross)`` → str
* ``store_decision(ticker, trade_date, final_trade_decision)`` → None
* ``batch_update_with_outcomes(updates)`` → None

The Lambda reads :env:`TRADINGAGENTS_MEMORY_TABLE` and talks to DynamoDB
directly via the :class:`DynamoDBMemoryLog` helper in the shared package.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_memory_log = None


def _log():
    global _memory_log
    if _memory_log is None:
        from tradingagents.agents.utils.memory_dynamodb import DynamoDBMemoryLog

        _memory_log = DynamoDBMemoryLog()
    return _memory_log


def _dispatch() -> Dict[str, Callable[..., Any]]:
    log = _log()
    return {
        "get_past_context": lambda ticker, n_same=5, n_cross=3: log.get_past_context(
            ticker, n_same=int(n_same), n_cross=int(n_cross)
        ),
        "store_decision": lambda ticker, trade_date, final_trade_decision: log.store_decision(
            ticker, trade_date, final_trade_decision
        ),
        "batch_update_with_outcomes": lambda updates: log.batch_update_with_outcomes(
            list(updates)
        ),
        "get_pending_entries": lambda: log.get_pending_entries(),
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    tool_name = event.get("tool_name") or event.get("__name") or ""
    args = event.get("tool_arguments") or event.get("arguments") or {}
    if not tool_name:
        raise ValueError("tool_name is required")

    dispatch = _dispatch()
    fn = dispatch.get(tool_name)
    if fn is None:
        raise ValueError(
            f"Unknown memory-log tool: {tool_name!r}. Known: {sorted(dispatch)}"
        )

    try:
        result = fn(**args) if isinstance(args, dict) else fn(*args)
    except Exception as err:  # noqa: BLE001
        logger.error("memory tool %s failed: %s", tool_name, err, exc_info=True)
        raise

    return {"tool_name": tool_name, "result": result}
