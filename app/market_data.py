"""Market data — Pionex primary, Binance fallback for crypto pairs.

Pionex carries products Binance does not: tokenised stocks (AAPLX, TSLAX…),
index ETFs (SPYX, QQQX), commodities (XAUT, PAXG, SLVX, USOX).
For those symbols Pionex is the only source.

For pure-crypto pairs (BTC_USDT, ETH_USDT, SOL_USDT …) we fall back to
Binance public API automatically if Pionex fails or times out. Binance has
no rate-limit issues for market data (1 200 req/min, no key required) and
returns identical OHLCV structure.
"""
import logging

import httpx

log = logging.getLogger("market_data")

# ── HTTP clients ──────────────────────────────────────────────────────────────
_pionex  = httpx.AsyncClient(base_url="https://api.pionex.com",   timeout=10)
_binance = httpx.AsyncClient(base_url="https://api.binance.com",  timeout=10)

# ── Interval maps ─────────────────────────────────────────────────────────────
_PIX = {                                     # Pionex uses uppercase M/H/D
    "1m":"1M","5m":"5M","15m":"15M","30m":"30M",
    "1h":"60M","60m":"60M","4h":"4H","8h":"8H","12h":"12H","1d":"1D",
}
_BIX = {                                     # Binance uses lowercase m/h/d
    "1m":"1m","5m":"5m","15m":"15m","30m":"30m",
    "1h":"1h","60m":"1h","4h":"4h","8h":"8h","12h":"12h","1d":"1d",
}

# ── Symbol helpers ────────────────────────────────────────────────────────────
# Pionex-exclusive prefixes — Binance has no equivalent instrument
_PIONEX_ONLY_PREFIXES = (
    "AAPLX","AMZNX","GOOGLX","METAX","NVDAX","TSLAX",  # xStocks
    "CRCLX","BMNRX",                                     # micro-caps
    "SPYX","QQQX",                                       # index ETFs
    "SLVX","USOX",                                       # commodities
)

def _is_pionex_only(symbol: str) -> bool:
    base = symbol.split("_")[0].upper()
    return any(base.startswith(p) for p in _PIONEX_ONLY_PREFIXES)

def _to_binance_symbol(symbol: str) -> str:
    """BTC_USDT  →  BTCUSDT"""
    return symbol.replace("_", "")


# ── Pionex fetchers ───────────────────────────────────────────────────────────
async def _pionex_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    r = await _pionex.get("/api/v1/market/klines", params={
        "symbol": symbol,
        "interval": _PIX.get(interval, interval),
        "limit": limit,
    })
    r.raise_for_status()
    data = r.json()
    if not data.get("result", False):
        raise RuntimeError(f"pionex klines {symbol}: {data}")
    out = []
    for k in reversed(data["data"]["klines"]):   # Pionex returns newest-first
        out.append({
            "time":   k["time"] // 1000,
            "open":   float(k["open"]),
            "high":   float(k["high"]),
            "low":    float(k["low"]),
            "close":  float(k["close"]),
            "volume": float(k["volume"]),
        })
    return out


async def _pionex_last_price(symbol: str) -> float:
    r = await _pionex.get("/api/v1/market/bookTickers", params={"symbol": symbol})
    r.raise_for_status()
    data = r.json()
    if not data.get("result", False) or not data["data"].get("tickers"):
        raise RuntimeError(f"pionex bookTicker {symbol}: {data}")
    t = data["data"]["tickers"][0]
    return (float(t["bidPrice"]) + float(t["askPrice"])) / 2


# ── Binance fetchers ──────────────────────────────────────────────────────────
async def _binance_klines(symbol: str, interval: str, limit: int) -> list[dict]:
    r = await _binance.get("/api/v3/klines", params={
        "symbol":   _to_binance_symbol(symbol),
        "interval": _BIX.get(interval, interval),
        "limit":    min(limit, 1000),            # Binance max per request
    })
    r.raise_for_status()
    out = []
    for k in r.json():
        # Binance: [openTime, open, high, low, close, volume, closeTime, ...]
        out.append({
            "time":   int(k[0]) // 1000,
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        })
    return out


async def _binance_last_price(symbol: str) -> float:
    r = await _binance.get("/api/v3/ticker/price",
                           params={"symbol": _to_binance_symbol(symbol)})
    r.raise_for_status()
    return float(r.json()["price"])


# ── Public API ────────────────────────────────────────────────────────────────
async def klines(symbol: str, interval: str = "15m", limit: int = 300) -> list[dict]:
    """Return OHLCV candles for *symbol*.

    Tries Pionex first. If Pionex errors or times out and the symbol is
    available on Binance, retries transparently on Binance.
    """
    try:
        return await _pionex_klines(symbol, interval, limit)
    except Exception as pionex_err:
        if _is_pionex_only(symbol):
            raise                          # no fallback for xStocks / synthetics
        log.warning("Pionex klines failed for %s (%s) — trying Binance", symbol, pionex_err)
        try:
            data = await _binance_klines(symbol, interval, limit)
            log.info("Binance fallback OK for %s (%d candles)", symbol, len(data))
            return data
        except Exception as binance_err:
            log.error("Binance fallback also failed for %s: %s", symbol, binance_err)
            raise RuntimeError(
                f"{symbol}: Pionex ({pionex_err}) and Binance ({binance_err}) both failed"
            ) from binance_err


async def last_price(symbol: str) -> float:
    """Return the current mid-price for *symbol*, with Binance fallback."""
    try:
        return await _pionex_last_price(symbol)
    except Exception as pionex_err:
        if _is_pionex_only(symbol):
            raise
        log.warning("Pionex price failed for %s (%s) — trying Binance", symbol, pionex_err)
        try:
            price = await _binance_last_price(symbol)
            log.info("Binance price fallback OK for %s: %.6f", symbol, price)
            return price
        except Exception as binance_err:
            log.error("Binance price fallback also failed for %s: %s", symbol, binance_err)
            raise RuntimeError(
                f"{symbol}: Pionex ({pionex_err}) and Binance ({binance_err}) both failed"
            ) from binance_err
