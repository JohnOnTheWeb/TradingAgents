"""AgentCore Gateway MCP-target Lambda for the brokerage-mcp sidecar.

Gateway invokes with ``event = {"tool_name": "...", "tool_arguments": {...}}``
(or ``event["__name"]``). We forward to the brokerage-mcp service behind the
internal ALB via JSON-RPC tools/call and unwrap the MCP envelope.

Env:
    BROKERAGE_MCP_URL    Internal ALB URL, e.g. http://internal-alb.xyz/mcp
    BROKERAGE_MCP_TIMEOUT   HTTP timeout (default 20s)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_URL = os.environ.get("BROKERAGE_MCP_URL", "")
_TIMEOUT = float(os.environ.get("BROKERAGE_MCP_TIMEOUT", "20"))


_cached_secret = ""


def _shared_secret() -> str:
    global _cached_secret
    if _cached_secret:
        return _cached_secret
    inline = (os.environ.get("BROKERAGE_SHARED_SECRET") or "").strip()
    if inline:
        _cached_secret = inline
        return inline
    secret_id = (os.environ.get("BROKERAGE_SHARED_SECRET_ID") or "").strip()
    if not secret_id:
        return ""
    try:
        import boto3
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id).get("SecretString") or ""
        try:
            parsed = json.loads(raw)
            resolved = str(parsed.get("secret") or raw).strip()
        except json.JSONDecodeError:
            resolved = raw.strip()
    except Exception as err:  # noqa: BLE001
        logger.warning("brokerage shared secret lookup failed: %s", err)
        resolved = ""
    _cached_secret = resolved
    return resolved


def _post_mcp(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if not _URL:
        raise RuntimeError("BROKERAGE_MCP_URL is not set")
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = _shared_secret()
    if secret:
        headers["X-Brokerage-Secret"] = secret
    req = urllib.request.Request(
        _URL,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"brokerage-mcp HTTP {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"brokerage-mcp unreachable: {err.reason}") from err

    parsed = json.loads(raw)
    if "error" in parsed:
        raise RuntimeError(f"brokerage-mcp rpc error: {parsed['error']}")
    result = parsed.get("result") or {}
    content = result.get("content") or []
    if content and content[0].get("type") == "json":
        return content[0].get("json", {})
    return result


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    tool_name = (
        event.get("tool_name")
        or event.get("__name")
        or (context.client_context.custom.get("bedrockAgentCoreToolName")
            if getattr(context, "client_context", None) and context.client_context
            and getattr(context.client_context, "custom", None) else None)
        or ""
    )
    args = event.get("tool_arguments") or event.get("arguments")
    if args is None:
        args = {k: v for k, v in event.items() if k not in ("tool_name", "__name")}
    if not tool_name:
        logger.warning("tool_name missing. event keys=%s", list(event.keys()))
        raise ValueError("tool_name is required")
    if "___" in tool_name:
        tool_name = tool_name.rsplit("___", 1)[-1]
    try:
        result = _post_mcp(tool_name, args)
    except Exception as err:  # noqa: BLE001
        logger.error("brokerage tool %s failed: %s", tool_name, err, exc_info=True)
        raise
    return {"tool_name": tool_name, "result": result}
