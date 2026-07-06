"""NYSE market-hours gate.

The engine calls ``is_market_open()`` before opening any new position.
Exits (stop-loss, take-profit, trend-exit) are always processed regardless
of session state — the gate only blocks NEW entries.

Symbol classification
─────────────────────
• Pure crypto (BTC, ETH, SOL, …)       → 24 / 7, but WEEKEND entries blocked
  because weekend volume is thin and manipulation risk is higher.
• xStocks / index ETFs / commodities   → NYSE session only (Mon–Fri 09:30–16:00 ET)
  These Pionex synthetic products track instruments that are only tradable
  while the underlying exchange is open.

The caller can override the whole gate at runtime via ``store.kv_get("market_hours_only")``.
"""
import datetime as dt
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")

# NYSE holidays 2024-2027 (observed calendar)
_HOLIDAYS: set[dt.date] = {
    # 2024
    dt.date(2024, 1, 1),   dt.date(2024, 1, 15),  dt.date(2024, 2, 19),
    dt.date(2024, 3, 29),  dt.date(2024, 5, 27),  dt.date(2024, 6, 19),
    dt.date(2024, 7, 4),   dt.date(2024, 9, 2),   dt.date(2024, 11, 28),
    dt.date(2024, 12, 25),
    # 2025
    dt.date(2025, 1, 1),   dt.date(2025, 1, 9),   # Carter mourning
    dt.date(2025, 1, 20),  dt.date(2025, 2, 17),  dt.date(2025, 4, 18),
    dt.date(2025, 5, 26),  dt.date(2025, 6, 19),  dt.date(2025, 7, 4),
    dt.date(2025, 9, 1),   dt.date(2025, 11, 27), dt.date(2025, 12, 25),
    # 2026
    dt.date(2026, 1, 1),   dt.date(2026, 1, 19),  dt.date(2026, 2, 16),
    dt.date(2026, 4, 3),   dt.date(2026, 5, 25),  dt.date(2026, 6, 19),
    dt.date(2026, 7, 3),   # observed Friday
    dt.date(2026, 9, 7),   dt.date(2026, 11, 26), dt.date(2026, 12, 25),
    # 2027
    dt.date(2027, 1, 1),   dt.date(2027, 1, 18),  dt.date(2027, 2, 15),
    dt.date(2027, 3, 26),  dt.date(2027, 5, 31),  dt.date(2027, 6, 18),
    dt.date(2027, 7, 5),   dt.date(2027, 9, 6),   dt.date(2027, 11, 25),
    dt.date(2027, 12, 24),
}

# NYSE regular session window (Eastern Time)
_OPEN  = dt.time(9, 30)
_CLOSE = dt.time(16, 0)

# Symbols that are purely crypto and can trade outside NYSE session
# (but still respect weekend gate)
_CRYPTO_PREFIXES = ("BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE",
                    "AVAX", "MATIC", "DOT", "LINK", "LTC", "UNI", "ATOM")


def _is_nyse_holiday(d: dt.date) -> bool:
    return d in _HOLIDAYS


def _in_nyse_session(now_et: dt.datetime) -> bool:
    """True when inside NYSE regular-session window on a business day."""
    if now_et.weekday() >= 5:
        return False
    if _is_nyse_holiday(now_et.date()):
        return False
    return _OPEN <= now_et.time() < _CLOSE


def _is_crypto(symbol: str) -> bool:
    base = symbol.split("_")[0].upper()
    return any(base.startswith(p) for p in _CRYPTO_PREFIXES)


def is_market_open(symbol: str | None = None,
                   now: dt.datetime | None = None) -> bool:
    """Return True if new entries are allowed for *symbol* right now.

    Rules:
    • xStocks / ETFs / commodities → NYSE session only
    • Pure crypto                  → always True on weekdays;
                                     False on weekends and NYSE holidays
    • symbol=None                  → conservative: NYSE session check
    """
    if now is None:
        now_et = dt.datetime.now(ET)
    else:
        now_et = now.astimezone(ET)

    weekend   = now_et.weekday() >= 5
    holiday   = _is_nyse_holiday(now_et.date())
    in_session = _in_nyse_session(now_et)

    if symbol and _is_crypto(symbol):
        # Crypto: block weekends and holidays, allow any hour on weekdays
        return not weekend and not holiday
    else:
        # xStocks / commodities / ETFs / unknown → NYSE session only
        return in_session


def next_open(now: dt.datetime | None = None, symbol: str | None = None) -> dt.datetime:
    """Return the next session open.

    Crypto reopens at 00:00 ET on the next non-weekend, non-holiday day
    (it trades any hour on a valid day). xStocks/ETFs/commodities reopen
    at the next NYSE regular-session open (9:30 ET on a business day).
    """
    if now is None:
        now_et = dt.datetime.now(ET)
    else:
        now_et = now.astimezone(ET)

    if symbol and _is_crypto(symbol):
        candidate_date = now_et.date() + dt.timedelta(days=1)
        while candidate_date.weekday() >= 5 or _is_nyse_holiday(candidate_date):
            candidate_date += dt.timedelta(days=1)
        return dt.datetime.combine(candidate_date, dt.time(0, 0), tzinfo=ET)

    candidate = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et >= candidate:
        candidate += dt.timedelta(days=1)

    while candidate.weekday() >= 5 or _is_nyse_holiday(candidate.date()):
        candidate += dt.timedelta(days=1)

    return candidate


def session_status(symbol: str | None = None,
                   now: dt.datetime | None = None) -> dict:
    """Human-readable status dict for the API / dashboard."""
    if now is None:
        now_et = dt.datetime.now(ET)
    else:
        now_et = now.astimezone(ET)

    open_ = is_market_open(symbol, now)
    nxt   = next_open(now, symbol) if not open_ else None
    return {
        "open": open_,
        "reason": (
            "weekend"  if now_et.weekday() >= 5 else
            "holiday"  if _is_nyse_holiday(now_et.date()) else
            "after_hours" if not _in_nyse_session(now_et) and (symbol is None or not _is_crypto(symbol)) else
            "open"
        ),
        "next_open_et": nxt.strftime("%Y-%m-%d %H:%M ET") if nxt else None,
        "next_open_in_hours": round((nxt - now_et).total_seconds() / 3600, 4) if nxt else None,
    }
