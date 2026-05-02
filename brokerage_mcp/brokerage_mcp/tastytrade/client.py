"""Tastytrade API client — OAuth refresh + access-token cache.

Docs: https://developer.tastytrade.com/
Production base: https://api.tastyworks.com
Access token TTL is ~15 min; refresh tokens rotate infrequently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from brokerage_mcp.auth import BrokerCreds, load_tastytrade_creds

_logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("TASTYTRADE_BASE_URL", "https://api.tastyworks.com")
OAUTH_URL = f"{BASE_URL}/oauth/token"

# Leave a 60s safety window so we rotate before the token actually expires.
_REFRESH_BUFFER_SEC = 60.0


class TastytradeError(RuntimeError):
    """Raised for non-retryable Tastytrade call failures."""


class TastytradeClient:
    def __init__(self, creds: Optional[BrokerCreds] = None) -> None:
        self._creds = creds or load_tastytrade_creds()
        if self._creds is None:
            raise TastytradeError(
                "Tastytrade credentials not available; set brokerage/tastytrade-oauth "
                "in Secrets Manager or populate .brokerage-tokens.json."
            )
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=20.0)
        # Short-circuit auth after a failure so concurrent callers don't all
        # re-refresh with the same bad token.
        self._auth_fail_until: float = 0.0
        self._auth_fail_reason: str = ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _ensure_access_token(self) -> str:
        now = time.monotonic()
        if now < self._auth_fail_until:
            raise TastytradeError(
                f"Tastytrade auth circuit open (last error: {self._auth_fail_reason})"
            )
        if self._access_token and now < self._expires_at - _REFRESH_BUFFER_SEC:
            return self._access_token
        async with self._lock:
            # Re-check inside the lock — another coroutine may have refreshed.
            now = time.monotonic()
            if now < self._auth_fail_until:
                raise TastytradeError(
                    f"Tastytrade auth circuit open (last error: {self._auth_fail_reason})"
                )
            if self._access_token and now < self._expires_at - _REFRESH_BUFFER_SEC:
                return self._access_token
            resp = await self._http.post(
                OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._creds["refresh_token"],
                    "client_id": self._creds["client_id"],
                    "client_secret": self._creds["client_secret"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code >= 400:
                reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self._auth_fail_reason = reason
                self._auth_fail_until = time.monotonic() + 300.0
                raise TastytradeError(f"Tastytrade OAuth refresh failed ({reason})")
            payload = resp.json()
            self._access_token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 900))
            self._expires_at = time.monotonic() + expires_in
            _logger.info("Tastytrade access token refreshed; expires in %.0fs", expires_in)
            return self._access_token

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        token = await self._ensure_access_token()
        resp = await self._http.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            raise TastytradeError(
                f"Tastytrade GET {path} failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()
