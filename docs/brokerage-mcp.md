# Brokerage MCP

Read-only MCP server that unifies Schwab + Tastytrade data behind one set of
tools. Runs as a Fargate sidecar behind an internet-facing ALB (gated by a
shared-secret header) and is exposed to the AgentCore Gateway via a thin
proxy Lambda. Source lives at `brokerage_mcp/` — it's a standalone
pip-installable package so you can reuse it outside TradingAgents.

## Why

Today the analyst agents only see yfinance OHLCV, news headlines, and SEC-style
fundamentals. Brokerage data adds:

- **IV rank / IV percentile / IV-HV spread** — forward-looking volatility
  regime, not derivable from price history alone.
- **Term structure** — IV across expirations; backwardation signals stress.
- **Liquidity rank + borrow rate** — position-size gate and short-pressure
  signal.
- **Greeks + OI + volume by strike** — lets the Trader express tactical theses
  as specific contracts.
- **Earnings window (date + time-of-day + recent EPS history)** — frames the
  News agent's analysis.
- **Live bid/ask/spread_bps** — microstructure context for the Market analyst.

## Architecture

```
┌─────────────────────────┐        ┌──────────────────────────┐
│ AgentCore Runtime       │        │  brokerage-mcp (Fargate) │
│  (LangGraph analysts    │──POST──▶│  ┌───────────────────┐  │
│   call brokerage tools  │        │  │ FastAPI /mcp       │  │
│   directly)             │        │  │  tools/call JSON-  │  │
└─────────────────────────┘        │  │  RPC 2.0           │  │
                                    │  │  per-tool TTL cache│  │
┌─────────────────────────┐        │  └───────┬─────┬─────┘  │
│ Fargate task-runner     │──POST──▶│          │     │        │
│ (same)                  │        │   ┌──────▼─┐ ┌─▼──────┐ │
└─────────────────────────┘        │   │Schwab  │ │Tastytr.│ │
                                    │   │client  │ │client  │ │
┌─────────────────────────┐        │   │OAuth   │ │OAuth   │ │
│ AgentCore Gateway       │──Lambda▶│   └────────┘ └────────┘ │
│  (external MCP clients) │        │            ▲            │
└─────────────────────────┘        └────────────┼────────────┘
                                                │
                                     Secrets Manager
                                     brokerage/schwab-oauth
                                     brokerage/tastytrade-oauth
                                     brokerage/shared-secret
```

- **ALB is public** because AgentCore Runtime (`NetworkMode: PUBLIC`, a managed
  AWS service) cannot reach an internal ALB. Access is gated by a shared-secret
  header (`X-Brokerage-Secret`) checked server-side.
- **Fail-open**: every Schwab call is wrapped so a 4xx/5xx surfaces as
  `sources.schwab = "failed"` in the response. Tastytrade-only data still
  returns. The analyst-side tool wrapper also degrades gracefully when the env
  var `BROKERAGE_MCP_URL` is unset or the call returns an error.

## Tool surface (10 tools, all read-only)

| Tool | Source priority | Cache TTL |
|---|---|---|
| `get_vol_regime` | Tastytrade | 60s |
| `get_term_structure` | Tastytrade | 60s |
| `get_options_chain` | Tastytrade (strikes) ⊕ Schwab (Greeks) | 30s |
| `get_earnings_context` | Tastytrade | 1h |
| `get_liquidity` | Tastytrade | 60s |
| `get_historical_vol` | Tastytrade | 5min |
| `get_corporate_events` | Tastytrade | 1h |
| `get_quote` | Schwab → Tastytrade fallback | 5s |
| `get_movers` | Schwab only; `[]` when Schwab down | 30s |
| `search_instruments` | Schwab → Tastytrade fallback | 1h |

## Per-agent wiring

All four analyst `ToolNode`s get role-relevant brokerage tools. The researcher,
Risk, and Trader prompts are extended with interpretation hints for the signals
that surface in analyst reports.

| Agent | Tools added |
|---|---|
| **Market** | `get_brokerage_quote`, `get_movers` |
| **Social** | `get_liquidity`, `get_vol_regime` |
| **News** | `get_earnings_context`, `get_corporate_events` |
| **Fundamentals** | `get_liquidity`, `get_vol_regime`, `get_corporate_events`, `search_instruments` |
| **Bull / Bear researchers** | (no tools; read enriched reports) |
| **Risk team** | (no tools; read enriched reports) |
| **Trader** | (no tools; reads enriched reports) |
| **Portfolio Manager** | unchanged |

## Deploy

1. **Phase A — infra only** (`brokerageEnabled=true`, `agentCoreEnabled=<current>`):
   ```bash
   cdk deploy -c brokerageEnabled=true -c agentCoreEnabled=true
   ```
   Creates: ECR repo (`brokerage-mcp`), CodeBuild project (`brokerage-mcp-build`),
   Fargate cluster + service, internet-facing ALB (open on :80, server-side
   secret-gated), three Secrets Manager secrets (populated below), Gateway
   Lambda proxy (`ta-mcp-brokerage`), Gateway target. `BROKERAGE_MCP_URL` +
   `BROKERAGE_SHARED_SECRET_ID` injected on AgentCore Runtime + Fargate task.

2. **Populate tokens** (one-time, or when Schwab refresh expires):
   ```
   /brokerage-refresh
   ```
   (See the skill at `repo/.claude/skills/brokerage-refresh/SKILL.md`.)

3. **Trigger CodeBuild** for the brokerage image:
   ```bash
   aws codebuild start-build --project-name brokerage-mcp-build --profile IGENV
   ```
   First build populates the ECR repo so the Fargate service can pull.

4. **Force Fargate to pull the new image** (tag is `:latest`, mutable):
   ```bash
   aws ecs update-service \
     --cluster brokerage-mcp \
     --service brokerage-mcp \
     --force-new-deployment --profile IGENV
   ```

5. **Bump AgentCore Runtime version** so it re-pulls the tradingagents image
   (which includes the new brokerage-tool wiring):
   ```bash
   aws bedrock-agentcore-control update-agent-runtime \
     --agent-runtime-id tradingagents_runtime-<suffix> \
     --role-arn ... \
     --agent-runtime-artifact '...' \
     --network-configuration '{"networkMode":"PUBLIC"}' \
     --protocol-configuration '{"serverProtocol":"HTTP"}' \
     --environment-variables '{...includes BROKERAGE_MCP_URL + BROKERAGE_SHARED_SECRET_ID...}'
   ```

## Verify

Smoke-test the ALB directly (need the shared secret):

```bash
SECRET=$(aws secretsmanager get-secret-value \
  --secret-id brokerage/shared-secret --profile IGENV \
  --query SecretString --output text | jq -r .secret)
URL=$(aws cloudformation describe-stacks --stack-name TradingAgentsStack \
  --profile IGENV --query 'Stacks[0].Outputs[?OutputKey==`BrokerageMcpUrl`].OutputValue' \
  --output text)

curl -s "$URL" \
  -H "Content-Type: application/json" \
  -H "X-Brokerage-Secret: $SECRET" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_vol_regime","arguments":{"ticker":"AAPL"}}}' \
  | jq .result.content[0].json
```

Expected response shape:
```json
{
  "data": { "ticker": "AAPL", "iv_rank": 23.4, "iv_percentile": 18.1, ... },
  "sources": { "schwab": "skipped", "tastytrade": "ok" }
}
```

Agent integration: run any analyst pipeline — tool calls to `get_vol_regime`
etc. appear in OpenSearch Dashboards as nested spans under `ta.agent_node`.

## Adding a new tool

1. Add the endpoint wrapper in either `brokerage_mcp/schwab/endpoints.py` or
   `brokerage_mcp/tastytrade/endpoints.py`.
2. Add a dispatch entry + JSON schema in `brokerage_mcp/tools.py` (`DISPATCH`
   and `TOOL_SCHEMAS`). Pick a cache TTL in `brokerage_mcp/cache.py`.
3. Wrap it in `tradingagents/agents/utils/brokerage_tools.py` with
   `@tool` + a docstring.
4. Re-export from `tradingagents/agents/utils/agent_utils.py`.
5. Add it to the relevant analyst's `tools = [...]` list and bind it to the
   matching `ToolNode` in `tradingagents/graph/trading_graph.py`.
6. Add an entry in the Gateway target's `InlinePayload` in
   `infra/lib/tradingagents-stack.ts` (so external MCP clients can see it too).
7. `pytest brokerage_mcp/tests/` — the `test_schemas.py` golden list will fail
   until you add the new tool name there.

## Rollback

```bash
cdk deploy -c brokerageEnabled=false
```

The env-var injection disappears on the next Runtime/Fargate deploy; brokerage
tools become no-ops (return `data: null, error: "BROKERAGE_MCP_URL not
configured"`). The Fargate service, ALB, ECR repo, and Secrets Manager secrets
are retained so re-enabling is one `cdk deploy` away.
