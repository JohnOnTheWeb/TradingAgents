"""Published Bedrock on-demand pricing for the Claude models TradingAgents uses.

Rates are in **USD per 1,000 tokens** and reflect us-east-1 list prices.
Update when AWS publishes new pricing or new model IDs land in the catalog.
"""

from typing import Dict, Iterable, List

# Keep keys matching the Bedrock model ids actually returned on the
# ``response_metadata`` — including the inference-profile ``us.`` prefix,
# because that's what ChatBedrockConverse surfaces.
RATES_USD_PER_1K: Dict[str, Dict[str, float]] = {
    # Opus 4.7 — released 2026-02; inference-profile-only
    "us.anthropic.claude-opus-4-7": {"input": 0.015, "output": 0.075},
    # Opus 4.6 — fallback if 4.7 isn't accessible in this account
    "us.anthropic.claude-opus-4-6-v1": {"input": 0.015, "output": 0.075},
    # Opus 4.5
    "us.anthropic.claude-opus-4-5-20251101-v1:0": {"input": 0.015, "output": 0.075},
    # Sonnet 4.5 — the standard quick-think model
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "input": 0.003,
        "output": 0.015,
    },
    # Haiku 4.5 — very cheap quick option
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 0.001,
        "output": 0.005,
    },
}


def _match_rate(model_id: str) -> Dict[str, float]:
    """Return the USD/1K rate for a model id, tolerating minor suffix drift."""
    if model_id in RATES_USD_PER_1K:
        return RATES_USD_PER_1K[model_id]
    # Strip common suffixes (":0", "-v1") and retry a prefix match so minor
    # version changes don't zero-out the cost silently.
    for known, rate in RATES_USD_PER_1K.items():
        if model_id.startswith(known.rsplit("-v", 1)[0]):
            return rate
    return {"input": 0.0, "output": 0.0}


def compute_cost(usage: Dict[str, int], model_id: str) -> float:
    """Return USD cost for a single usage dict ``{input_tokens, output_tokens}``."""
    rate = _match_rate(model_id)
    return (
        (usage.get("input_tokens", 0) or 0) * rate["input"] / 1000.0
        + (usage.get("output_tokens", 0) or 0) * rate["output"] / 1000.0
    )


def summarize(
    buckets: Iterable[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Attach a ``cost_usd`` field to each per-model usage bucket.

    ``buckets`` is the list returned by ``PerModelTokenTracker.as_list()``
    (``[{model, input_tokens, output_tokens}, ...]``).
    """
    result: List[Dict[str, object]] = []
    for b in buckets:
        model = str(b.get("model", "unknown"))
        usage = {
            "input_tokens": int(b.get("input_tokens", 0) or 0),
            "output_tokens": int(b.get("output_tokens", 0) or 0),
        }
        result.append(
            {
                "model": model,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_usd": round(compute_cost(usage, model), 6),
            }
        )
    return result


def total_cost(buckets: Iterable[Dict[str, object]]) -> float:
    """Return the total USD cost across all buckets."""
    return round(sum(float(b["cost_usd"]) for b in summarize(buckets)), 6)
