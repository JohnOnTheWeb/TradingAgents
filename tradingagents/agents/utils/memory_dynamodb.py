"""DynamoDB-backed decision log — drop-in substitute for :class:`TradingMemoryLog`.

Table shape (created by CDK, so we only read/write here):

* Partition key: ``ticker``  (String)
* Sort key:      ``trade_date`` (String, YYYY-MM-DD)
* Attributes:
    - ``status``         "pending" | "resolved"
    - ``rating``         five-tier rating parsed from the decision
    - ``decision``       raw decision text
    - ``reflection``     reflection text once the outcome has been resolved
    - ``raw_return``     +/- decimal (e.g. 0.0312)
    - ``alpha_return``   +/- decimal vs SPY
    - ``holding_days``   integer
    - ``created_at``     ISO timestamp
    - ``resolved_at``    ISO timestamp (only on resolved rows)
* GSI ``status-index`` on ``status`` (HASH) so we can list pending rows
  without a full scan.

The class emulates the on-disk log's public surface so the rest of the
pipeline (``TradingAgentsGraph._resolve_pending_entries``, the PM prompt
injection) doesn't need to know which backend is active.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tradingagents.agents.utils.rating import parse_rating

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_pct(value: Optional[float]) -> Optional[str]:
    return f"{value:+.1%}" if isinstance(value, (int, float)) else None


class DynamoDBMemoryLog:
    """DynamoDB implementation of the decision log."""

    _TABLE_ENV = "TRADINGAGENTS_MEMORY_TABLE"
    _GSI_NAME = "status-index"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self._table_name = cfg.get("memory_table") or os.environ.get(self._TABLE_ENV)
        if not self._table_name:
            raise RuntimeError(
                f"DynamoDBMemoryLog requires {self._TABLE_ENV} env var "
                "or memory_table config key"
            )
        import boto3  # lazy

        region = (
            cfg.get("aws_region")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        self._table = boto3.resource("dynamodb", region_name=region).Table(
            self._table_name
        )
        self._max_entries = cfg.get("memory_log_max_entries")  # currently unused

    # --- Write path ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
    ) -> None:
        """Append a pending row. Idempotent on (ticker, trade_date)."""
        rating = parse_rating(final_trade_decision)
        # ConditionExpression prevents double-writes on the same (ticker, date).
        try:
            self._table.put_item(
                Item={
                    "ticker": ticker,
                    "trade_date": trade_date,
                    "status": "pending",
                    "rating": rating,
                    "decision": final_trade_decision,
                    "created_at": _now(),
                },
                ConditionExpression="attribute_not_exists(ticker) AND attribute_not_exists(trade_date)",
            )
        except Exception as err:  # noqa: BLE001
            # ConditionalCheckFailedException → already stored, fine.
            name = getattr(err, "response", {}).get("Error", {}).get("Code")
            if name == "ConditionalCheckFailedException":
                return
            raise

    def batch_update_with_outcomes(self, updates: List[Dict[str, Any]]) -> None:
        """Resolve multiple pending rows.

        Each update dict: ``{ticker, trade_date, raw_return, alpha_return,
        holding_days, reflection}``.
        """
        for u in updates:
            self._table.update_item(
                Key={"ticker": u["ticker"], "trade_date": u["trade_date"]},
                UpdateExpression=(
                    "SET #s = :resolved, "
                    "raw_return = :raw, "
                    "alpha_return = :alpha, "
                    "holding_days = :days, "
                    "reflection = :refl, "
                    "resolved_at = :ts"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":resolved": "resolved",
                    ":raw": self._decimal(u["raw_return"]),
                    ":alpha": self._decimal(u["alpha_return"]),
                    ":days": int(u["holding_days"]),
                    ":refl": u["reflection"],
                    ":ts": _now(),
                },
            )

    # --- Read path ---

    def get_pending_entries(self) -> List[Dict[str, Any]]:
        """Return all pending rows across every ticker (via GSI)."""
        kwargs: Dict[str, Any] = {
            "IndexName": self._GSI_NAME,
            "KeyConditionExpression": "#s = :pending",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":pending": "pending"},
        }
        items: List[Dict[str, Any]] = []
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        return [self._normalize(i) for i in items]

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3
    ) -> str:
        """Same-ticker recent decisions + recent cross-ticker lessons."""
        same = self._recent_same(ticker, limit=n_same)
        cross = self._recent_cross(ticker, limit=n_cross)

        if not same and not cross:
            return ""

        parts: List[str] = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    # --- Helpers ---

    def _recent_same(self, ticker: str, limit: int) -> List[Dict[str, Any]]:
        resp = self._table.query(
            KeyConditionExpression="ticker = :t",
            FilterExpression="#s = :resolved",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":t": ticker, ":resolved": "resolved"},
            ScanIndexForward=False,
            Limit=limit * 3,  # oversample because the status filter runs post-query
        )
        items = [self._normalize(i) for i in resp.get("Items", [])]
        return items[:limit]

    def _recent_cross(self, ticker: str, limit: int) -> List[Dict[str, Any]]:
        # Query the status GSI for resolved rows, then drop same-ticker matches.
        kwargs: Dict[str, Any] = {
            "IndexName": self._GSI_NAME,
            "KeyConditionExpression": "#s = :resolved",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":resolved": "resolved"},
            "ScanIndexForward": False,
            "Limit": limit * 3,
        }
        resp = self._table.query(**kwargs)
        items = [self._normalize(i) for i in resp.get("Items", [])]
        return [e for e in items if e["ticker"] != ticker][:limit]

    @staticmethod
    def _decimal(value: Any) -> Any:
        # boto3 requires Decimal for numeric writes; import lazily
        from decimal import Decimal

        return Decimal(str(value))

    @staticmethod
    def _normalize(item: Dict[str, Any]) -> Dict[str, Any]:
        """Translate DDB item shape → dict matching the file-backend format."""
        status = item.get("status")
        raw_return = item.get("raw_return")
        alpha_return = item.get("alpha_return")
        return {
            "ticker": item.get("ticker"),
            "date": item.get("trade_date"),
            "rating": item.get("rating", "Hold"),
            "pending": status == "pending",
            "raw": _fmt_pct(float(raw_return)) if raw_return is not None else None,
            "alpha": (
                _fmt_pct(float(alpha_return)) if alpha_return is not None else None
            ),
            "holding": (
                f"{int(item['holding_days'])}d" if "holding_days" in item else None
            ),
            "decision": item.get("decision", ""),
            "reflection": item.get("reflection", ""),
        }

    @staticmethod
    def _format_full(e: Dict[str, Any]) -> str:
        raw = e.get("raw") or "n/a"
        alpha = e.get("alpha") or "n/a"
        holding = e.get("holding") or "n/a"
        tag = (
            f"[{e['date']} | {e['ticker']} | {e['rating']} | "
            f"{raw} | {alpha} | {holding}]"
        )
        parts = [tag, f"DECISION:\n{e['decision']}"]
        if e.get("reflection"):
            parts.append(f"REFLECTION:\n{e['reflection']}")
        return "\n\n".join(parts)

    @staticmethod
    def _format_reflection_only(e: Dict[str, Any]) -> str:
        tag = (
            f"[{e['date']} | {e['ticker']} | {e['rating']} | "
            f"{e.get('raw') or 'n/a'}]"
        )
        if e.get("reflection"):
            return f"{tag}\n{e['reflection']}"
        decision = e.get("decision", "")
        text = decision[:300]
        suffix = "..." if len(decision) > 300 else ""
        return f"{tag}\n{text}{suffix}"
