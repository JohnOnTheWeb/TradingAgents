"""Tool schemas stay in sync with the dispatch table and agent-side bindings."""

from brokerage_mcp.tools import DISPATCH, TOOL_SCHEMAS


EXPECTED_TOOLS = {
    "get_vol_regime",
    "get_term_structure",
    "get_options_chain",
    "get_earnings_context",
    "get_liquidity",
    "get_historical_vol",
    "get_corporate_events",
    "get_quote",
    "get_movers",
    "search_instruments",
}


def test_schemas_match_dispatch():
    assert set(DISPATCH) == set(TOOL_SCHEMAS)


def test_expected_tools_all_present():
    assert EXPECTED_TOOLS == set(DISPATCH)


def test_every_schema_has_description_and_input():
    for name, s in TOOL_SCHEMAS.items():
        assert s.get("description"), f"{name} missing description"
        assert s.get("input_schema", {}).get("type") == "object", f"{name} invalid input_schema"
