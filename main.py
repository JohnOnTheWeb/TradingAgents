"""Local/CLI driver for TradingAgents.

Calls ``run_ticker`` with a single ticker (NVDA by default, today's date).
The AgentCore container entrypoint (``tradingagents.agentcore.app``) imports
and invokes ``run_ticker`` directly — keep the signature stable.
"""

import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


def _build_config(
    deep_model: Optional[str] = None,
    quick_model: Optional[str] = None,
    analysts: Optional[List[str]] = None,
    debate_rounds: int = 1,
) -> Dict[str, Any]:
    """Build a Bedrock-targeted TradingAgents config with sensible defaults."""
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = "bedrock"
    cfg["deep_think_llm"] = deep_model or os.getenv(
        "BEDROCK_DEEP_THINK_MODEL", "us.anthropic.claude-opus-4-7"
    )
    cfg["quick_think_llm"] = quick_model or os.getenv(
        "BEDROCK_QUICK_THINK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    # Extended thinking is incompatible with the structured-output tool-call
    # path used by Research Manager / Trader / Portfolio Manager on
    # ChatBedrockConverse, so leave it disabled regardless of caller input.
    cfg["anthropic_effort"] = None
    cfg["max_debate_rounds"] = debate_rounds
    cfg["data_vendors"] = {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    }
    return cfg


def run_ticker(
    ticker: str,
    trade_date: Optional[str] = None,
    *,
    analysts: Optional[List[str]] = None,
    debate_rounds: int = 1,
    deep_model: Optional[str] = None,
    quick_model: Optional[str] = None,
    debug: bool = False,
) -> Tuple[Dict[str, Any], str]:
    """Run the TradingAgents pipeline for a single ticker.

    Returns ``(final_state, decision)``. ``final_state`` is the dict emitted by
    the LangGraph pipeline; ``decision`` is the parsed final trade decision.
    """
    selected = analysts or ["market", "social", "news", "fundamentals"]
    cfg = _build_config(
        deep_model=deep_model,
        quick_model=quick_model,
        debate_rounds=debate_rounds,
    )
    ta = TradingAgentsGraph(
        selected_analysts=selected,
        debug=debug,
        config=cfg,
    )
    resolved_date = trade_date or date.today().isoformat()
    final_state, decision = ta.propagate(ticker, resolved_date)
    return final_state, decision


if __name__ == "__main__":
    load_dotenv()
    load_dotenv(".env.enterprise", override=False)

    ticker = os.getenv("TRADINGAGENTS_TICKER", "NVDA")
    _, decision = run_ticker(ticker, debug=True)
    print(decision)
