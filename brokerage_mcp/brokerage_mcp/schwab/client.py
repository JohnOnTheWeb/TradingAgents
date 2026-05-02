"""Charles Schwab Individual Trader API client — OAuth refresh + access cache.

Docs: https://developer.schwab.com/
Access token TTL ~30 min; refresh token TTL 7 days (interactive re-auth required
after expiry — see the /brokerage-refresh skill).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from brokerage_mcp.auth import BrokerCreds, load_schwab_creds

_logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("SCHWAB_BASE_URL", "https://api.schwabapi.com")
OAUTH_URL = f"{BASE_URL}/v1/oauth/token"

_REFRESH_BUFFER_SEC = 60.0


class SchwabError(RuntimeError):
    """Raised for Schwab call failures — caller converts to fail-open sentinel."""


class SchwabClient:
    def __init__(self, creds: Optional[BrokerCreds] = None) -> None:
        self._creds = creds or load_schwab_creds()
        if self._creds is None:
            raise SchwabError(
                "Schwab credentials not available; set brokerage/schwab-oauth in "
                "Secrets Manager or populate .brokerage-tokens.json."
            )
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=20.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _basic_auth_header(self) -> str:
        raw = f"{self._creds['client_id']}:{self._creds['client_secret']}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    async def _ensure_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._expires_at - _REFRESH_BUFFER_SEC:
            return self._access_token
        async with self._lock:
            now = time.monotonic()
            if self._access_token and now < self._expires_at - _REFRESH_BUFFER_SEC:
                return self._access_token
            resp = await self._http.post(
                OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._creds["refresh_token"],
                },
                headers={
                    "Authorization": self._basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code >= 400:
                raise SchwabError(
                    f"Schwab OAuth refresh failed ({resp.status_code}): {resp.text[:200]}"
                )
            payload = resp.json()
            self._access_token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 1800))
            self._expires_at = time.monotonic() + expires_in
            _logger.info("Schwab access token refreshed; expires in %.0fs", expires_in)
            return self._access_token

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        token = await self._ensure_access_token()
        resp = await self._http.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            raise SchwabError(
                f"Schwab GET {path} failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()
