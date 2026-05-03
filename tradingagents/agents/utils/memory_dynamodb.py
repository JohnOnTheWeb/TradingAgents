"""Gateway-backed decision log — drop-in substitute for ``TradingMemoryLog``.

This module used to talk to DynamoDB directly. After the Gateway refactor,
all reads and writes go through the ``memory-log`` target on the AgentCore
Gateway. The class name is preserved so ``TradingMemoryLog.__new__`` in
:mod:`tradingagents.agents.utils.memory` still dispatches here when
``TRADINGAGENTS_MEMORY_BACKEND=dynamodb``; a rename to ``GatewayMemoryLog``
is fine in a follow-up but isn't needed for correctness.

Tool names at the Gateway (prefix ``memory-log___``):
  * ``get_past_context(ticker, n_same, n_cross) -> str``
  * ``store_decision(ticker, trade_date, final_trade_decision) -> None``
  * ``get_pending_entries() -> list[dict]``
  * ``batch_update_with_outcomes(updates) -> None``
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tradingagents.gateway_client import GatewayError, call

logger = logging.getLogger(__name__)

_TARGET = "memory-log"


class DynamoDBMemoryLog:
    """Gateway-backed decision log; keeps the historical class name."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        # No persistent state; config is accepted only for API compatibility.
        self._config = config or {}

    # --- Write path ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
    ) -> None:
        try:
            call(
                f"{_TARGET}___store_decision",
                {
                    "ticker": ticker,
                    "trade_date": trade_date,
                    "final_trade_decision": final_trade_decision,
                },
            )
        except GatewayError as err:
            # Memory-log write failure must not crash the pipeline; the
            # original file-backed log also swallowed duplicate writes.
            logger.warning("memory-log store_decision failed: %s", err)

    def batch_update_with_outcomes(self, updates: List[Dict[str, Any]]) -> None:
        if not updates:
            return
        try:
            call(
                f"{_TARGET}___batch_update_with_outcomes",
                {"updates": list(updates)},
            )
        except GatewayError as err:
            logger.warning("memory-log batch_update_with_outcomes failed: %s", err)

    # --- Read path ---

    def get_pending_entries(self) -> List[Dict[str, Any]]:
        try:
            result = call(f"{_TARGET}___get_pending_entries", {})
        except GatewayError as err:
            logger.warning("memory-log get_pending_entries failed: %s", err)
            return []
        if isinstance(result, list):
            return result
        return []

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3
    ) -> str:
        try:
            result = call(
                f"{_TARGET}___get_past_context",
                {"ticker": ticker, "n_same": int(n_same), "n_cross": int(n_cross)},
            )
        except GatewayError as err:
            logger.warning("memory-log get_past_context failed: %s", err)
            return ""
        return result if isinstance(result, str) else ""
