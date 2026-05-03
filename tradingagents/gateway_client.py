"""SigV4-signed MCP client for the AgentCore Gateway.

Every agent-side tool call (brokerage, data-tools, memory-log) must flow
through this client so the AgentCore Gateway remains the single entrypoint
for MCP traffic. The client speaks MCP JSON-RPC 2.0 over HTTPS with SigV4
using boto3-resolved credentials (task role / runtime role).

Tool names at the Gateway are namespaced as ``<target>___<tool>`` (three
underscores), e.g. ``brokerage___get_vol_regime``. Callers pass the full
namespaced name to :func:`call`.

The returned value is unwrapped to match the pre-refactor ``@tool`` contract:
MCP's ``result.content[0].json`` is itself ``{"tool_name": ..., "result": ...}``
from the target Lambda, so we unwrap one more layer and return the inner
``result`` (string for data-tools, dict envelope for brokerage-tools, etc.).

Environment:
    GATEWAY_URL             — required; base URL (``.../mcp``)
    AWS_REGION              — optional, default us-east-1
    GATEWAY_TIMEOUT         — per-request timeout seconds, default 20
    GATEWAY_MAX_RETRIES     — transport retry count, default 3
    GATEWAY_CIRCUIT_COOLDOWN — breaker cool-down seconds, default 120
    GATEWAY_CIRCUIT_FAIL_THRESHOLD — breaker trip count, default 3
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from typing import Any, Dict, Optional

import boto3
import botocore.auth
import botocore.awsrequest
import requests

_logger = logging.getLogger(__name__)

_GATEWAY_SERVICE = "bedrock-agentcore"


class GatewayError(RuntimeError):
    """Transport or protocol error invoking the Gateway."""


class GatewayToolError(GatewayError):
    """Gateway returned a well-formed JSON-RPC ``error`` object."""

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class GatewayUnavailable(GatewayError):
    """``GATEWAY_URL`` is unset — no Gateway configured in this environment."""


# --- module-level state -----------------------------------------------------

_DEFAULT_TIMEOUT = float(os.environ.get("GATEWAY_TIMEOUT", "20"))
_MAX_RETRIES = int(os.environ.get("GATEWAY_MAX_RETRIES", "3"))
_CIRCUIT_COOLDOWN_SEC = float(os.environ.get("GATEWAY_CIRCUIT_COOLDOWN", "120"))
_CIRCUIT_FAIL_THRESHOLD = int(os.environ.get("GATEWAY_CIRCUIT_FAIL_THRESHOLD", "3"))

_session_lock = threading.Lock()
_session: Optional[requests.Session] = None

_creds_lock = threading.Lock()
_creds = None  # botocore credentials; refreshable — pass to SigV4Auth as-is

_circuit_lock = threading.Lock()
_circuit_fails = 0
_circuit_open_until = 0.0


def _url() -> str:
    raw = (os.environ.get("GATEWAY_URL") or "").strip()
    if not raw:
        raise GatewayUnavailable("GATEWAY_URL is not set")
    return raw.rstrip("/")


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
        return _session


def _get_credentials():
    """Resolve botocore credentials once; they auto-refresh when signed."""
    global _creds
    with _creds_lock:
        if _creds is None:
            sess = boto3.Session()
            _creds = sess.get_credentials()
            if _creds is None:
                raise GatewayError("no AWS credentials available for SigV4")
        return _creds


# --- circuit breaker --------------------------------------------------------


def _circuit_check() -> Optional[str]:
    now = time.monotonic()
    with _circuit_lock:
        if now < _circuit_open_until:
            return f"gateway circuit open (cooling down {_circuit_open_until - now:.0f}s)"
    return None


def _circuit_record_failure() -> None:
    global _circuit_fails, _circuit_open_until
    with _circuit_lock:
        _circuit_fails += 1
        if _circuit_fails >= _CIRCUIT_FAIL_THRESHOLD:
            _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SEC
            _circuit_fails = 0
            _logger.warning(
                "AgentCore Gateway circuit breaker opened for %.0fs after repeated failures",
                _CIRCUIT_COOLDOWN_SEC,
            )


def _circuit_record_success() -> None:
    global _circuit_fails
    with _circuit_lock:
        _circuit_fails = 0


# --- core RPC ---------------------------------------------------------------


def _sign_and_send(body: bytes, timeout: float) -> requests.Response:
    url = _url()
    creds = _get_credentials()
    region = _region()

    req = botocore.awsrequest.AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    botocore.auth.SigV4Auth(creds, _GATEWAY_SERVICE, region).add_auth(req)

    session = _get_session()
    return session.post(
        url,
        data=body,
        headers=dict(req.headers.items()),
        timeout=timeout,
    )


def _do_rpc(method: str, params: Optional[Dict[str, Any]], timeout: float) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload).encode("utf-8")

    last_error = "unknown"
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = _sign_and_send(body, timeout)
        except requests.exceptions.Timeout as err:
            last_error = f"timeout: {err}"
            _logger.warning("gateway %s timeout (attempt %d/%d)", method, attempt, _MAX_RETRIES)
        except requests.exceptions.RequestException as err:
            last_error = f"transport: {err}"
            _logger.warning("gateway %s transport error: %s (attempt %d/%d)", method, err, attempt, _MAX_RETRIES)
        else:
            status = resp.status_code
            if 200 <= status < 300:
                try:
                    return resp.json()
                except ValueError as err:
                    _circuit_record_failure()
                    raise GatewayError(f"non-JSON response from gateway: {err}") from err
            # 4xx non-429: fail fast
            if 400 <= status < 500 and status != 429:
                text = (resp.text or "")[:400]
                _circuit_record_failure()
                raise GatewayError(f"gateway HTTP {status}: {text!r}")
            # 5xx or 429 → retryable
            text = (resp.text or "")[:200]
            last_error = f"HTTP {status}: {text!r}"
            _logger.warning("gateway %s %s (attempt %d/%d)", method, last_error, attempt, _MAX_RETRIES)

        if attempt < _MAX_RETRIES:
            delay = 0.3 * (2 ** (attempt - 1))
            time.sleep(delay + random.uniform(0, delay * 0.25))

    _circuit_record_failure()
    raise GatewayError(f"gateway {method} failed after {_MAX_RETRIES} attempts: {last_error}")


# --- public API -------------------------------------------------------------


_TOOLS_CACHE: Dict[str, Any] = {}
_TOOLS_FETCHED_AT = 0.0
_TOOLS_TTL = float(os.environ.get("GATEWAY_TOOLS_TTL", "300"))


def list_tools(force_refresh: bool = False) -> Any:
    """Return the gateway ``tools/list`` result. Cached for 5 minutes by default."""
    global _TOOLS_CACHE, _TOOLS_FETCHED_AT
    now = time.monotonic()
    if (
        not force_refresh
        and _TOOLS_CACHE
        and (now - _TOOLS_FETCHED_AT) < _TOOLS_TTL
    ):
        return _TOOLS_CACHE

    circuit = _circuit_check()
    if circuit:
        raise GatewayError(circuit)

    parsed = _do_rpc("tools/list", None, _DEFAULT_TIMEOUT)
    if "error" in parsed:
        err = parsed["error"] or {}
        raise GatewayToolError(err.get("code"), err.get("message", ""), err.get("data"))
    result = parsed.get("result") or {}
    _TOOLS_CACHE = result
    _TOOLS_FETCHED_AT = now
    _circuit_record_success()
    return result


def call(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Any:
    """Invoke a Gateway tool. Returns the inner Lambda ``result`` payload."""
    circuit = _circuit_check()
    if circuit:
        raise GatewayError(circuit)

    parsed = _do_rpc(
        "tools/call",
        {"name": tool_name, "arguments": arguments},
        timeout if timeout is not None else _DEFAULT_TIMEOUT,
    )

    if "error" in parsed:
        err = parsed["error"] or {}
        _circuit_record_failure()
        raise GatewayToolError(err.get("code"), err.get("message", ""), err.get("data"))

    _circuit_record_success()
    result = parsed.get("result") or {}
    content = result.get("content") or []
    if content and isinstance(content, list) and isinstance(content[0], dict):
        first = content[0]
        ctype = first.get("type")
        if ctype == "json":
            inner = first.get("json")
        elif ctype == "text":
            # Target Lambdas may JSON-encode their result into a text block.
            text = first.get("text") or ""
            try:
                inner = json.loads(text)
            except (TypeError, ValueError):
                inner = text
        else:
            inner = first
        if (
            isinstance(inner, dict)
            and "tool_name" in inner
            and "result" in inner
        ):
            return inner["result"]
        return inner
    return result


def call_or(
    tool_name: str,
    arguments: Dict[str, Any],
    fallback: Any,
    *,
    log_errors: bool = True,
) -> Any:
    """Call ``tool_name`` on the Gateway; on any failure return ``fallback``.

    The fallback may be a callable ``fn(exc) -> Any`` which lets callers
    compute an error-aware default (e.g., brokerage's "unavailable" envelope).
    """
    try:
        return call(tool_name, arguments)
    except GatewayError as err:
        if log_errors:
            _logger.warning("gateway %s failed: %s", tool_name, err)
        if callable(fallback):
            return fallback(err)
        return fallback
