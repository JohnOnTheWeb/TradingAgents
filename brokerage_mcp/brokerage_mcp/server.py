"""FastAPI MCP JSON-RPC server.

Exposes a single POST /mcp endpoint implementing the MCP methods ``tools/list``
and ``tools/call``. Also exposes a GET /health for ALB target-group checks.

This server is stateless other than the in-process OAuth/token caches.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from brokerage_mcp.tools import DISPATCH, TOOL_SCHEMAS, Broker, call_tool

_logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

_broker: Broker
_SHARED_SECRET: str = ""


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _broker, _SHARED_SECRET
    _broker = Broker()
    _SHARED_SECRET = (os.environ.get("BROKERAGE_SHARED_SECRET") or "").strip()
    if not _SHARED_SECRET:
        _logger.warning(
            "BROKERAGE_SHARED_SECRET is not set — /mcp accepts all requests. "
            "Set the env var via ECS secrets for production.",
        )
    try:
        yield
    finally:
        await _broker.aclose()


app = FastAPI(title="brokerage-mcp", lifespan=_lifespan)


def _authorized(request: Request) -> bool:
    if not _SHARED_SECRET:
        return True  # unsecured mode for local dev
    return request.headers.get("x-brokerage-secret", "").strip() == _SHARED_SECRET


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _jsonrpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


@app.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse(
            _jsonrpc_error(None, -32001, "Unauthorized"),
            status_code=401,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"), status_code=400)

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "tools/list":
        tools = [
            {
                "name": name,
                "description": schema["description"],
                "inputSchema": schema["input_schema"],
            }
            for name, schema in TOOL_SCHEMAS.items()
        ]
        return JSONResponse(_jsonrpc_result(req_id, {"tools": tools}))

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in DISPATCH:
            return JSONResponse(
                _jsonrpc_error(req_id, -32601, f"Unknown tool: {name}"),
                status_code=404,
            )
        try:
            result = await call_tool(_broker, name, arguments)
        except TypeError as err:
            return JSONResponse(
                _jsonrpc_error(req_id, -32602, f"Invalid params: {err}"),
                status_code=400,
            )
        return JSONResponse(
            _jsonrpc_result(
                req_id,
                {"content": [{"type": "json", "json": result}]},
            )
        )

    return JSONResponse(
        _jsonrpc_error(req_id, -32601, f"Method not found: {method}"),
        status_code=404,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "brokerage_mcp.server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
