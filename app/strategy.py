"""Signal engine: EMA trend + RSI filter + ATR volatility, long and short.

NEXUSBOT V3 adds a weighted scoring engine on top of the original indicators.
Each signal source is normalised to [0, 1] (1 = maximally bullish, 0 = bearish).
A weighted average produces a composite score:
  score > BUY_THRESHOLD  → LONG entry considered
  score < SELL_THRESHOLD → SHORT entry considered

Technical signals (EMA cross + RSI) remain the entry gate for exits.
"""


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    gains, losses = [0.0], [0.0]
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[1:period + 1]) / period
    avg_l = sum(losses[1:period + 1]) / period
    out = [50.0] * (period + 1)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else 100
        out.append(100 - 100 / (1 + rs))
    return out


def atr(candles: list[dict], period: int = 14) -> list[float]:
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - p["close"]),
                       abs(c["low"] - p["close"])))
    out = [sum(trs[:period]) / period] * period
    for i in range(period, len(trs)):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out


def analyze(candles: list[dict], vol_halt_atr_pct: float) -> dict:
    """Returns signal snapshot computed on the latest CLOSED candle."""
    closes = [c["close"] for c in candles]
    e_fast, e_slow = ema(closes, 12), ema(closes, 26)
    r = rsi(closes, 14)
    a = atr(candles, 14)
    i = len(candles) - 2  # last closed candle (last item may be forming)
    price = closes[i]
    atr_pct = a[i] / price if price else 0

    cross_up = e_fast[i] > e_slow[i] and e_fast[i - 1] <= e_slow[i - 1]
    cross_dn = e_fast[i] < e_slow[i] and e_fast[i - 1] >= e_slow[i - 1]
    vol_ok = atr_pct <= vol_halt_atr_pct

    entry_long = cross_up and 50 <= r[i] <= 72 and price > e_slow[i] and vol_ok
    entry_short = cross_dn and 28 <= r[i] <= 50 and price < e_slow[i] and vol_ok

    sparkline = closes[max(0, i - 29): i + 1]

    return {
        "price": price, "atr": a[i], "atr_pct": atr_pct, "rsi": r[i],
        "ema_fast": e_fast[i], "ema_slow": e_slow[i],
        "entry_long": entry_long, "entry_short": entry_short,
        "exit_long": cross_dn, "exit_short": cross_up,
        "candle_time": candles[i]["time"],
        "sparkline": sparkline,
    }


# ---------------------------------------------------------------------------
# NEXUSBOT V3 — weighted composite scorer
# ---------------------------------------------------------------------------

def technical_score(sig: dict) -> float:
    """Convert raw analyze() output to a [0, 1] bullish score.

    1.0 = strong long signal, 0.0 = strong short signal, 0.5 = neutral.
    Uses RSI position relative to midpoint + EMA alignment.
    """
    rsi_val = sig["rsi"]
    fast, slow = sig["ema_fast"], sig["ema_slow"]
    price = sig["price"]

    # RSI component: map 0-100 to 0-1 (mid=50 → 0.5)
    rsi_score = rsi_val / 100

    # EMA component: fast vs slow + price vs slow
    ema_bull = 1.0 if fast > slow and price > slow else 0.0
    ema_bear = 1.0 if fast < slow and price < slow else 0.0
    ema_score = 0.75 if ema_bull else (0.25 if ema_bear else 0.5)

    return 0.5 * rsi_score + 0.5 * ema_score


def volatility_score(sig: dict, vol_halt_atr_pct: float) -> float:
    """Volatility has no directional opinion — it never votes long or short.

    Always neutral (0.5). Trade-or-no-trade gating on high ATR% is handled
    separately by the hard `atr_pct < vol_halt_atr_pct` check in engine.tick()
    / backtest's composite decider — that's a binary "skip this symbol" gate,
    not a signal that should bias the composite score toward one side.

    NOTE: an earlier version returned a value in [0.5, 1.0] for any tradeable
    (low-vol) condition and only dropped below 0.5 once volatility was already
    high enough to be hard-blocked elsewhere. That meant this signal could
    never actually push the composite score toward SHORT — only ever toward
    LONG — biasing every symbol's entries toward longs regardless of market
    direction. Returning a constant here keeps long/short opportunities
    symmetric; the weight on "volatility" still exists for the AI Architect/
    Autopilot to use as a dial, it just no longer skews direction.
    """
    return 0.5


def btc_dom_score(btc_dom: float, symbol: str) -> float:
    """Interpret BTC dominance per symbol.

    BTC / BTC-pegged: high dominance = bullish → score = dominance.
    Everything else: high dominance = rotation away from alts → bearish.
    """
    if symbol.startswith("BTC"):
        return btc_dom
    return 1.0 - btc_dom


def weighted_score(
    sig: dict,
    candles: list[dict],
    external: dict,
    weights: dict,
    vol_halt_atr_pct: float,
    symbol: str,
) -> dict:
    """Compute composite [0, 1] score from all signal sources.

    Args:
        sig:             Output of analyze().
        candles:         Raw candle list (for volume delta).
        external:        Dict with fear_greed, btc_dominance, news_sentiment,
                         reddit_buzz values.
        weights:         Dict mapping signal name → weight (should sum to 1).
        vol_halt_atr_pct: ATR% threshold from config.
        symbol:          Pionex symbol string.

    Returns:
        Dict with per-signal scores and final composite score.
    """
    from . import signals as sig_mod  # lazy import to avoid circular

    scores = {
        "technical":    technical_score(sig),
        "volatility":   volatility_score(sig, vol_halt_atr_pct),
        "fear_greed":   external.get("fear_greed", 0.5),
        "news":         external.get("news_sentiment", 0.5),
        "reddit":       external.get("reddit_buzz", 0.5),
        "volume_delta": sig_mod.volume_delta(candles),
        "btc_dom":      btc_dom_score(external.get("btc_dominance", 0.5), symbol),
    }

    total_weight = sum(weights.values()) or 1.0
    composite = sum(weights.get(k, 0) * v for k, v in scores.items()) / total_weight

    return {
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "composite": round(composite, 3),
        "weights": weights,
    }
