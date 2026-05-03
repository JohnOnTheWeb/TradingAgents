"""LangChain ``@tool`` wrappers for OHLCV stock-price data.

All calls flow through the AgentCore Gateway (``data-tools`` target).
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.gateway_client import GatewayError, call

_TARGET = "data-tools"


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    try:
        result = call(
            f"{_TARGET}___get_stock_data",
            {"symbol": symbol, "start_date": start_date, "end_date": end_date},
        )
    except GatewayError as err:
        return f"[get_stock_data unavailable: {err}]"
    return result if isinstance(result, str) else str(result)
