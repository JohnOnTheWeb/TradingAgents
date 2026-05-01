"""Per-model token usage tracker for Bedrock Converse calls.

The CLI's ``StatsCallbackHandler`` only reports aggregate input/output tokens.
Cost calculation needs the split between the deep-think model (Claude Opus)
and the quick-think model (Claude Sonnet) because their per-token rates
differ by ~5x.  This callback reads the model id from the response metadata
and files tokens into a per-model bucket.
"""

import threading
from typing import Any, Dict, List

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


class PerModelTokenTracker(BaseCallbackHandler):
    """Accumulate input/output token counts keyed by model id."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._buckets: Dict[str, Dict[str, int]] = {}

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            return

        model_id = self._extract_model_id(response, generation)
        usage = self._extract_usage(generation)
        if not usage:
            return

        with self._lock:
            bucket = self._buckets.setdefault(
                model_id, {"input_tokens": 0, "output_tokens": 0}
            )
            bucket["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            bucket["output_tokens"] += int(usage.get("output_tokens", 0) or 0)

    # Support both chat-model and legacy LLM code paths.
    on_chat_model_end = on_llm_end

    @staticmethod
    def _extract_usage(generation: Any) -> Dict[str, int]:
        if hasattr(generation, "message"):
            msg = generation.message
            if isinstance(msg, AIMessage) and getattr(msg, "usage_metadata", None):
                return msg.usage_metadata or {}
        return {}

    @staticmethod
    def _extract_model_id(response: LLMResult, generation: Any) -> str:
        # langchain-aws puts the resolved model id on response_metadata
        if hasattr(generation, "message"):
            msg = generation.message
            meta = getattr(msg, "response_metadata", None) or {}
            for key in ("model_id", "model", "model_name"):
                if key in meta and meta[key]:
                    return str(meta[key])
        llm_output = getattr(response, "llm_output", None) or {}
        for key in ("model_id", "model", "model_name"):
            if key in llm_output and llm_output[key]:
                return str(llm_output[key])
        return "unknown"

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return a deep-ish copy of the current per-model buckets."""
        with self._lock:
            return {k: dict(v) for k, v in self._buckets.items()}

    def as_list(self) -> List[Dict[str, Any]]:
        """Return ``[{model, input_tokens, output_tokens}, ...]``."""
        with self._lock:
            return [
                {
                    "model": model,
                    "input_tokens": bucket["input_tokens"],
                    "output_tokens": bucket["output_tokens"],
                }
                for model, bucket in self._buckets.items()
            ]
