import logging
import os
import requests
import pandas as pd
import json
from datetime import datetime
from io import StringIO

logger = logging.getLogger(__name__)

API_BASE_URL = "https://www.alphavantage.co/query"
_DEFAULT_TIMEOUT = float(os.environ.get("ALPHA_VANTAGE_TIMEOUT", "15"))

def get_api_key() -> str:
    """Retrieve the API key for Alpha Vantage from environment variables."""
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set.")
    return api_key

def format_datetime_for_api(date_input) -> str:
    """Convert various date formats to YYYYMMDDTHHMM format required by Alpha Vantage API."""
    if isinstance(date_input, str):
        # If already in correct format, return as-is
        if len(date_input) == 13 and 'T' in date_input:
            return date_input
        # Try to parse common date formats
        try:
            dt = datetime.strptime(date_input, "%Y-%m-%d")
            return dt.strftime("%Y%m%dT0000")
        except ValueError:
            try:
                dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y%m%dT%H%M")
            except ValueError:
                raise ValueError(f"Unsupported date format: {date_input}")
    elif isinstance(date_input, datetime):
        return date_input.strftime("%Y%m%dT%H%M")
    else:
        raise ValueError(f"Date must be string or datetime object, got {type(date_input)}")

class AlphaVantageRateLimitError(Exception):
    """Raised for ANY Alpha Vantage condition that should trigger fallback:
    rate limit, missing/invalid key, transport errors, timeout, 4xx/5xx,
    or an error-shaped JSON envelope. The name is kept for backwards
    compatibility with route_to_vendor's except-clause.
    """
    pass


def _make_api_request(function_name: str, params: dict) -> dict | str:
    """Helper function to make API requests and handle responses.

    Raises:
        AlphaVantageRateLimitError: For any condition that should cause the
            vendor router to fall back to the next vendor (missing key,
            rate limit, timeout, 4xx/5xx, malformed response).
    """
    # Resolve API key up front; missing key = fallback, not hard crash.
    try:
        api_key = get_api_key()
    except ValueError as err:
        raise AlphaVantageRateLimitError(str(err)) from err

    # Create a copy of params to avoid modifying the original
    api_params = params.copy()
    api_params.update({
        "function": function_name,
        "apikey": api_key,
        "source": "trading_agents",
    })

    # Handle entitlement parameter if present in params or global variable
    current_entitlement = globals().get('_current_entitlement')
    entitlement = api_params.get("entitlement") or current_entitlement

    if entitlement:
        api_params["entitlement"] = entitlement
    elif "entitlement" in api_params:
        # Remove entitlement if it's None or empty
        api_params.pop("entitlement", None)

    try:
        response = requests.get(API_BASE_URL, params=api_params, timeout=_DEFAULT_TIMEOUT)
    except requests.exceptions.RequestException as err:
        # Timeout, DNS failure, connection reset — fall back.
        raise AlphaVantageRateLimitError(f"Alpha Vantage transport error: {err}") from err

    if response.status_code >= 400:
        # 4xx/5xx — fall back. Body is usually a short JSON or HTML error.
        body = (response.text or "")[:200]
        raise AlphaVantageRateLimitError(
            f"Alpha Vantage HTTP {response.status_code}: {body!r}"
        )

    response_text = response.text

    # Check if response is JSON (error responses are typically JSON)
    try:
        response_json = json.loads(response_text)
    except json.JSONDecodeError:
        # Response is not JSON (likely CSV data), which is normal.
        return response_text

    if isinstance(response_json, dict):
        # Alpha Vantage packs various error conditions into different keys;
        # treat every one of them as a fallback trigger rather than returning
        # the error object as if it were data.
        for err_key in ("Information", "Note", "Error Message", "Message"):
            val = response_json.get(err_key)
            if isinstance(val, str) and val.strip():
                raise AlphaVantageRateLimitError(
                    f"Alpha Vantage {err_key}: {val}"
                )

    return response_text



def _filter_csv_by_date_range(csv_data: str, start_date: str, end_date: str) -> str:
    """
    Filter CSV data to include only rows within the specified date range.

    Args:
        csv_data: CSV string from Alpha Vantage API
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Filtered CSV string
    """
    if not csv_data or csv_data.strip() == "":
        return csv_data

    try:
        # Parse CSV data
        df = pd.read_csv(StringIO(csv_data))

        # Assume the first column is the date column (timestamp)
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])

        # Filter by date range
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)

        filtered_df = df[(df[date_col] >= start_dt) & (df[date_col] <= end_dt)]

        # Convert back to CSV string
        return filtered_df.to_csv(index=False)

    except Exception as e:
        # If filtering fails, return original data with a warning
        print(f"Warning: Failed to filter CSV data by date range: {e}")
        return csv_data
