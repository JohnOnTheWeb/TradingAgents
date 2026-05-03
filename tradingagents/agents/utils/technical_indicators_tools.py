"""LangChain ``@tool`` wrapper for technical indicators via the Gateway."""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.gateway_client import GatewayError, call

_TARGET = "data-tools"


@tool
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve a single technical indicator for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): A single technical indicator name, e.g. 'rsi', 'macd'. Call this tool once per indicator.
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the specified ticker symbol and indicator.
    """
    indicators = [i.strip().lower() for i in str(indicator).split(",") if i.strip()]
    if not indicators:
        return ""
    results = []
    for ind in indicators:
        try:
            r = call(
                f"{_TARGET}___get_indicators",
                {
                    "symbol": symbol,
                    "indicator": ind,
                    "curr_date": curr_date,
                    "look_back_days": look_back_days,
                },
            )
            results.append(r if isinstance(r, str) else str(r))
        except GatewayError as err:
            results.append(f"[get_indicators({ind}) unavailable: {err}]")
    return "\n\n".join(results)
