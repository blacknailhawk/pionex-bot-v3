import asyncio
import collections
import datetime as dt
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backtest, market_data, market_hours, store
from .config import cfg
from .engine import engine

# ---------------------------------------------------------------------------
# In-memory log ring buffer — exposed via /api/logs for the console panel
# ---------------------------------------------------------------------------
_log_buffer: collections.deque = collections.deque(maxlen=500)


class _BufHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_buffer.append({
                "ts": int(record.created * 1000),
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
            })
        except Exception:
            pass


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger().addHandler(_BufHandler())


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(engine.run())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def auth(x_token: str = Header(default="")):
    if x_token != cfg.DASHBOARD_TOKEN:
        raise HTTPException(401, "bad token")


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/status", dependencies=[Depends(auth)])
async def status():
    eq = await engine.equity()
    anchor = store.kv_get("day_anchor", {"equity": eq})
    day_pnl = eq - anchor["equity"]
    positions = []
    for p in store.positions():
        px = await market_data.last_price(p["symbol"])
        sgn = -1 if p.get("side", "LONG") == "SHORT" else 1
        positions.append({**p, "price": px,
                          "upnl": (px - p["entry"]) * p["size"] * sgn,
                          "upnl_pct": (px / p["entry"] - 1) * 100 * sgn})
    mh = market_hours.session_status()
    return {
        "mode": engine.mode, "paused": engine.paused,
        "halted": bool(engine.halted_until and time.time() < engine.halted_until),
        "ai_autopilot": engine.ai_autopilot,
        "ai_trader": engine.ai_trader,
        "market_hours_only": engine.market_hours_only,
        "market_open": mh["open"],
        "market_reason": mh["reason"],
        "market_next_open": mh["next_open_et"],
        "equity": eq, "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl / anchor["equity"] * 100 if anchor["equity"] else 0,
        "positions": positions, "signals": engine.last_signals,
        "scores": engine.last_scores,
        "market_signals": engine.market_sigs,
        "risk": engine.risk, "symbols": cfg.SYMBOLS,
        "last_error": engine.last_error,
        "server_time": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


@app.get("/api/klines", dependencies=[Depends(auth)])
async def klines(symbol: str, interval: str = "15m", limit: int = 300):
    return await market_data.klines(symbol, interval, limit)


@app.get("/api/trades", dependencies=[Depends(auth)])
async def trades(limit: int = 100):
    return store.trades(limit)


@app.get("/api/equity", dependencies=[Depends(auth)])
async def equity():
    return store.equity_series()


class Settings(BaseModel):
    paused: bool | None = None
    mode: str | None = None
    risk_per_trade: float | None = None
    max_alloc: float | None = None
    max_positions: int | None = None
    daily_loss_halt: float | None = None
    market_hours_only: bool | None = None
    ai_autopilot: bool | None = None
    ai_trader: bool | None = None


@app.post("/api/settings", dependencies=[Depends(auth)])
async def settings(s: Settings):
    if s.paused is not None:
        engine.set_paused(s.paused)
    if s.mode is not None:
        engine.set_mode(s.mode)
    if s.market_hours_only is not None:
        engine.set_market_hours_only(s.market_hours_only)
    if s.ai_autopilot is not None:
        engine.set_ai_autopilot(s.ai_autopilot)
    if s.ai_trader is not None:
        engine.set_ai_trader(s.ai_trader)
    engine.set_risk(risk_per_trade=s.risk_per_trade, max_alloc=s.max_alloc,
                    max_positions=s.max_positions, daily_loss_halt=s.daily_loss_halt)
    return {"ok": True}


@app.post("/api/flatten", dependencies=[Depends(auth)])
async def flatten():
    """Emergency: close all positions at market."""
    closed = []
    for p in store.positions():
        px = await market_data.last_price(p["symbol"])
        await engine._close(p["symbol"], p, px, "manual_flatten")
        closed.append(p["symbol"])
    return {"closed": closed}


# ---------------------------------------------------------------------------
# NEXUSBOT v2 — signal weights & AI Architect endpoints
# ---------------------------------------------------------------------------

class WeightsPayload(BaseModel):
    weights: dict
    thresholds: dict | None = None


@app.get("/api/weights", dependencies=[Depends(auth)])
async def get_weights():
    return {
        "weights": engine.signal_weights,
        "thresholds": engine.score_thresholds,
        "defaults": cfg.DEFAULT_SIGNAL_WEIGHTS,
    }


@app.post("/api/weights", dependencies=[Depends(auth)])
async def set_weights(payload: WeightsPayload):
    """Update signal weights and optional thresholds at runtime."""
    # Normalise weights so they sum to 1
    total = sum(payload.weights.values()) or 1
    normed = {k: round(v / total, 4) for k, v in payload.weights.items()}
    engine.set_weights(normed, payload.thresholds)
    return {"ok": True, "weights": normed, "thresholds": engine.score_thresholds}


class ArchitectMessage(BaseModel):
    message: str


@app.post("/api/architect", dependencies=[Depends(auth)])
async def architect(req: ArchitectMessage):
    """AI Architect: natural-language strategy editor powered by Claude."""
    if not cfg.ANTHROPIC_API_KEY:
        return {
            "reply": (
                "AI Architect requires an ANTHROPIC_API_KEY in your .env file. "
                "Add it and restart the container."
            ),
            "changes": None,
        }

    try:
        import anthropic  # lazy import — only needed when key is set

        current_weights = engine.signal_weights
        current_thresholds = engine.score_thresholds

        system = (
            "You are an AI trading strategy architect for NEXUSBOT V3.\n"
            "You help the user tune the bot's signal weights and entry thresholds.\n"
            "The bot scores each symbol 0–1 across 7 signals and enters trades when "
            "composite score > buy_threshold (LONG) or < sell_threshold (SHORT).\n\n"
            "Signals: technical (EMA+RSI), volatility (inverse ATR%), fear_greed, "
            "news, reddit, volume_delta, btc_dom.\n\n"
            "CRITICAL TOPIC RESTRICTION:\n"
            "You must ONLY answer questions and perform actions related to trading operations, "
            "strategy tuning, and the financial markets. If the user asks anything unrelated "
            "to trading operations or the market (such as general knowledge, coding help, off-topic "
            "chat, or creative tasks), you must politely but firmly refuse to answer, explaining "
            "that your capabilities are restricted to NEXUSBOT V3's trading operations and market "
            "discussions only. Do NOT output any JSON weights/thresholds block if you are refusing "
            "an off-topic request.\n\n"
            "When the user asks for a valid change, respond in two parts:\n"
            "1. A brief human-readable explanation of what you're changing and why.\n"
            "2. A JSON block (no markdown) with this exact shape:\n"
            '{"weights": {...}, "thresholds": {"buy": float, "sell": float}}\n\n'
            "Only include keys the user wants to change. "
            "Weights will be auto-normalised to sum to 1. "
            "Thresholds must stay between 0.1 and 0.9 with buy > sell.\n\n"
            "By default keep thresholds symmetric around 0.5 — (buy - 0.5) should "
            "equal (0.5 - sell), e.g. buy=0.62/sell=0.38 — so long and short setups "
            "have an equal chance to trigger. Only make them asymmetric if the user "
            "explicitly asks for a directional bias (e.g. 'make it more bullish' or "
            "'favor longs'); say so plainly in your explanation when you do."
        )

        user_msg = (
            f"Current weights: {json.dumps(current_weights)}\n"
            f"Current thresholds: {json.dumps(current_thresholds)}\n\n"
            f"User request: {req.message}"
        )

        client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Extract JSON patch from response
        first_brace = raw.find("{")
        last_brace = raw.rfind("}")
        patch = None
        explanation = raw
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            try:
                patch = json.loads(raw[first_brace:last_brace + 1])
            except Exception:
                pass

            pre_json = raw[:first_brace].strip()
            post_json = raw[last_brace + 1:].strip()
            if pre_json.endswith("```json"):
                pre_json = pre_json[:-7].strip()
            elif pre_json.endswith("```"):
                pre_json = pre_json[:-3].strip()
            if post_json.startswith("```"):
                post_json = post_json[3:].strip()
            explanation = (pre_json + "\n\n" + post_json).strip()

        changes = None
        if patch:
            new_weights = {**current_weights, **patch.get("weights", {})}
            new_thresholds = {**current_thresholds, **patch.get("thresholds", {})}
            engine.set_weights(new_weights, new_thresholds)
            changes = {"weights": engine.signal_weights, "thresholds": engine.score_thresholds}

        return {"reply": explanation or raw, "changes": changes}

    except Exception as e:
        log.exception("architect error")
        return {"reply": f"Error: {e}", "changes": None}


@app.get("/api/logs", dependencies=[Depends(auth)])
async def logs(limit: int = 150):
    return list(_log_buffer)[-limit:]


# ---------------------------------------------------------------------------
# Strategies tab — backtesting
# ---------------------------------------------------------------------------

@app.get("/api/backtest/symbols", dependencies=[Depends(auth)])
async def backtest_symbols():
    """Crypto-major symbols backtestable via free Binance history."""
    return backtest.crypto_symbols()


@app.get("/api/backtest/strategies", dependencies=[Depends(auth)])
async def backtest_strategies():
    return backtest.STRATEGIES


@app.get("/api/backtest/run", dependencies=[Depends(auth)])
async def backtest_run(symbol: str, interval: str = "15m", limit: int = 500):
    return await backtest.run_all(symbol, interval, limit)


app.mount("/static", StaticFiles(directory="app/static"), name="static")
