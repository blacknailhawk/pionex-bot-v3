"""Pionex REST client (signed).

Signing scheme per Pionex docs (verify against https://pionex-doc.gitbook.io):
  PATH_URL = path + "?" + query params sorted by key (timestamp in ms required)
  payload  = METHOD + PATH_URL            (+ JSON body string for POST/DELETE)
  signature = hex(HMAC_SHA256(api_secret, payload))
Headers: PIONEX-KEY, PIONEX-SIGNATURE
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

from .config import cfg

BASE = "https://api.pionex.com"


class PionexError(Exception):
    pass


class PionexClient:
    def __init__(self, key: str = None, secret: str = None):
        self.key = key or cfg.PIONEX_KEY
        self.secret = (secret or cfg.PIONEX_SECRET).encode()
        self._http = httpx.AsyncClient(base_url=BASE, timeout=15)

    def _sign(self, method: str, path: str, params: dict, body: str = "") -> tuple[str, str]:
        params = dict(params or {})
        params["timestamp"] = str(int(time.time() * 1000))
        query = urlencode(sorted(params.items()))
        path_url = f"{path}?{query}"
        payload = f"{method.upper()}{path_url}{body}"
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return path_url, sig

    async def _request(self, method: str, path: str, params: dict = None, body: dict = None):
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        path_url, sig = self._sign(method, path, params, body_str)
        headers = {"PIONEX-KEY": self.key, "PIONEX-SIGNATURE": sig}
        if body_str:
            headers["Content-Type"] = "application/json"
        r = await self._http.request(method, path_url, headers=headers, content=body_str or None)
        data = r.json()
        if not data.get("result", False):
            raise PionexError(f"{path}: {data}")
        return data.get("data", data)

    # ---- Account ----
    async def balances(self) -> dict:
        """Returns {coin: free_amount} for non-zero balances."""
        data = await self._request("GET", "/api/v1/account/balances")
        out = {}
        for b in data.get("balances", []):
            free = float(b.get("free", 0))
            if free > 0:
                out[b["coin"]] = free
        return out

    # ---- Trading (spot market orders) ----
    async def market_buy(self, symbol: str, quote_amount: float) -> dict:
        """Market buy spending `quote_amount` of the quote currency (e.g. USDT)."""
        return await self._request("POST", "/api/v1/trade/order", body={
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "amount": f"{quote_amount:.4f}",
        })

    async def market_sell(self, symbol: str, base_size: float) -> dict:
        """Market sell `base_size` of the base currency (e.g. BTC)."""
        return await self._request("POST", "/api/v1/trade/order", body={
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "size": f"{base_size:.8f}",
        })

    async def close(self):
        await self._http.aclose()
