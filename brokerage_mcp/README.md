# brokerage-mcp

Read-only MCP server that unifies Schwab + Tastytrade data behind a single set
of tools. Purpose: give downstream agents volatility-regime, term-structure,
earnings-context, liquidity, options-chain, quotes, and movers without having
to speak either vendor API directly.

## Design

- **Read-only.** No order placement, no dry-run, no trading endpoints.
- **Schwab fails open.** If Schwab returns a 4xx/5xx, the request still
  succeeds with whatever Tastytrade returned, and the response payload
  marks `sources.schwab = "failed"`.
- **OAuth refresh tokens** are loaded from Secrets Manager in production
  (`brokerage/schwab-oauth`, `brokerage/tastytrade-oauth`) and from
  `.brokerage-tokens.json` locally. Both shapes: `{"refresh_token": "...", "client_id": "...", "client_secret": "..."}`.
- **Access tokens** are cached in-process with a 60s safety buffer.
- **Tool results** are cached with a per-tool TTL (5s for quotes, 30s for
  chains, 60s for regime data, 1h for earnings/corporate events).

## Tool surface (10 tools)

| Tool | Source priority |
|---|---|
| `get_vol_regime` | Tastytrade |
| `get_term_structure` | Tastytrade |
| `get_options_chain` | Tastytrade → Schwab |
| `get_earnings_context` | Tastytrade |
| `get_liquidity` | Tastytrade |
| `get_historical_vol` | Tastytrade |
| `get_corporate_events` | Tastytrade |
| `get_quote` | Schwab → Tastytrade |
| `get_movers` | Schwab (degraded `[]` on failure) |
| `search_instruments` | Schwab → Tastytrade |

All tool responses are wrapped:

```json
{
  "data": { ... },
  "sources": { "schwab": "ok|failed|skipped", "tastytrade": "ok|failed|skipped" }
}
```

## Run locally

```bash
cd brokerage_mcp
pip install -e ".[dev]"

# Populate .brokerage-tokens.json — easiest via the repo-local skill:
#   In Claude Code:  /brokerage-refresh

uvicorn brokerage_mcp.server:app --host 0.0.0.0 --port 8080
```

Smoke-test one tool:

```bash
curl -s localhost:8080/mcp -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {"name": "get_vol_regime", "arguments": {"ticker": "AAPL"}}
}' | jq
```

## Refresh tokens

Tokens expire and require periodic re-auth (Schwab refresh tokens last 7 days;
Tastytrade refresh tokens last longer but still rotate). Use the local skill:

```
/brokerage-refresh
```

The skill walks through the Schwab browser-redirect OAuth flow, exchanges the
callback code, and writes new refresh tokens to both Secrets Manager and the
local `.brokerage-tokens.json`.
