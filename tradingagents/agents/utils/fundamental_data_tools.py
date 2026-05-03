"""LangChain ``@tool`` wrappers for company fundamentals via the Gateway."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool

from tradingagents.gateway_client import GatewayError, call

_TARGET = "data-tools"


def _str_result(result: Any) -> str:
    return result if isinstance(result, str) else str(result)


@tool
def get_fundamentals(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    try:
        return _str_result(
            call(f"{_TARGET}___get_fundamentals", {"ticker": ticker, "curr_date": curr_date})
        )
    except GatewayError as err:
        return f"[get_fundamentals unavailable: {err}]"


@tool
def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve balance sheet data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing balance sheet data
    """
    try:
        return _str_result(
            call(
                f"{_TARGET}___get_balance_sheet",
                {"ticker": ticker, "freq": freq, "curr_date": curr_date},
            )
        )
    except GatewayError as err:
        return f"[get_balance_sheet unavailable: {err}]"


@tool
def get_cashflow(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve cash flow statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing cash flow statement data
    """
    try:
        return _str_result(
            call(
                f"{_TARGET}___get_cashflow",
                {"ticker": ticker, "freq": freq, "curr_date": curr_date},
            )
        )
    except GatewayError as err:
        return f"[get_cashflow unavailable: {err}]"


@tool
def get_income_statement(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve income statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing income statement data
    """
    try:
        return _str_result(
            call(
                f"{_TARGET}___get_income_statement",
                {"ticker": ticker, "freq": freq, "curr_date": curr_date},
            )
        )
    except GatewayError as err:
        return f"[get_income_statement unavailable: {err}]"
