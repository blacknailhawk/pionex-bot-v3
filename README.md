# Helm — Pionex spot bot with phone console

Autonomous trading bot for Pionex with a mobile-first web dashboard.
Market data and charts come from Pionex's free public API (no key needed),
which covers the full Pionex universe — crypto, tokenized stocks (xStocks),
index ETFs and commodities; execution goes through Pionex's signed REST API.

**No strategy guarantees profit.** The bot ships in PAPER mode with a $1,000
simulated balance. Run it in paper for at least 2–4 weeks across different
market regimes before flipping to live, and only fund the Pionex account with
what you can afford to lose.

## Architecture

```
phone (PWA dashboard) ──Pangolin──▶ Mac Mini :8090
                                      │ FastAPI + engine loop (60s)
                                      ├─▶ Pionex public API  (klines, prices — free)
                                      └─▶ Pionex signed API  (orders, balances)
                                      SQLite at ./data/bot.db
```

## Universe

Crypto majors (BTC, ETH, SOL) plus every index/financial product Pionex
lists: index ETFs **SPYX** (S&P 500) and **QQQX** (Nasdaq-100), xStocks
equities (AAPLX, AMZNX, GOOGLX, METAX, NVDAX, TSLAX, CRCLX, BMNRX), and
commodities (XAUT/PAXG gold, SLVX silver, USOX oil) — 17 symbols analyzed,
up to 10 concurrent trades. Edit `SYMBOLS` in `.env` to change.

xStocks track the underlying but trade 24/7 on thin books — expect gaps at
market open and wider spreads off-hours; the ATR volatility filter helps but
won't catch everything.

## Strategy

Long **and short**, per symbol on 15m candles:
- **Long entry**: EMA12 crosses above EMA26 on a closed candle, RSI(14) in
  50–72, price above EMA26, and ATR < 6% of price (volatility filter).
- **Short entry**: mirror image — EMA12 crosses below EMA26, RSI(14) in
  28–50, price below EMA26, same ATR filter.
- **Exit**: ATR(14) trailing stop at 2×ATR (ratchets in the trade's favor
  only), take-profit at 3×ATR, or EMA cross back against the position.

**Shorts are simulated in paper mode only.** Pionex's spot API cannot open
margin shorts and its leveraged tokens are not API-tradable, so in live mode
short signals are logged and skipped — only longs execute with real funds.

Risk management:
- Position size = equity × risk_per_trade ÷ stop distance, capped at 25%
  equity per position and 10 concurrent positions.
- **Daily circuit breaker**: if day PnL hits −3%, entries halt until the next
  UTC day (BREAKER lamp goes TRIPPED on the dashboard). Exits keep running.
- All parameters adjustable live from the Settings tab.

## Strategies tab — backtesting

The **Strategies** tab backtests six rule sets against Binance historical
candles (crypto majors only — BTC/ETH/SOL-class; Pionex's xStocks/ETF/
commodity synthetics have no free historical source):

- **EMA 12/26 + RSI** — the bot's actual live entry rule
- **RSI Mean Reversion** — RSI<30 buy / >70 short, exit through 50
- **MACD Crossover** — MACD(12,26,9) line/signal cross
- **Bollinger Band Reversion** — buy/short on band re-entry (20, 2σ)
- **Composite — current live weights** — the 7-signal scorer with whatever
  weights/thresholds are active now (including AI Architect changes)
- **Composite — default weights** — same scorer reset to factory defaults,
  as a baseline to compare against your tuned settings

Every strategy is simulated through the same ATR stop/take-profit and
risk-based position sizing as the live engine, against a $1,000 starting
balance, and reports trade count, win rate, total return, max drawdown and
profit factor. Pick a symbol/interval/candle count and hit **Run
Backtests**; click any row to plot its equity curve.

**Caveat on composite strategies**: fear & greed, BTC dominance, and news
sentiment have no free historical time series, so those values are fetched
once (current reading) and held constant across the whole backtest window.
Composite results are indicative of current-regime behavior, not an exact
historical replay — the four technical strategies (EMA, RSI, MACD,
Bollinger) are fully historical and exact.

Endpoints: `GET /api/backtest/symbols`, `GET /api/backtest/strategies`,
`GET /api/backtest/run?symbol=BTC_USDT&interval=15m&limit=500`.

## Setup (Mac Mini)

```bash
cp .env.example .env        # fill in keys + a strong DASHBOARD_TOKEN
docker compose up -d --build
# dashboard at http://macmini.larran.com:8090 — route through Pangolin for remote
```

Pionex API key: pionex.com → API Management → create key with **trade
permission only, never withdrawal**. Bind it to your home egress IP if Pionex
offers IP allowlisting.

## Before going live — verify the signature

`app/pionex.py` implements Pionex's HMAC-SHA256 signing
(METHOD + sorted-query path + body). **Verify against the current docs at
https://pionex-doc.gitbook.io before live trading** — exchanges change signing
details. Quick test from inside the container:

```bash
docker exec -it pionex-bot python -c "
import asyncio
from app.pionex import PionexClient
print(asyncio.run(PionexClient().balances()))"
```

If that prints your balances, signing is correct. Pionex market buys are
quote-denominated (`amount`), sells are base-denominated (`size`); minimum
order is ~10 USDT.

## Notes

- Live fills are estimated at last price for bookkeeping; reconcile against
  Pionex's order history periodically. A future improvement is reading actual
  fill data from the order response / fills endpoint.
- Add to home screen on iOS for an app-like experience.
- The `Flatten` button in Settings market-closes everything immediately.
- Logs: `docker logs -f pionex-bot`.
