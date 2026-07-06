"""Trading engine — long and short.

Risk controls:
- Risk-based sizing: notional = equity * risk_per_trade / stop_distance%
- Hard cap per position (MAX_ALLOC_PER_POS) and max concurrent positions
- ATR trailing stop + take-profit on every position (both directions)
- Daily circuit breaker: total PnL <= -DAILY_LOSS_HALT * day-start equity => HALT
  (no new entries until next UTC day; exits still managed)
- Volatility filter: no entries when ATR% exceeds threshold

Shorts: fully simulated in paper mode (notional held as collateral,
pnl = (entry - price) * size). Pionex's spot API cannot open margin shorts
and leveraged tokens are not API-tradable, so in LIVE mode short signals
are logged and skipped.
"""
import asyncio
import datetime as dt
import logging
import time

from . import market_data, market_hours, signals as sig_mod, store, strategy
from .config import cfg
from .pionex import PionexClient, PionexError

log = logging.getLogger("engine")

PAPER_START_EQUITY = 1000.0


class Engine:
    def __init__(self):
        store.init()
        self.client = PionexClient()
        self.paused = store.kv_get("paused", False)
        self.mode = store.kv_get("mode", cfg.MODE)
        self.halted_until = store.kv_get("halted_until", 0)
        self.risk = store.kv_get("risk", {
            "risk_per_trade": cfg.RISK_PER_TRADE,
            "max_alloc": cfg.MAX_ALLOC_PER_POS,
            "max_positions": cfg.MAX_POSITIONS,
            "daily_loss_halt": cfg.DAILY_LOSS_HALT,
        })
        self.last_signals = {}
        self.last_error = None
        self.last_scores = {}      # per-symbol composite scores
        self.market_sigs = {}      # market-wide signals (fear_greed, btc_dom…)
        if store.kv_get("paper_cash") is None:
            store.kv_set("paper_cash", PAPER_START_EQUITY)
        if store.kv_get("day_anchor") is None:
            self._reset_day_anchor(PAPER_START_EQUITY)

    # ---------- scoring config ----------
    @property
    def signal_weights(self) -> dict:
        return store.kv_get("signal_weights", cfg.DEFAULT_SIGNAL_WEIGHTS)

    @property
    def score_thresholds(self) -> dict:
        return store.kv_get("score_thresholds", {
            "buy": cfg.BUY_THRESHOLD,
            "sell": cfg.SELL_THRESHOLD,
        })

    def set_weights(self, weights: dict, thresholds: dict | None = None):
        store.kv_set("signal_weights", weights)
        if thresholds:
            store.kv_set("score_thresholds", thresholds)

    # ---------- market-hours gate ----------
    @property
    def market_hours_only(self) -> bool:
        return store.kv_get("market_hours_only", cfg.MARKET_HOURS_ONLY)

    def set_market_hours_only(self, value: bool):
        store.kv_set("market_hours_only", value)

    # ---------- AI autopilot ----------
    @property
    def ai_autopilot(self) -> bool:
        return store.kv_get("ai_autopilot", False)

    def set_ai_autopilot(self, value: bool):
        store.kv_set("ai_autopilot", value)
        if value:
            log.info("AI AUTOPILOT enabled — Claude will manage weights & thresholds")
        else:
            log.info("AI AUTOPILOT disabled — manual weights restored")

    # ---------- AI trader ----------
    @property
    def ai_trader(self) -> bool:
        return store.kv_get("ai_trader", False)

    def set_ai_trader(self, value: bool):
        store.kv_set("ai_trader", value)
        if value:
            log.info("AI TRADER enabled — Claude will evaluate and execute trades")
        else:
            log.info("AI TRADER disabled — standard score-based entries/exits restored")

    async def _autopilot_update(self):
        """Ask Claude to re-evaluate all signal weights and entry thresholds
        given the current market state, then apply the result immediately."""
        if not cfg.ANTHROPIC_API_KEY:
            log.warning("AI Autopilot: ANTHROPIC_API_KEY not set — skipping")
            return
        try:
            import anthropic as _ant
            import json as _json

            context = {
                "market_signals": self.market_sigs,
                "symbol_scores": {
                    sym: {"composite": sc.get("composite"), "scores": sc.get("scores", {})}
                    for sym, sc in self.last_scores.items()
                },
                "current_weights": self.signal_weights,
                "current_thresholds": self.score_thresholds,
                "open_positions": len(store.positions()),
            }

            system = (
                "You are the autonomous strategy engine for NEXUSBOT, a crypto trading bot.\n"
                "Analyse the market state and output ONLY a single JSON object — no prose, no markdown:\n"
                '{"weights":{"technical":0.30,"volatility":0.10,"fear_greed":0.15,'
                '"news":0.20,"reddit":0.10,"volume_delta":0.10,"btc_dom":0.05},'
                '"thresholds":{"buy":0.62,"sell":0.38},"rationale":"<15 words"}\n\n'
                "Hard rules:\n"
                "- All 7 weight keys must be present; weights must sum to exactly 1.0\n"
                "- buy: 0.52–0.88  |  sell: 0.12–0.48  |  buy − sell >= 0.15\n"
                "- DEFAULT TO SYMMETRIC thresholds: (buy - 0.5) should equal (0.5 - sell) "
                "so long and short setups trigger equally often. Only break symmetry when "
                "you have a specific, stated directional rationale (e.g. strong one-sided "
                "trend) — symmetry is the default, not the exception.\n"
                "- High fear (fear_greed<0.3): raise fear_greed weight, widen thresholds "
                "(symmetrically, unless fear itself is the stated directional rationale)\n"
                "- High volatility (atr_pct>0.03): raise volatility weight, widen thresholds "
                "(symmetrically — volatility says 'trade less', not 'trade one direction')\n"
                "- Strong trend (technical>0.65 or <0.35): raise technical weight\n"
                "- Many open positions: widen thresholds to reduce new entries (symmetrically)\n"
                "- Rationale: one sentence, under 15 words. If thresholds are asymmetric, "
                "the rationale must say why."
            )

            client = _ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                system=system,
                messages=[{"role": "user", "content": _json.dumps(context)}],
            )
            raw = resp.content[0].text.strip()
            first_brace = raw.find("{")
            last_brace = raw.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                raw_json = raw[first_brace:last_brace + 1]
            else:
                raw_json = raw
            patch = _json.loads(raw_json)

            new_w = patch.get("weights", self.signal_weights)
            new_t = patch.get("thresholds", self.score_thresholds)
            rationale = patch.get("rationale", "")

            # Normalise weights
            total = sum(new_w.values()) or 1
            new_w = {k: round(v / total, 4) for k, v in new_w.items()}

            buy, sell = new_t.get("buy", 0.62), new_t.get("sell", 0.38)
            if buy > sell + 0.14 and 0.52 <= buy <= 0.88 and 0.12 <= sell <= 0.48:
                self.set_weights(new_w, new_t)
                log.info("AI PILOT ▸ %s | buy=%.2f sell=%.2f", rationale, buy, sell)
            else:
                log.warning("AI PILOT: thresholds out of range (buy=%.2f sell=%.2f) — ignored", buy, sell)

        except Exception as e:
            log.warning("AI autopilot error: %s", e)

    # ---------- equity ----------
    @staticmethod
    def _position_value(pos, price) -> float:
        """Mark-to-market value of a position incl. collateral for shorts."""
        if pos.get("side", "LONG") == "SHORT":
            # collateral (size*entry) + unrealized pnl (entry-price)*size
            return pos["size"] * (2 * pos["entry"] - price)
        return pos["size"] * price

    async def equity(self) -> float:
        if self.mode == "paper":
            eq = store.kv_get("paper_cash", PAPER_START_EQUITY)
        else:
            bal = await self.client.balances()
            eq = bal.get("USDT", 0.0)
        for p in store.positions():
            px = await market_data.last_price(p["symbol"])
            eq += self._position_value(p, px)
        return eq

    def _reset_day_anchor(self, equity_now):
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        store.kv_set("day_anchor", {"date": today, "equity": equity_now})

    async def _check_circuit_breaker(self, equity_now) -> bool:
        anchor = store.kv_get("day_anchor")
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        if anchor["date"] != today:
            self._reset_day_anchor(equity_now)
            if self.halted_until and time.time() > self.halted_until:
                self.halted_until = 0
                store.kv_set("halted_until", 0)
            return False
        dd = (equity_now - anchor["equity"]) / anchor["equity"] if anchor["equity"] else 0
        if dd <= -self.risk["daily_loss_halt"] and not self.halted_until:
            tomorrow = dt.datetime.now(dt.timezone.utc).replace(
                hour=0, minute=0, second=0) + dt.timedelta(days=1)
            self.halted_until = tomorrow.timestamp()
            store.kv_set("halted_until", self.halted_until)
            log.warning("CIRCUIT BREAKER: daily drawdown %.2f%% — halting entries", dd * 100)
        return bool(self.halted_until and time.time() < self.halted_until)

    # ---------- execution ----------
    async def _open(self, symbol, side, notional, price, stop, tp, reason):
        if side == "SHORT" and self.mode == "live":
            log.warning("SHORT signal on %s skipped: Pionex spot API cannot short "
                        "(paper mode only)", symbol)
            return
        fee = notional * cfg.FEE_RATE
        size = (notional - fee) / price
        if self.mode == "paper":
            # both directions lock `notional` as cash/collateral
            store.kv_set("paper_cash", store.kv_get("paper_cash") - notional)
        else:
            res = await self.client.market_buy(symbol, notional)
            # Pionex fills market buys by quote amount; estimate size at last price
            log.info("LIVE BUY %s: %s", symbol, res)
        store.open_position(symbol, side, size, price, stop, tp, self.mode)
        store.record_trade(symbol, "BUY" if side == "LONG" else "SHORT",
                           price, size, fee, None, reason, self.mode)
        log.info("%s %s size=%.6f @ %.4f stop=%.4f tp=%.4f",
                 side, symbol, size, price, stop, tp)

    async def _close(self, symbol, pos, price, reason):
        side = pos.get("side", "LONG")
        size = pos["size"]
        entry = pos["entry"]
        fee = size * price * cfg.FEE_RATE
        # Entry fee was already paid at open (baked into a smaller position size via
        # size = (notional - fee_entry) / entry), so it never appears as a subtracted
        # amount anywhere. Recover it here so the recorded P&L reflects the true
        # round-trip cost, not just the exit fee.
        fee_entry = size * entry * cfg.FEE_RATE / (1 - cfg.FEE_RATE)
        if side == "LONG":
            cash_pnl = (price - entry) * size - fee
            if self.mode == "paper":
                store.kv_set("paper_cash",
                             store.kv_get("paper_cash") + size * price - fee)
            else:
                res = await self.client.market_sell(symbol, size)
                log.info("LIVE SELL %s: %s", symbol, res)
        else:  # SHORT (paper only): return collateral + pnl
            cash_pnl = (entry - price) * size - fee
            store.kv_set("paper_cash",
                         store.kv_get("paper_cash") + size * entry + cash_pnl)
        pnl = cash_pnl - fee_entry
        store.close_position(symbol)
        store.record_trade(symbol, "SELL" if side == "LONG" else "COVER",
                           price, size, fee, pnl, reason, self.mode)
        log.info("CLOSE %s %s @ %.4f pnl=%.2f (%s)", side, symbol, price, pnl, reason)

    # ---------- main loop ----------
    async def tick(self):
        import json as _json
        import anthropic as _ant

        equity_now = await self.equity()
        store.snapshot_equity(equity_now, self.mode)
        halted = await self._check_circuit_breaker(equity_now)

        # Fetch market-wide signals once per tick (cached internally)
        try:
            self.market_sigs = await sig_mod.market_signals()
        except Exception as e:
            log.warning("market_signals error: %s", e)

        # AI Autopilot: let Claude update strategy every N ticks
        if self.ai_autopilot:
            self._ai_counter = getattr(self, "_ai_counter", 0) + 1
            if self._ai_counter >= cfg.AI_AUTOPILOT_INTERVAL:
                self._ai_counter = 0
                await self._autopilot_update()

        weights = self.signal_weights
        thresholds = self.score_thresholds

        # 1. Gather states for all symbols
        symbol_states = {}
        for symbol in cfg.SYMBOLS:
            try:
                candles = await market_data.klines(symbol, cfg.INTERVAL, 300)
                sig = strategy.analyze(candles, cfg.VOL_HALT_ATR_PCT)

                # Per-symbol external signals
                news = await sig_mod.news_sentiment(symbol, cfg.EXA_API_KEY)
                external = {**self.market_sigs, "news_sentiment": news, "reddit_buzz": 0.5}

                # Composite score
                scored = strategy.weighted_score(
                    sig, candles, external, weights, cfg.VOL_HALT_ATR_PCT, symbol)
                sig["score"] = scored["composite"]
                sig["signal_scores"] = scored["scores"]
                self.last_signals[symbol] = sig
                self.last_scores[symbol] = scored

                price = await market_data.last_price(symbol)
                pos = store.get_position(symbol)
                market_open = market_hours.is_market_open(symbol)

                symbol_states[symbol] = {
                    "price": price,
                    "composite_score": scored["composite"],
                    "rsi": sig.get("rsi"),
                    "atr_pct": sig.get("atr_pct"),
                    "vol_ok": sig.get("atr_pct") < cfg.VOL_HALT_ATR_PCT,
                    "market_open": market_open,
                    "position": {
                        "side": pos["side"],
                        "entry": pos["entry"],
                        "stop": pos["stop"],
                        "tp": pos["tp"],
                        "unrealized_pnl": (price - pos["entry"]) * pos["size"] if pos["side"] == "LONG" else (pos["entry"] - price) * pos["size"]
                    } if pos else None
                }
            except (PionexError, Exception) as e:
                self.last_error = f"{symbol}: {e}"
                log.exception("tick error gathering state %s", symbol)

        # 2. Check stops / TP safety closures first (risk controls)
        for symbol, state in symbol_states.items():
            pos = store.get_position(symbol)
            if pos:
                try:
                    price = state["price"]
                    sig = self.last_signals[symbol]
                    side = pos.get("side", "LONG")
                    if side == "LONG":
                        # trail the stop upward
                        new_stop = price - cfg.ATR_STOP_MULT * sig["atr"]
                        if new_stop > pos["stop"]:
                            store.update_stop(symbol, new_stop)
                            pos["stop"] = new_stop
                        if price <= pos["stop"]:
                            await self._close(symbol, pos, price, "stop")
                            state["position"] = None
                        elif price >= pos["tp"]:
                            await self._close(symbol, pos, price, "take_profit")
                            state["position"] = None
                    else:
                        # trail the stop downward
                        new_stop = price + cfg.ATR_STOP_MULT * sig["atr"]
                        if new_stop < pos["stop"]:
                            store.update_stop(symbol, new_stop)
                            pos["stop"] = new_stop
                        if price >= pos["stop"]:
                            await self._close(symbol, pos, price, "stop")
                            state["position"] = None
                        elif price <= pos["tp"]:
                            await self._close(symbol, pos, price, "take_profit")
                            state["position"] = None
                except Exception as ex_err:
                    log.exception("safety exit check failed for %s: %s", symbol, ex_err)

        # 3. AI Direct Trader evaluation
        ai_trade_success = False
        if self.ai_trader and cfg.ANTHROPIC_API_KEY and not self.paused and not halted:
            try:
                system = (
                    "You are the autonomous trading brain of NEXUSBOT.\n"
                    "Analyse the current portfolio and market states, then output a JSON block "
                    "containing trade decisions for each symbol.\n\n"
                    "For each symbol, you can make one of the following decisions:\n"
                    "- \"OPEN_LONG\": Open a long position. Valid only if we do not already have a position on this symbol.\n"
                    "- \"OPEN_SHORT\": Open a short position. Valid only if we do not already have a position on this symbol.\n"
                    "- \"CLOSE\": Close the current position (long or short) on this symbol. Valid only if we have an open position on this symbol.\n"
                    "- \"HOLD\": Do nothing (default if symbol is omitted from the decisions object).\n\n"
                    "Output ONLY a valid JSON object in the following format (no prose, no markdown codeblocks):\n"
                    "{\n"
                    "  \"decisions\": {\n"
                    "    \"BTC_USDT\": {\"action\": \"OPEN_LONG\", \"rationale\": \"Strong uptrend + score breakout\"},\n"
                    "    \"ETH_USDT\": {\"action\": \"CLOSE\", \"rationale\": \"Technical signals weakening\"}\n"
                    "  }\n"
                    "}\n\n"
                    "Hard rules:\n"
                    "1. If a position is already open for a symbol, you can only choose \"CLOSE\" or \"HOLD\".\n"
                    "2. If no position is open for a symbol, you can only choose \"OPEN_LONG\", \"OPEN_SHORT\", or \"HOLD\".\n"
                    "3. You must not exceed the max_positions limit of open positions (total open positions after new entries must be <= max_positions).\n"
                    "4. Only open LONG or SHORT if vol_ok is true and market_open is true for that symbol.\n"
                    "5. Provide a short, data-driven rationale (under 15 words) for each non-HOLD decision.\n"
                    "6. If the symbol's market is closed, you must not open new positions (action must be HOLD)."
                )

                context = {
                    "max_positions": self.risk["max_positions"],
                    "current_positions_count": len(store.positions()),
                    "available_cash": (store.kv_get("paper_cash") if self.mode == "paper"
                                      else (await self.client.balances()).get("USDT", 0.0)),
                    "market_signals": self.market_sigs,
                    "symbols_data": {
                        sym: {
                            "price": state["price"],
                            "composite_score": round(state["composite_score"], 3),
                            "rsi": round(state["rsi"], 1) if state["rsi"] is not None else None,
                            "atr_pct": round(state["atr_pct"], 4),
                            "vol_ok": state["vol_ok"],
                            "market_open": state["market_open"],
                            "position": {
                                "side": state["position"]["side"],
                                "entry": state["position"]["entry"],
                                "unrealized_pnl_pct": round(
                                    (state["price"] - state["position"]["entry"]) / state["position"]["entry"] * 100
                                    if state["position"]["side"] == "LONG" else
                                    (state["position"]["entry"] - state["price"]) / state["position"]["entry"] * 100,
                                    2
                                )
                            } if state["position"] else None
                        }
                        for sym, state in symbol_states.items()
                    }
                }

                client = _ant.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=500,
                    system=system,
                    messages=[{"role": "user", "content": _json.dumps(context)}],
                )
                raw = resp.content[0].text.strip()
                first_brace = raw.find("{")
                last_brace = raw.rfind("}")
                if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                    raw_json = raw[first_brace:last_brace + 1]
                else:
                    raw_json = raw

                decision_patch = _json.loads(raw_json)
                decisions = decision_patch.get("decisions", {})

                log.info("AI TRADER ▸ Decisions: %s", _json.dumps(decisions) if decisions else "{} (all HOLD)")

                for symbol, dec in decisions.items():
                    if symbol not in symbol_states:
                        continue

                    action = dec.get("action", "HOLD")
                    rationale = dec.get("rationale", "")
                    state = symbol_states[symbol]

                    if action == "CLOSE":
                        pos = store.get_position(symbol)
                        if pos:
                            price = state["price"]
                            log.info("AI CLOSE %s %s | %s", pos["side"], symbol, rationale)
                            await self._close(symbol, pos, price, f"ai_exit: {rationale}")
                            state["position"] = None

                    elif action in ("OPEN_LONG", "OPEN_SHORT"):
                        pos = store.get_position(symbol)
                        if pos:
                            log.warning("AI TRADER tried to open position on %s but one already exists", symbol)
                            continue
                        if len(store.positions()) >= self.risk["max_positions"]:
                            log.warning("AI TRADER tried to open position on %s but max_positions reached", symbol)
                            continue
                        if not state["vol_ok"]:
                            log.warning("AI TRADER tried to open position on %s but volatility is too high", symbol)
                            continue
                        if self.market_hours_only and not state["market_open"]:
                            log.warning("AI TRADER tried to open position on %s but market is closed", symbol)
                            continue

                        price = state["price"]
                        sig = self.last_signals[symbol]
                        side = "LONG" if action == "OPEN_LONG" else "SHORT"

                        if side == "LONG":
                            stop = price - cfg.ATR_STOP_MULT * sig["atr"]
                            tp = price + cfg.ATR_TP_MULT * sig["atr"]
                        else:
                            stop = price + cfg.ATR_STOP_MULT * sig["atr"]
                            tp = price - cfg.ATR_TP_MULT * sig["atr"]

                        stop_dist = abs(price - stop) / price
                        notional = equity_now * self.risk["risk_per_trade"] / max(stop_dist, 1e-6)
                        notional = min(notional, equity_now * self.risk["max_alloc"])
                        cash = (store.kv_get("paper_cash") if self.mode == "paper"
                                else (await self.client.balances()).get("USDT", 0.0))
                        notional = min(notional, cash * 0.98)

                        if notional >= cfg.MIN_NOTIONAL:
                            log.info("AI ENTRY %s %s | %s", side, symbol, rationale)
                            await self._open(symbol, side, notional, price, stop, tp, f"ai_entry: {rationale}")

                # Only suppress the score-based fallback when the AI actually
                # made a decision this tick. An empty dict means it held on
                # everything, so let the classic entry logic still get a turn.
                ai_trade_success = bool(decisions)
            except Exception as ai_err:
                log.warning("AI Trader decision execution failed: %s", ai_err)

        # 4. Fallback / Standard logic if AI Trader was not run or failed
        if not ai_trade_success and not self.paused and not halted:
            for symbol, state in symbol_states.items():
                try:
                    pos = store.get_position(symbol)
                    sig = self.last_signals[symbol]
                    price = state["price"]

                    if pos:
                        side = pos.get("side", "LONG")
                        if side == "LONG" and sig["exit_long"]:
                            await self._close(symbol, pos, price, "trend_exit")
                        elif side == "SHORT" and sig["exit_short"]:
                            await self._close(symbol, pos, price, "trend_exit")
                        continue

                    # Market-hours gate: skip new entries when market is closed
                    if self.market_hours_only and not state["market_open"]:
                        continue

                    # Entry decision: score + vol guard
                    score = state["composite_score"]
                    if not state["vol_ok"]:
                        continue

                    if score > thresholds["buy"]:
                        side = "LONG"
                    elif score < thresholds["sell"]:
                        side = "SHORT"
                    else:
                        continue

                    if len(store.positions()) >= self.risk["max_positions"]:
                        continue

                    if side == "LONG":
                        stop = price - cfg.ATR_STOP_MULT * sig["atr"]
                        tp = price + cfg.ATR_TP_MULT * sig["atr"]
                    else:
                        stop = price + cfg.ATR_STOP_MULT * sig["atr"]
                        tp = price - cfg.ATR_TP_MULT * sig["atr"]

                    stop_dist = abs(price - stop) / price
                    notional = equity_now * self.risk["risk_per_trade"] / max(stop_dist, 1e-6)
                    notional = min(notional, equity_now * self.risk["max_alloc"])
                    cash = (store.kv_get("paper_cash") if self.mode == "paper"
                            else (await self.client.balances()).get("USDT", 0.0))
                    notional = min(notional, cash * 0.98)
                    if notional < cfg.MIN_NOTIONAL:
                        continue

                    log.info("ENTRY %s %s score=%.3f", side, symbol, score)
                    await self._open(symbol, side, notional, price, stop, tp, "score_entry")
                except (PionexError, Exception) as e:  # noqa: BLE001
                    self.last_error = f"{symbol}: {e}"
                    log.exception("tick error %s", symbol)

    async def run(self):
        store.init()
        log.info("Engine started: mode=%s symbols=%s", self.mode, cfg.SYMBOLS)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("loop error")
            await asyncio.sleep(cfg.LOOP_SECONDS)

    # ---------- controls ----------
    def set_paused(self, paused: bool):
        self.paused = paused
        store.kv_set("paused", paused)

    def set_mode(self, mode: str):
        assert mode in ("paper", "live")
        self.mode = mode
        store.kv_set("mode", mode)

    def set_risk(self, **kw):
        self.risk.update({k: v for k, v in kw.items() if k in self.risk and v is not None})
        store.kv_set("risk", self.risk)


engine = Engine()
