from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

from dotenv import load_dotenv

# Load base env (.env) and enterprise overlay (.env.enterprise) if present.
# The enterprise overlay holds AWS / Bedrock credentials and model IDs.
load_dotenv()
load_dotenv(".env.enterprise", override=False)

config = DEFAULT_CONFIG.copy()

# Bedrock-hosted Claude Opus 4.7 for deep reasoning (Research Manager,
# Trader, Portfolio Manager, Risk team) and Sonnet 4.5 for the fast
# analyst/tool-call path. Override via BEDROCK_DEEP_THINK_MODEL /
# BEDROCK_QUICK_THINK_MODEL in .env.enterprise if you prefer different IDs.
import os
config["llm_provider"] = "bedrock"
config["deep_think_llm"] = os.getenv(
    "BEDROCK_DEEP_THINK_MODEL", "us.anthropic.claude-opus-4-7"
)
config["quick_think_llm"] = os.getenv(
    "BEDROCK_QUICK_THINK_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
# NOTE: extended thinking is disabled because three agents (Research Manager,
# Trader, Portfolio Manager) use structured output via forced tool calling,
# which ChatBedrockConverse does not support while thinking is enabled.
# Set this to "low"/"medium"/"high" only if you remove structured output
# from those agents or switch to the direct Anthropic API.
config["anthropic_effort"] = None
config["max_debate_rounds"] = 1

# yfinance vendor path requires no API keys and is the default.
config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}

ta = TradingAgentsGraph(debug=True, config=config)

from datetime import date
_, decision = ta.propagate("NVDA", date.today().isoformat())
print(decision)
