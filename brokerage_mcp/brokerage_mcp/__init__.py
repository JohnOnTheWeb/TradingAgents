"""Read-only MCP server over Schwab + Tastytrade brokerage APIs.

All tools are information-retrieval only. No order placement, no trading
endpoints. Schwab calls fail-open: if Schwab errors, the Tastytrade result
(when available) is returned alone with ``sources.schwab = "failed"``.
"""

__version__ = "0.1.0"
