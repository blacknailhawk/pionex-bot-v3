"""Strategy backtesting engine — NEXUSBOT v2.

Replays historical candles through several entry/exit rule sets and reports
win rate, total return, max drawdown and profit factor for each — powers
the "Strategies" dashboard tab.

Data source: Binance public klines (free, no key) via market_data.klines().
Backtesting is scoped to crypto majors (BTC/ETH/SOL-class pairs) — Pionex's
exclusive synthetics (xStocks, index ETFs, commodities) have no Binance
equivalent and no free historical source.

Composite (multi-signal) strategies reuse the live weighted scorer from
strategy.py, but fear & greed / BTC dominance / news sentiment are fetched
*once* and held constant across the whole backtest window — there's no free
historical time series for those sources. Treat composite results as
indicative of current-regime behaviour, not an exact historical replay.
"""
import logging

from . import market_data
from . import signals as sig_mod
from . import strategy as strat
from .config import cfg

log = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
# Universe — Binance only covers crypto majors, not Pionex-exclusive synthetics
# ---------------------------------------------------------------------------

def crypto_symbols() -> list[str]:
    return [s for s in cfg.SYMBOLS if not market_data._is_pionex_only(s)]


# ---------------------------------------------------------------------------
# Extra indicators (EMA / RSI / ATR already live in strategy.py)
# ---------------------------------------------------------------------------

def _macd(closes: list[float], fast=12, slow=26, signal=9):
    ef, es = strat.ema(closes, fast), strat.ema(closes, slow)
    line = [f - s for f, s in zip(ef, es)]
    sig = strat.ema(line, signal)
    return line, sig


def _bollinger(closes: list[float], period=20, mult=2.0):
    upper, mid, lower = [], [], []
    for i in range(len(closes)):
        window = closes[max(0, i - period + 1): i + 1]
        m = sum(window) / len(window)
        var = sum((c - m) ** 2 for c in window) / len(window)
        sd = var ** 0.5
        mid.append(m)
        upper.append(m + mult * sd)
        lower.append(m - mult * sd)
    return upper, mid, lower


# ---------------------------------------------------------------------------
# Per-bar decision generators. Each returns a list of dicts:
#   {i, price, atr, entry_long, entry_short, exit_long, exit_short}
# `i` indexes into the original `candles` list (for timestamps in simulate()).
# ---------------------------------------------------------------------------

def _decide_ema_rsi(candles):
    closes = [c["close"] for c in candles]
    ef, es, r = strat.ema(closes, 12), strat.ema(closes, 26), strat.rsi(closes, 14)
    a = strat.atr(candles, 14)
    out = []
    for i in range(27, len(candles)):
        price = closes[i]
        atr_pct = a[i] / price if price else 0
        cross_up = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        cross_dn = ef[i] < es[i] and ef[i - 1] >= es[i - 1]
        vol_ok = atr_pct <= cfg.VOL_HALT_ATR_PCT
        out.append({
            "i": i, "price": price, "atr": a[i],
            "entry_long": cross_up and 50 <= r[i] <= 72 and price > es[i] and vol_ok,
            "entry_short": cross_dn and 28 <= r[i] <= 50 and price < es[i] and vol_ok,
            "exit_long": cross_dn, "exit_short": cross_up,
        })
    return out


def _decide_rsi_meanrev(candles):
    closes = [c["close"] for c in candles]
    r = strat.rsi(closes, 14)
    a = strat.atr(candles, 14)
    out = []
    for i in range(16, len(candles)):
        price = closes[i]
        out.append({
            "i": i, "price": price, "atr": a[i],
            "entry_long": r[i] < 30 and r[i - 1] >= 30,
            "entry_short": r[i] > 70 and r[i - 1] <= 70,
            "exit_long": r[i] >= 55, "exit_short": r[i] <= 45,
        })
    return out


def _decide_macd(candles):
    closes = [c["close"] for c in candles]
    line, sig = _macd(closes)
    a = strat.atr(candles, 14)
    out = []
    for i in range(35, len(candles)):
        price = closes[i]
        cross_up = line[i] > sig[i] and line[i - 1] <= sig[i - 1]
        cross_dn = line[i] < sig[i] and line[i - 1] >= sig[i - 1]
        out.append({
            "i": i, "price": price, "atr": a[i],
            "entry_long": cross_up, "entry_short": cross_dn,
            "exit_long": cross_dn, "exit_short": cross_up,
        })
    return out


def _decide_bollinger(candles):
    closes = [c["close"] for c in candles]
    upper, mid, lower = _bollinger(closes, 20, 2.0)
    a = strat.atr(candles, 14)
    out = []
    for i in range(21, len(candles)):
        price = closes[i]
        out.append({
            "i": i, "price": price, "atr": a[i],
            "entry_long": price <= lower[i] and closes[i - 1] > lower[i - 1],
            "entry_short": price >= upper[i] and closes[i - 1] < upper[i - 1],
            "exit_long": price >= mid[i], "exit_short": price <= mid[i],
        })
    return out


def _decide_composite(candles, weights, thresholds, external, symbol):
    closes = [c["close"] for c in candles]
    ef, es, r = strat.ema(closes, 12), strat.ema(closes, 26), strat.rsi(closes, 14)
    a = strat.atr(candles, 14)
    out = []
    for i in range(27, len(candles)):
        price = closes[i]
        atr_pct = a[i] / price if price else 0
        sig = {"rsi": r[i], "ema_fast": ef[i], "ema_slow": es[i],
               "price": price, "atr_pct": atr_pct}
        scored = strat.weighted_score(sig, candles[: i + 1], external,
                                      weights, cfg.VOL_HALT_ATR_PCT, symbol)
        composite = scored["composite"]
        cross_up = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        cross_dn = ef[i] < es[i] and ef[i - 1] >= es[i - 1]
        vol_ok = atr_pct < cfg.VOL_HALT_ATR_PCT
        out.append({
            "i": i, "price": price, "atr": a[i],
            "entry_long": vol_ok and composite > thresholds["buy"],
            "entry_short": vol_ok and composite < thresholds["sell"],
            "exit_long": cross_dn, "exit_short": cross_up,
        })
    return out


# ---------------------------------------------------------------------------
# Generic trade simulator — mirrors the live engine's ATR stop/TP + risk sizing
# ---------------------------------------------------------------------------

def simulate(candles, decisions, allow_short=True, starting_equity=1000.0):
    stop_mult, tp_mult = cfg.ATR_STOP_MULT, cfg.ATR_TP_MULT
    risk_per_trade, max_alloc = cfg.RISK_PER_TRADE, cfg.MAX_ALLOC_PER_POS

    cash = starting_equity
    pos = None
    trades = []
    equity_curve = []
    peak = starting_equity
    max_dd = 0.0

    for d in decisions:
        price = d["price"]

        if pos:
            if pos["side"] == "LONG":
                new_stop = price - stop_mult * d["atr"]
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                hit_stop, hit_tp = price <= pos["stop"], price >= pos["tp"]
                exit_sig = d["exit_long"]
            else:
                new_stop = price + stop_mult * d["atr"]
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop
                hit_stop, hit_tp = price >= pos["stop"], price <= pos["tp"]
                exit_sig = d["exit_short"]

            if hit_stop or hit_tp or exit_sig:
                reason = "stop" if hit_stop else ("take_profit" if hit_tp else "trend_exit")
                fee = pos["size"] * price * cfg.FEE_RATE
                if pos["side"] == "LONG":
                    pnl = (price - pos["entry"]) * pos["size"] - fee
                    cash += pos["size"] * price - fee
                else:
                    pnl = (pos["entry"] - price) * pos["size"] - fee
                    cash += pos["size"] * pos["entry"] + pnl
                trades.append({
                    "side": pos["side"], "entry": round(pos["entry"], 6),
                    "exit": round(price, 6), "pnl": round(pnl, 2),
                    "reason": reason, "time": candles[d["i"]]["time"],
                })
                pos = None

        if not pos:
            side = None
            if d["entry_long"]:
                side = "LONG"
            elif allow_short and d["entry_short"]:
                side = "SHORT"
            if side:
                equity_now = cash  # flat when entering — no open position to mark
                stop = price - stop_mult * d["atr"] if side == "LONG" else price + stop_mult * d["atr"]
                tp = price + tp_mult * d["atr"] if side == "LONG" else price - tp_mult * d["atr"]
                stop_dist = abs(price - stop) / price
                notional = equity_now * risk_per_trade / max(stop_dist, 1e-6)
                notional = min(notional, equity_now * max_alloc, cash * 0.98)
                if notional >= cfg.MIN_NOTIONAL:
                    fee = notional * cfg.FEE_RATE
                    size = (notional - fee) / price
                    cash -= notional
                    pos = {"side": side, "entry": price, "size": size, "stop": stop, "tp": tp}

        mtm = cash
        if pos:
            mtm += pos["size"] * price if pos["side"] == "LONG" else pos["size"] * (2 * pos["entry"] - price)
        peak = max(peak, mtm)
        dd = (mtm - peak) / peak if peak else 0
        max_dd = min(max_dd, dd)
        equity_curve.append({"time": candles[d["i"]]["time"], "value": round(mtm, 2)})

    final_equity = equity_curve[-1]["value"] if equity_curve else starting_equity
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    return {
        "num_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "total_return_pct": round((final_equity - starting_equity) / starting_equity * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "final_equity": round(final_equity, 2),
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "trades": trades[-50:],   # cap payload size
        "equity_curve": equity_curve,
    }


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

STRATEGIES = [
    {"id": "ema_rsi", "name": "EMA 12/26 + RSI (live strategy)",
     "description": "The bot's actual live entry rule: EMA12/26 cross + RSI(14) "
                     "in 50-72 (long) / 28-50 (short), with an ATR volatility filter."},
    {"id": "rsi_meanrev", "name": "RSI Mean Reversion",
     "description": "Buy when RSI(14) drops below 30, short when above 70; "
                     "exit on RSI reverting back through 50."},
    {"id": "macd", "name": "MACD Crossover",
     "description": "MACD(12,26,9) line/signal-line crossover, long and short."},
    {"id": "bollinger", "name": "Bollinger Band Reversion",
     "description": "Buy on a close back inside the lower band (20, 2σ); "
                     "short on a close back inside the upper band."},
    {"id": "composite_live", "name": "Composite — current live weights",
     "description": "NEXUSBOT v2's weighted 7-signal scorer using whatever weights/"
                     "thresholds are active right now (including AI Architect changes)."},
    {"id": "composite_default", "name": "Composite — default weights",
     "description": "Same weighted scorer, reset to factory-default weights/thresholds — "
                     "a baseline to compare against your tuned live settings."},
]

_DECIDERS = {
    "ema_rsi": _decide_ema_rsi,
    "rsi_meanrev": _decide_rsi_meanrev,
    "macd": _decide_macd,
    "bollinger": _decide_bollinger,
}

_EMPTY_OUTCOME = {
    "num_trades": 0, "win_rate": 0.0, "total_return_pct": 0.0,
    "max_drawdown_pct": 0.0, "profit_factor": None, "final_equity": 1000.0,
    "avg_win": 0.0, "avg_loss": 0.0, "trades": [], "equity_curve": [],
}


async def run_all(symbol: str, interval: str = "15m", limit: int = 500) -> dict:
    """Backtest every registered strategy on `symbol`, ranked by total return."""
    limit = min(max(int(limit), 60), 1000)   # Binance caps at 1000 candles/request
    candles = await market_data.klines(symbol, interval, limit)

    # External signals for composite strategies — fetched once, held constant
    # (see module docstring: no free historical series for these sources).
    from .engine import engine  # lazy import — avoids circular import at module load
    mkt = await sig_mod.market_signals()
    news = await sig_mod.news_sentiment(symbol, cfg.EXA_API_KEY)
    external = {**mkt, "news_sentiment": news}

    results = []
    for s in STRATEGIES:
        try:
            if s["id"] == "composite_live":
                decisions = _decide_composite(candles, engine.signal_weights,
                                              engine.score_thresholds, external, symbol)
            elif s["id"] == "composite_default":
                decisions = _decide_composite(
                    candles, cfg.DEFAULT_SIGNAL_WEIGHTS,
                    {"buy": cfg.BUY_THRESHOLD, "sell": cfg.SELL_THRESHOLD},
                    external, symbol)
            else:
                decisions = _DECIDERS[s["id"]](candles)
            outcome = simulate(candles, decisions)
        except Exception as e:
            log.exception("backtest failed for %s/%s", s["id"], symbol)
            outcome = {**_EMPTY_OUTCOME, "error": str(e)}
        results.append({"id": s["id"], "name": s["name"], "description": s["description"], **outcome})

    results.sort(key=lambda r: r["total_return_pct"], reverse=True)
    return {"symbol": symbol, "interval": interval, "candles": len(candles), "results": results}
