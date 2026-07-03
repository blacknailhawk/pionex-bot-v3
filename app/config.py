import os

def env(key, default=None, cast=str):
    v = os.getenv(key, default)
    if v is None:
        return None
    return cast(v)

class Cfg:
    PIONEX_KEY = env("PIONEX_API_KEY", "")
    PIONEX_SECRET = env("PIONEX_API_SECRET", "")
    DASHBOARD_TOKEN = env("DASHBOARD_TOKEN", "changeme")

    # Trading universe (Pionex symbol format BASE_QUOTE).
    # Crypto majors + every Pionex index/financial product: xStocks equities
    # (AAPLX…), index ETFs (SPYX=S&P500, QQQX=Nasdaq-100), commodities
    # (XAUT/PAXG=gold, SLVX=silver, USOX=oil).
    DEFAULT_SYMBOLS = (
        "BTC_USDT,ETH_USDT,SOL_USDT,"
        "SPYX_USDT,QQQX_USDT,"
        "AAPLX_USDT,AMZNX_USDT,GOOGLX_USDT,METAX_USDT,NVDAX_USDT,TSLAX_USDT,"
        "CRCLX_USDT,BMNRX_USDT,"
        "XAUT_USDT,PAXG_USDT,SLVX_USDT,USOX_USDT"
    )
    SYMBOLS = [s.strip() for s in env("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
    INTERVAL = env("INTERVAL", "15m")            # candle interval for signals
    LOOP_SECONDS = env("LOOP_SECONDS", "60", int)

    # Risk parameters (changeable at runtime via dashboard)
    RISK_PER_TRADE = env("RISK_PER_TRADE", "0.01", float)    # 1% equity risked per trade
    MAX_ALLOC_PER_POS = env("MAX_ALLOC_PER_POS", "0.25", float)  # 25% equity cap per position
    MAX_POSITIONS = env("MAX_POSITIONS", "10", int)
    DAILY_LOSS_HALT = env("DAILY_LOSS_HALT", "0.03", float)  # halt at -3% on the day
    ATR_STOP_MULT = env("ATR_STOP_MULT", "2.0", float)
    ATR_TP_MULT = env("ATR_TP_MULT", "3.0", float)
    VOL_HALT_ATR_PCT = env("VOL_HALT_ATR_PCT", "0.06", float)  # skip entries if ATR > 6% of price
    MIN_NOTIONAL = env("MIN_NOTIONAL", "12", float)          # USDT, Pionex min order ~10
    FEE_RATE = env("FEE_RATE", "0.0005", float)              # 0.05% taker, used in paper fills

    MODE = env("MODE", "paper")  # paper | live
    DB_PATH = env("DB_PATH", "/data/bot.db")

    # External AI / data API keys (optional)
    ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
    EXA_API_KEY = env("EXA_API_KEY", "")

    # ---------------------------------------------------------------------------
    # NEXUSBOT V3 — weighted scoring engine defaults
    # ---------------------------------------------------------------------------
    # Weights should sum to 1.0.  Changeable at runtime from the dashboard.
    DEFAULT_SIGNAL_WEIGHTS = {
        "technical":    0.30,   # EMA12/26 cross + RSI alignment
        "volatility":   0.10,   # inverted ATR% (low vol = better entry)
        "fear_greed":   0.15,   # Fear & Greed Index (alternative.me)
        "news":         0.20,   # News sentiment (Exa.ai; neutral stub if no key)
        "reddit":       0.10,   # Social buzz (stub 0.5)
        "volume_delta": 0.10,   # On-chain buy/sell volume pressure
        "btc_dom":      0.05,   # BTC dominance rotation signal
    }

    # Score thresholds for entry decisions (changeable at runtime)
    BUY_THRESHOLD = env("BUY_THRESHOLD", "0.62", float)    # score > this → LONG
    SELL_THRESHOLD = env("SELL_THRESHOLD", "0.38", float)   # score < this → SHORT

    # AI Autopilot: Claude re-evaluates weights + thresholds every N ticks
    AI_AUTOPILOT_INTERVAL = env("AI_AUTOPILOT_INTERVAL", "5", int)  # ticks between AI updates

    # Market-hours gate: block NEW entries when the relevant market is closed.
    # Exits (stop, TP, trend) are always processed regardless of this flag.
    # Overridable at runtime from the dashboard.
    MARKET_HOURS_ONLY = env("MARKET_HOURS_ONLY", "true").lower() in ("true", "1", "yes")


cfg = Cfg()
