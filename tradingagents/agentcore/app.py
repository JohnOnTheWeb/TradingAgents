"""FastAPI entrypoint for the AgentCore Runtime container.

AgentCore Runtime requires two endpoints on port 8080:

* ``GET  /ping``        — health check
* ``POST /invocations`` — run the agent once

The invocation payload is free-form JSON; we expect:

.. code-block:: json

    {
      "ticker": "NVDA",
      "trade_date": "2026-04-30",            // optional, defaults to today (UTC)
      "analysts": ["market", "news"],        // optional subset
      "debate_rounds": 1,                     // optional
      "deep_model": "us.anthropic...",        // optional overrides
      "quick_model": "us.anthropic...",
      "run_id": "<uuid>",                     // optional; Step Functions supplies it
      "write_report": true                    // default true; skip md-store write when false
    }

The response body is a JSON object with the per-ticker report key, raw
decision, per-model token usage, total cost, and wall-clock duration —
everything the Step Functions aggregator needs to build the summary.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
import uuid
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tradingagents.agentcore.bedrock_rates import summarize, total_cost
from tradingagents.agentcore.report_format import render_ticker_report
from tradingagents.agentcore.report_writer import (
    ReportWriteError,
    report_filename,
    write_report,
)
from tradingagents.agentcore.token_tracker import PerModelTokenTracker
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

app = FastAPI(title="TradingAgents on AgentCore")


class InvocationPayload(BaseModel):
    ticker: str
    trade_date: Optional[str] = None
    analysts: Optional[List[str]] = None
    debate_rounds: int = 1
    deep_model: Optional[str] = None
    quick_model: Optional[str] = None
    run_id: Optional[str] = None
    write_report: bool = True


class InvocationResponse(BaseModel):
    ticker: str
    trade_date: str
    run_id: str
    status: str
    duration_seconds: float
    decision: str
    report_key: Optional[str] = None
    token_usage: List[Dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = 0.0
    error: Optional[str] = None


@app.get("/ping")
def ping() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/invocations", response_model=InvocationResponse)
def invocations(payload: InvocationPayload) -> InvocationResponse:
    run_id = payload.run_id or str(uuid.uuid4())
    trade_date = payload.trade_date or date.today().isoformat()
    started = time.monotonic()

    tracker = PerModelTokenTracker()
    try:
        final_state, decision = _run_pipeline(payload, trade_date, tracker)
        duration = time.monotonic() - started
        buckets = tracker.as_list()
        priced = summarize(buckets)
        cost = total_cost(buckets)

        report_key: Optional[str] = None
        if payload.write_report:
            markdown = render_ticker_report(
                ticker=payload.ticker,
                trade_date=trade_date,
                run_id=run_id,
                status="success",
                duration_seconds=duration,
                final_state=final_state,
                decision=decision,
                token_buckets=buckets,
            )
            try:
                report_key = write_report(
                    report_filename(payload.ticker, trade_date), markdown
                )
            except ReportWriteError as err:
                # Don't fail the whole invocation just because md-store is down —
                # the Step Functions orchestrator can still record the outcome
                # and surface the write error in the summary.
                logger.error("md-store write failed for %s: %s", payload.ticker, err)
                return InvocationResponse(
                    ticker=payload.ticker,
                    trade_date=trade_date,
                    run_id=run_id,
                    status="report_write_failed",
                    duration_seconds=duration,
                    decision=decision,
                    token_usage=priced,
                    cost_usd=cost,
                    error=str(err),
                )

        return InvocationResponse(
            ticker=payload.ticker,
            trade_date=trade_date,
            run_id=run_id,
            status="success",
            duration_seconds=duration,
            decision=decision,
            report_key=report_key,
            token_usage=priced,
            cost_usd=cost,
        )
    except HTTPException:
        raise
    except Exception as err:  # noqa: BLE001 - we want to capture everything
        duration = time.monotonic() - started
        tb = traceback.format_exc()
        logger.error(
            "invocation failed for ticker=%s run_id=%s:\n%s",
            payload.ticker, run_id, tb,
        )
        buckets = tracker.as_list()
        priced = summarize(buckets)
        cost = total_cost(buckets)
        return InvocationResponse(
            ticker=payload.ticker,
            trade_date=trade_date,
            run_id=run_id,
            status="failed",
            duration_seconds=duration,
            decision="",
            token_usage=priced,
            cost_usd=cost,
            error=f"{type(err).__name__}: {err}",
        )


def _run_pipeline(
    payload: InvocationPayload,
    trade_date: str,
    tracker: PerModelTokenTracker,
):
    selected = payload.analysts or ["market", "social", "news", "fundamentals"]
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = "bedrock"
    cfg["deep_think_llm"] = payload.deep_model or os.getenv(
        "BEDROCK_DEEP_THINK_MODEL", "us.anthropic.claude-opus-4-7"
    )
    cfg["quick_think_llm"] = payload.quick_model or os.getenv(
        "BEDROCK_QUICK_THINK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    cfg["anthropic_effort"] = None
    cfg["max_debate_rounds"] = payload.debate_rounds
    cfg["data_vendors"] = {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    }

    ta = TradingAgentsGraph(
        selected_analysts=selected,
        debug=False,
        config=cfg,
        callbacks=[tracker],
    )
    return ta.propagate(payload.ticker, trade_date)
