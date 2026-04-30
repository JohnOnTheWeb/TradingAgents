import os
from typing import Any, Optional

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

# dotenv-loaded .env files commonly declare keys with empty values
# (e.g. AWS_PROFILE=). boto3 reads AWS_PROFILE directly from os.environ and
# will attempt to look up a profile named "" — which fails before any of
# our own client kwargs are consulted. Scrub empty AWS_ entries on import.
for _k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
           "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION"):
    if _k in os.environ and not os.environ[_k].strip():
        del os.environ[_k]

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "max_tokens", "temperature",
    "callbacks", "client",
)

# Claude 4.x (Opus 4.5 / Sonnet 4.5 / Haiku 4.5 on Bedrock) accepts the
# legacy extended-thinking shape: {"type": "enabled", "budget_tokens": N}.
# Claude 4.7 on Bedrock uses the new adaptive-thinking API:
# {"type": "adaptive"} plus output_config.effort (low/medium/high).
_EFFORT_TO_BUDGET = {
    "low": 2048,
    "medium": 6144,
    "high": 16384,
}


def _build_normalized_class():
    """Import langchain_aws lazily and return a normalized subclass.

    Imported lazily so the package only becomes a hard dependency when a
    user actually selects the Bedrock provider.
    """
    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as err:
        raise ImportError(
            "Bedrock provider requires `langchain-aws`. Install with "
            "`pip install langchain-aws`."
        ) from err

    class NormalizedChatBedrockConverse(ChatBedrockConverse):
        def invoke(self, input, config=None, **kwargs):
            return normalize_content(super().invoke(input, config, **kwargs))

    return NormalizedChatBedrockConverse


class BedrockClient(BaseLLMClient):
    """Client for AWS Bedrock hosted models via the Converse API.

    Designed for Anthropic Claude on Bedrock (Opus 4.7, Sonnet 4.6,
    Haiku 4.5) but works for any Converse-compatible model ID.

    Credentials resolve through the standard boto3 chain. Honored env vars:
        AWS_REGION / AWS_DEFAULT_REGION  - region of the Bedrock runtime
        AWS_PROFILE                      - named profile for SSO / static keys
        BEDROCK_ENDPOINT_URL             - override for VPC / FIPS endpoints
        ANTHROPIC_MAX_TOKENS             - default output-token cap (16384)
    """

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        self.warn_if_unknown_model()

        normalized_cls = _build_normalized_class()

        llm_kwargs: dict[str, Any] = {"model": self.model}

        # .env files commonly declare keys with empty values; treat those as
        # unset so we don't pass region_name="" or profile="" to boto3.
        region = (
            os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
        ).strip()
        if region:
            llm_kwargs["region_name"] = region

        profile = (os.environ.get("AWS_PROFILE") or "").strip()
        if profile:
            llm_kwargs["credentials_profile_name"] = profile

        endpoint_url = (
            self.base_url or os.environ.get("BEDROCK_ENDPOINT_URL") or ""
        ).strip()
        if endpoint_url:
            llm_kwargs["endpoint_url"] = endpoint_url

        llm_kwargs.setdefault(
            "max_tokens",
            int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16384")),
        )

        # Build a boto3 client with extended read timeout + retry budget.
        # The default 60s read timeout is too aggressive for Claude invocations
        # that process long tool-call outputs (e.g. 124 rows of price data).
        from botocore.config import Config
        import boto3

        boto_config = Config(
            read_timeout=int(os.environ.get("BEDROCK_READ_TIMEOUT", "600")),
            connect_timeout=int(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10")),
            retries={
                "max_attempts": int(os.environ.get("BEDROCK_MAX_RETRIES", "4")),
                "mode": "adaptive",
            },
        )
        session_kwargs: dict[str, Any] = {}
        if profile:
            session_kwargs["profile_name"] = profile
        if region:
            session_kwargs["region_name"] = region
        session = boto3.Session(**session_kwargs)
        client_kwargs: dict[str, Any] = {"config": boto_config}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        llm_kwargs["client"] = session.client("bedrock-runtime", **client_kwargs)

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        effort = self.kwargs.get("effort")
        if effort and "claude" in self.model.lower():
            effort_lower = str(effort).lower()
            model_lower = self.model.lower()
            if "claude-opus-4-7" in model_lower or "claude-sonnet-4-7" in model_lower:
                llm_kwargs["additional_model_request_fields"] = {
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": effort_lower},
                }
            else:
                budget = _EFFORT_TO_BUDGET.get(effort_lower)
                if budget is not None:
                    llm_kwargs["additional_model_request_fields"] = {
                        "thinking": {"type": "enabled", "budget_tokens": budget},
                    }

        return normalized_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        return validate_model("bedrock", self.model)
