"""External signal sources for the NEXUSBOT V3 weighted scoring engine.

Sources:
  - Fear & Greed Index  (alternative.me — free, no key)
  - BTC Dominance       (CoinGecko global — free, rate-limited)
  - Volume Delta        (computed from Pionex candles — free)
  - News Sentiment      (Exa.ai — requires EXA_API_KEY; stubs to 0.5 if absent)
  - Reddit Buzz         (stub 0.5 — placeholder for future Reddit API integration)

All scores are normalised to [0, 1]:
  1.0 = maximally bullish signal
  0.5 = neutral
  0.0 = maximally bearish signal

Results are cached to avoid hammering external APIs on every tick.
"""

import logging
import time

import httpx

log = logging.getLogger("signals")

_cache: dict = {}
_FG_TTL = 3600      # Fear & Greed changes once per hour
_DOM_TTL = 300      # BTC dominance: 5-min cache
_NEWS_TTL = 600     # News: 10-min cache


def _cached(key: str, ttl: float):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < ttl:
        return entry["val"]
    return None


def _store(key: str, val: float) -> float:
    _cache[key] = {"ts": time.time(), "val": val}
    return val


# ---------------------------------------------------------------------------
# Fear & Greed Index
# ---------------------------------------------------------------------------

async def fear_greed() -> float:
    """Return Fear & Greed Index normalised to [0, 1].

    High value (greed) = bullish sentiment.
    Falls back to last cached value or 0.5 on error.
    """
    cached = _cached("fg", _FG_TTL)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get("https://api.alternative.me/fng/?limit=1")
            r.raise_for_status()
            raw = int(r.json()["data"][0]["value"])
            return _store("fg", raw / 100)
    except Exception as e:
        log.warning("fear_greed fetch error: %s", e)
        return _cached("fg", 86400) or 0.5  # use stale cache up to 24h


# ---------------------------------------------------------------------------
# BTC Dominance
# ---------------------------------------------------------------------------

async def btc_dominance() -> float:
    """Return BTC dominance as a [0, 1] fraction.

    Note: the caller decides how to interpret this per-symbol
    (bullish for BTC itself, bearish for alts when high).
    """
    cached = _cached("dom", _DOM_TTL)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get("https://api.coingecko.com/api/v3/global")
            r.raise_for_status()
            pct = r.json()["data"]["market_cap_percentage"].get("btc", 50)
            return _store("dom", pct / 100)
    except Exception as e:
        log.warning("btc_dominance fetch error: %s", e)
        return _cached("dom", 86400) or 0.5


# ---------------------------------------------------------------------------
# On-chain Volume Delta (approximated from candle bodies)
# ---------------------------------------------------------------------------

def volume_delta(candles: list[dict], lookback: int = 20) -> float:
    """Estimate buy pressure from the last `lookback` candles.

    Uses candle-body direction as a proxy for buy/sell volume:
    bullish candle (close >= open) → buy volume; bearish → sell volume.

    Returns [0, 1]:  1 = all buys, 0 = all sells, 0.5 = balanced.
    """
    if not candles:
        return 0.5
    recent = candles[-lookback:]
    buy_vol = sum(c["volume"] for c in recent if c["close"] >= c["open"])
    total_vol = sum(c["volume"] for c in recent) or 1
    return buy_vol / total_vol


# ---------------------------------------------------------------------------
# News Sentiment (Exa.ai)
# ---------------------------------------------------------------------------

async def news_sentiment(symbol: str, exa_key: str = "") -> float:
    """Return news sentiment for `symbol` in [0, 1].

    Requires EXA_API_KEY.  Returns 0.5 (neutral) when key is absent or on error.
    Sentiment is a simple positive/negative keyword count over recent headlines.
    """
    if not exa_key:
        return 0.5

    cache_key = f"news_{symbol}"
    cached = _cached(cache_key, _NEWS_TTL)
    if cached is not None:
        return cached

    # Derive a human-readable query term from the Pionex symbol (e.g. BTC_USDT → BTC)
    base = symbol.split("_")[0].replace("X", "").replace("x", "")
    query = f"{base} cryptocurrency price news"

    positive_words = {"bullish", "rally", "surge", "gains", "buy", "breakout",
                      "uptrend", "growth", "record", "high", "outperform"}
    negative_words = {"bearish", "crash", "drop", "sell", "downtrend", "loss",
                      "warning", "dump", "decline", "low", "underperform"}

    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": exa_key, "Content-Type": "application/json"},
                json={"query": query, "numResults": 10,
                      "type": "neural", "useAutoprompt": True},
            )
            r.raise_for_status()
            results = r.json().get("results", [])

        pos = neg = 0
        for item in results:
            text = (item.get("title", "") + " " + item.get("snippet", "")).lower()
            pos += sum(1 for w in positive_words if w in text)
            neg += sum(1 for w in negative_words if w in text)

        total = pos + neg or 1
        score = pos / total
        return _store(cache_key, score)
    except Exception as e:
        log.warning("news_sentiment error for %s: %s", symbol, e)
        return _cached(cache_key, 86400) or 0.5


# ---------------------------------------------------------------------------
# Reddit / Social Buzz
# ---------------------------------------------------------------------------

def reddit_buzz() -> float:
    """Social buzz score [0, 1].  Stub — returns neutral 0.5.

    Future: integrate Reddit API or LunarCrush to get real social volume.
    """
    return 0.5


# ---------------------------------------------------------------------------
# Convenience: fetch all market-wide signals in one call
# ---------------------------------------------------------------------------

async def market_signals() -> dict:
    """Return a dict with all market-wide signals (not per-symbol)."""
    fg, dom = 0.5, 0.5
    try:
        import asyncio
        fg, dom = await asyncio.gather(fear_greed(), btc_dominance())
    except Exception as e:
        log.warning("market_signals error: %s", e)
    return {
        "fear_greed": round(fg, 3),
        "btc_dominance": round(dom, 3),
        "reddit_buzz": reddit_buzz(),
    }
