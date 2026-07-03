import json
import os
import sqlite3
import time
from contextlib import contextmanager

from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY, size REAL, entry REAL, stop REAL, tp REAL,
  opened_at INTEGER, mode TEXT, side TEXT DEFAULT 'LONG'
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, side TEXT,
  price REAL, size REAL, notional REAL, fee REAL, pnl REAL, reason TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  ts INTEGER PRIMARY KEY, value REAL, mode TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


@contextmanager
def db():
    os.makedirs(os.path.dirname(cfg.DB_PATH), exist_ok=True)
    con = sqlite3.connect(cfg.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA)
        # migrate pre-side databases
        cols = [r["name"] for r in con.execute("PRAGMA table_info(positions)")]
        if "side" not in cols:
            con.execute("ALTER TABLE positions ADD COLUMN side TEXT DEFAULT 'LONG'")


def kv_get(key, default=None):
    with db() as con:
        row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def kv_set(key, value):
    with db() as con:
        con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (key, json.dumps(value)))


def positions():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM positions")]


def get_position(symbol):
    with db() as con:
        r = con.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    return dict(r) if r else None


def open_position(symbol, side, size, entry, stop, tp, mode):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO positions(symbol,size,entry,stop,tp,opened_at,mode,side) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (symbol, size, entry, stop, tp, int(time.time()), mode, side))


def update_stop(symbol, stop):
    with db() as con:
        con.execute("UPDATE positions SET stop=? WHERE symbol=?", (stop, symbol))


def close_position(symbol):
    with db() as con:
        con.execute("DELETE FROM positions WHERE symbol=?", (symbol,))


def record_trade(symbol, side, price, size, fee, pnl, reason, mode):
    with db() as con:
        con.execute(
            "INSERT INTO trades(ts,symbol,side,price,size,notional,fee,pnl,reason,mode) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), symbol, side, price, size, price * size, fee, pnl, reason, mode))


def trades(limit=100):
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))]


def snapshot_equity(value, mode):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)",
                    (int(time.time()), value, mode))


def equity_series(limit=2000):
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,))][::-1]


def realized_pnl_since(ts):
    with db() as con:
        r = con.execute("SELECT COALESCE(SUM(pnl),0) p FROM trades WHERE ts>=? AND pnl IS NOT NULL",
                        (ts,)).fetchone()
    return r["p"]
