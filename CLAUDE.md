# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install (editable, dev-friendly):

```bash
pip install -e .
pip install -e ".[bedrock]"   # adds langchain-aws + boto3 for AWS Bedrock
```

Run the interactive CLI:

```bash
tradingagents analyze                     # installed entry point
tradingagents analyze --checkpoint        # LangGraph checkpoint resume
tradingagents analyze --clear-checkpoints # wipe per-ticker SQLite checkpoints
python -m cli.main analyze                # equivalent, from source
```

Run from code: `python main.py` (single-ticker smoke run; currently pinned to the Bedrock provider — edit `main.py` to switch providers).

Tests (pytest, configured in `pyproject.toml` with markers `unit`, `integration`, `smoke`):

```bash
pytest                                    # full suite
pytest -m unit                            # only unit-marked
pytest tests/test_structured_agents.py    # single file
pytest tests/test_structured_agents.py::test_name
```

`tests/conftest.py` auto-injects placeholder API keys for every supported provider via a `monkeypatch` autouse fixture, so the suite runs without real credentials. When adding a new provider, extend `_API_KEY_ENV_VARS` there, and prefer the `mock_llm_client` fixture over hitting a real LLM.

Docker:

```bash
docker compose run --rm tradingagents                          # default image
docker compose --profile ollama run --rm tradingagents-ollama  # with local Ollama
```

## Architecture

### LangGraph agent pipeline

`TradingAgentsGraph` (`tradingagents/graph/trading_graph.py`) is the single entry point. One `propagate(ticker, date)` call drives the full pipeline; construction is split across helper modules under `tradingagents/graph/`:

- `setup.py` (`GraphSetup`) — assembles the LangGraph `StateGraph` from analyst, researcher, manager, and risk-management nodes. The set of analyst branches is controlled by the `selected_analysts` constructor arg (`market`, `social`, `news`, `fundamentals`).
- `conditional_logic.py` — routes debate/risk-discussion loops using `max_debate_rounds` / `max_risk_discuss_rounds` from config.
- `propagation.py` — builds the initial `AgentState` and injects memory-log context for the Portfolio Manager.
- `checkpointer.py` — opt-in LangGraph `SqliteSaver` per ticker. Thread ID = `hash(ticker+date)`; different dates start fresh, same ticker+date resumes. Checkpoint DBs live under `~/.tradingagents/cache/checkpoints/<TICKER>.db` and are cleared on successful completion.
- `reflection.py` / `signal_processing.py` — post-run reflection on realized returns and final decision parsing.

Pipeline shape (all driven by `AgentState` in `tradingagents/agents/utils/agent_states.py`):

```
Analysts (market/social/news/fundamentals, parallel branches with ToolNode)
  → Bull ↔ Bear researchers (debate loop)
  → Research Manager (structured output)
  → Trader (structured output)
  → Risk team (aggressive ↔ conservative ↔ neutral, loop)
  → Portfolio Manager (structured output, sees memory-log context)
```

Each analyst branch uses the **quick-think LLM** with a `ToolNode` for its data tools; Research Manager / Trader / Portfolio Manager / Risk use the **deep-think LLM**. `_create_tool_nodes` in `trading_graph.py` is where tool-per-branch wiring lives.

### Two-LLM provider system

The framework runs two models per analysis: `deep_think_llm` (reasoning) and `quick_think_llm` (tool-calling analysts). Both share a `llm_provider`. Client construction is lazy and pluggable:

- `tradingagents/llm_clients/factory.py` — `create_llm_client(provider, model, base_url, **kwargs)`. OpenAI-compatible providers (`openai`, `xai`, `deepseek`, `qwen`, `glm`, `ollama`, `openrouter`) share one client; `anthropic`, `google`, `azure`, `bedrock` each have their own. Imports are deferred so that missing SDKs only fail when that provider is actually selected.
- `tradingagents/llm_clients/base_client.py` — `BaseLLMClient` + `normalize_content` (flattens typed-block responses from Responses API / Gemini 3 / Bedrock into a plain string that downstream agents expect).
- `tradingagents/llm_clients/model_catalog.py` — authoritative list of known models per provider/mode. `validators.py` warns on unknown IDs but does not block `ollama`, `openrouter`, or `bedrock` (custom IDs allowed).
- Provider-specific thinking knobs (`google_thinking_level`, `openai_reasoning_effort`, `anthropic_effort`) are passed through `_get_provider_kwargs` in `TradingAgentsGraph`. For Bedrock, `anthropic_effort` maps to either the adaptive-thinking API (Claude 4.7+ on Bedrock) or the legacy `thinking.budget_tokens` shape (Claude 4.5/4.6 on Bedrock); see the effort-handling block in `bedrock_client.py`.

**Important for Bedrock**: extended thinking is incompatible with the structured-output tool-call path used by Research Manager / Trader / Portfolio Manager on `ChatBedrockConverse`. `main.py` sets `anthropic_effort = None` for that reason. Enable effort only if you strip structured output from those three agents or switch to the direct Anthropic provider.

### Structured-output agents

Three agents (Research Manager, Trader, Portfolio Manager) use `tradingagents/agents/utils/structured.py`:

- `bind_structured(llm, schema, name)` wraps with `with_structured_output(schema)`; returns `None` if the provider/model doesn't support it.
- `invoke_structured_or_freetext(...)` invokes the structured path and renders results to markdown, falling back to free-text with the identical prompt on any failure.

Schemas live in `cli/schemas.py` (five-tier rating scale is shared across all three).

### Data vendor abstraction

Tools are vendor-abstracted through `tradingagents/dataflows/interface.py`. Routing is controlled by config:

```python
config["data_vendors"] = {
    "core_stock_apis": "yfinance",      # or "alpha_vantage"
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}
config["tool_vendors"] = {              # optional per-tool override
    "get_stock_data": "alpha_vantage",
}
```

Per-tool overrides in `tool_vendors` take precedence over category defaults. The top-level abstract tool methods in `tradingagents/agents/utils/agent_utils.py` (`get_stock_data`, `get_indicators`, `get_fundamentals`, `get_balance_sheet`, `get_cashflow`, `get_income_statement`, `get_news`, `get_insider_transactions`, `get_global_news`) are what the `ToolNode`s bind to — add a new vendor by implementing the same signatures in `tradingagents/dataflows/<vendor>.py` and extending the dispatch in `interface.py`.

### Persistent memory log

Always-on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md` (override with `TRADINGAGENTS_MEMORY_LOG_PATH`). On the **next run for the same ticker**, `_resolve_pending_entries` fetches realized returns (raw + alpha vs SPY) via yfinance, generates a reflection, and batch-writes resolved entries. `get_past_context` then injects recent same-ticker decisions plus cross-ticker lessons into the Portfolio Manager prompt.

Pending entries for other tickers accumulate until that ticker is analyzed again — this is intentional.

### Configuration

`tradingagents/default_config.py` defines `DEFAULT_CONFIG`. Always `.copy()` before mutating. Key paths (override via `TRADINGAGENTS_*` env vars):

- `results_dir` → `~/.tradingagents/logs`
- `data_cache_dir` → `~/.tradingagents/cache` (also holds checkpoint DBs under `checkpoints/`)
- `memory_log_path` → `~/.tradingagents/memory/trading_memory.md`

### CLI vs package

- `cli/main.py` defines the Typer `analyze` command (the interactive TUI — `MessageBuffer`, Rich layout, `StatsCallbackHandler`). Provider/model selection is in `cli/utils.py` via `questionary`.
- `main.py` (repo root) is a non-interactive single-ticker driver. It currently hard-codes `llm_provider = "bedrock"` and reads `BEDROCK_DEEP_THINK_MODEL` / `BEDROCK_QUICK_THINK_MODEL` from `.env.enterprise` — edit freely for one-off runs; don't treat it as stable API.

Both entry points call `load_dotenv()` and then `load_dotenv(".env.enterprise", override=False)` so enterprise credentials (Azure, Bedrock) layer on top of per-provider API keys from `.env`.

## Adding a new LLM provider

1. Create `tradingagents/llm_clients/<provider>_client.py` subclassing `BaseLLMClient`. Import the SDK **inside** `get_llm()` (lazy) so the factory doesn't break when the optional SDK is missing.
2. Add a branch to `create_llm_client` in `factory.py`.
3. Extend `MODEL_OPTIONS` in `model_catalog.py` with `quick` and `deep` lists. If IDs are dynamic or user-supplied, add the provider to the skip set in `validators.py`.
4. If it needs a thinking-style knob, plumb it through `_get_provider_kwargs` in `trading_graph.py`.
5. Add the provider to the CLI selector in `cli/utils.py` (`select_llm_provider` + any special `_select_model` branch).
6. Extend the placeholder-keys fixture in `tests/conftest.py` if the SDK refuses to import without an env var set.
