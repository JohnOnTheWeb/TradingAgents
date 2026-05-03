"""LangChain ``@tool`` wrappers for news data via the Gateway."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool

from tradingagents.gateway_client import GatewayError, call

_TARGET = "data-tools"


def _str_result(result: Any) -> str:
    return result if isinstance(result, str) else str(result)


@tool
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve news data for a given ticker symbol.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    try:
        return _str_result(
            call(
                f"{_TARGET}___get_news",
                {"ticker": ticker, "start_date": start_date, "end_date": end_date},
            )
        )
    except GatewayError as err:
        return f"[get_news unavailable: {err}]"


@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "Number of days to look back"] = 7,
    limit: Annotated[int, "Maximum number of articles to return"] = 5,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor.
    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back (default 7)
        limit (int): Maximum number of articles to return (default 5)
    Returns:
        str: A formatted string containing global news data
    """
    try:
        return _str_result(
            call(
                f"{_TARGET}___get_global_news",
                {"curr_date": curr_date, "look_back_days": look_back_days, "limit": limit},
            )
        )
    except GatewayError as err:
        return f"[get_global_news unavailable: {err}]"


@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
    Returns:
        str: A report of insider transaction data
    """
    try:
        return _str_result(
            call(f"{_TARGET}___get_insider_transactions", {"ticker": ticker})
        )
    except GatewayError as err:
        return f"[get_insider_transactions unavailable: {err}]"
