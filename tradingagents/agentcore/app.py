"""FastAPI entrypoint for the AgentCore Runtime container.

AgentCore Runtime requires two endpoints on port 8080:

* ``GET  /ping``        — health check
* ``POST /invocations`` — run the agent once (streams NDJSON)

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

AgentCore Runtime enforces a 15-minute *idle* timeout — if the client
receives no response bytes for 15 minutes the request is terminated with
``RuntimeClientError``. Deep-research runs (4 analysts + multiple debate
rounds + risk discussion) regularly exceed that wall-clock.

To keep the stream alive we emit NDJSON events:

* ``{"type": "heartbeat", "elapsed": <sec>, "phase": "running"}`` every 10s
* ``{"type": "result", ...InvocationResponse...}`` when the pipeline finishes

The caller (``ta-invoke-agent`` Lambda) reads the stream line by line,
discards heartbeats, and keeps the final ``result`` event as the response.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
from datetime import date
from typing import Any, Dict, Iterator, List, Optional

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
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

# AgentCore's idle timeout is 15 min; send heartbeats every 10s with plenty
# of headroom so a slow analyst step still keeps the connection alive.
HEARTBEAT_INTERVAL_SEC = 10.0

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


@app.post("/invocations")
def invocations(payload: InvocationPayload) -> StreamingResponse:
    """Stream NDJSON heartbeat events until the agent pipeline completes."""
    run_id = payload.run_id or str(uuid.uuid4())
    trade_date = payload.trade_date or date.today().isoformat()

    def event_stream() -> Iterator[bytes]:
        started = time.monotonic()
        tracker = PerModelTokenTracker()

        # Container for the worker thread's return value.
        result_holder: Dict[str, Any] = {}

        def worker() -> None:
            try:
                final_state, decision = _run_pipeline(payload, trade_date, tracker)
                result_holder["final_state"] = final_state
                result_holder["decision"] = decision
            except BaseException as err:  # noqa: BLE001
                result_holder["error"] = err
                result_holder["traceback"] = traceback.format_exc()

        t = threading.Thread(target=worker, daemon=True, name=f"ta-run-{run_id}")
        t.start()

        while t.is_alive():
            t.join(timeout=HEARTBEAT_INTERVAL_SEC)
            if t.is_alive():
                elapsed = time.monotonic() - started
                hb = {
                    "type": "heartbeat",
                    "run_id": run_id,
                    "ticker": payload.ticker,
                    "elapsed": round(elapsed, 1),
                    "phase": "running",
                }
                yield (json.dumps(hb) + "\n").encode("utf-8")

        # Worker done — finalize the result.
        duration = time.monotonic() - started
        buckets = tracker.as_list()
        priced = summarize(buckets)
        cost = total_cost(buckets)

        if "error" in result_holder:
            err = result_holder["error"]
            logger.error(
                "invocation failed for ticker=%s run_id=%s:\n%s",
                payload.ticker, run_id, result_holder.get("traceback", ""),
            )
            final = InvocationResponse(
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
            yield _result_event(final)
            return

        final_state = result_holder.get("final_state") or {}
        decision = result_holder.get("decision", "") or ""
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
                logger.error("md-store write failed for %s: %s", payload.ticker, err)
                yield _result_event(InvocationResponse(
                    ticker=payload.ticker,
                    trade_date=trade_date,
                    run_id=run_id,
                    status="report_write_failed",
                    duration_seconds=duration,
                    decision=decision,
                    token_usage=priced,
                    cost_usd=cost,
                    error=str(err),
                ))
                return

        yield _result_event(InvocationResponse(
            ticker=payload.ticker,
            trade_date=trade_date,
            run_id=run_id,
            status="success",
            duration_seconds=duration,
            decision=decision,
            report_key=report_key,
            token_usage=priced,
            cost_usd=cost,
        ))

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def _result_event(resp: InvocationResponse) -> bytes:
    payload = {"type": "result", **resp.model_dump()}
    return (json.dumps(payload) + "\n").encode("utf-8")


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
