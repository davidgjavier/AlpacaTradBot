#!/usr/bin/env python3
"""
Centralized SQLite state layer for the trading system (Tier 1
refactor). Replaces the old flat-JSON files (bot_control.json,
crypto_position_state.json, strategy_state.json, bot_activity.json,
crypto_bot_activity.json, day_trade_log.json) with a single
trading_system.db, opened in WAL mode so the three separate OS
processes (day_trading_bot, crypto_trading_bot, dashboard) can read
and write concurrently without corrupting each other's state.

Every public function opens and closes its own short-lived
connection — no long-held connections across the 5-minute bot loop,
no explicit file locking needed beyond WAL + a busy_timeout.
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "trading_system.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_conn():
    """Context manager: opens a connection, commits on clean exit,
    rolls back on exception, always closes. Use for every write; safe
    to use for reads too."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS control (
                symbol TEXT PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                active TEXT NOT NULL DEFAULT 'auto'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS position_state (
                symbol TEXT PRIMARY KEY,
                entry_price REAL,
                stop_order_id TEXT,
                stop_price REAL,
                entry_time TEXT,
                peak_price REAL,
                pending_exit_order_id TEXT,
                pending_exit_cycles INTEGER NOT NULL DEFAULT 0
            )
        """)
        for col, coltype in (
            ("stop_price", "REAL"), ("entry_time", "TEXT"), ("peak_price", "REAL"),
            ("pending_exit_order_id", "TEXT"), ("pending_exit_cycles", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                conn.execute(f"ALTER TABLE position_state ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists — this file predates it
        conn.execute("""
            CREATE TABLE IF NOT EXISTS equity_baseline (
                key TEXT PRIMARY KEY,
                day_start_equity REAL,
                day_stamp TEXT,
                breaker_tripped_stamp TEXT,
                eod_flattened_stamp TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE equity_baseline ADD COLUMN eod_flattened_stamp TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists — this file predates it
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT,
                message TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_regime TEXT,
                candidate_regime TEXT,
                candidate_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_symbol_ts
            ON activity_log (symbol, timestamp DESC)
        """)
        conn.execute("INSERT OR IGNORE INTO strategy (id, active) VALUES (1, 'auto')")
        conn.execute("INSERT OR IGNORE INTO regime_state (id, current_regime, candidate_regime, candidate_count) VALUES (1, NULL, NULL, 0)")
        for sym in ("NVDA", "INTC", "NOK", "BTC/USD"):
            conn.execute(
                "INSERT OR IGNORE INTO control (symbol, paused) VALUES (?, 0)", (sym,)
            )


# ---------- control (pause / manual override) ----------
def is_paused(symbol):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT paused FROM control WHERE symbol = ?", (symbol,)
        ).fetchone()
    return bool(row["paused"]) if row else False


def set_paused(symbol, paused):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO control (symbol, paused) VALUES (?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET paused = excluded.paused",
            (symbol, int(paused)),
        )


def get_all_control():
    with db_conn() as conn:
        rows = conn.execute("SELECT symbol, paused FROM control").fetchall()
    return {r["symbol"]: bool(r["paused"]) for r in rows}


# ---------- strategy ----------
def get_active_strategy():
    with db_conn() as conn:
        row = conn.execute("SELECT active FROM strategy WHERE id = 1").fetchone()
    return row["active"] if row else "auto"


def set_active_strategy(name):
    with db_conn() as conn:
        conn.execute("UPDATE strategy SET active = ? WHERE id = 1", (name,))


# ---------- regime hysteresis state (auto-mode strategy switching) ----------
def get_regime_state():
    with db_conn() as conn:
        row = conn.execute(
            "SELECT current_regime, candidate_regime, candidate_count FROM regime_state WHERE id = 1"
        ).fetchone()
    if row:
        return dict(row)
    return {"current_regime": None, "candidate_regime": None, "candidate_count": 0}


def set_regime_state(current_regime, candidate_regime, candidate_count):
    with db_conn() as conn:
        conn.execute(
            "UPDATE regime_state SET current_regime = ?, candidate_regime = ?, candidate_count = ? WHERE id = 1",
            (current_regime, candidate_regime, candidate_count),
        )


# ---------- position state (entry price/time, stop order, peak price for trailing) ----------
def get_position_state(symbol):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT entry_price, stop_order_id, stop_price, entry_time, peak_price, "
            "pending_exit_order_id, pending_exit_cycles FROM position_state WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    if row:
        return {
            "entry_price": row["entry_price"],
            "stop_order_id": row["stop_order_id"],
            "stop_price": row["stop_price"],
            "entry_time": row["entry_time"],
            "peak_price": row["peak_price"],
            "pending_exit_order_id": row["pending_exit_order_id"],
            "pending_exit_cycles": row["pending_exit_cycles"] or 0,
        }
    return {
        "entry_price": None, "stop_order_id": None, "stop_price": None, "entry_time": None,
        "peak_price": None, "pending_exit_order_id": None, "pending_exit_cycles": 0,
    }


def set_position_state(symbol, entry_price=None, stop_order_id=None, stop_price=None,
                        entry_time=None, peak_price=None, pending_exit_order_id=None,
                        pending_exit_cycles=0):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO position_state "
            "(symbol, entry_price, stop_order_id, stop_price, entry_time, peak_price, "
            "pending_exit_order_id, pending_exit_cycles) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET entry_price = excluded.entry_price, "
            "stop_order_id = excluded.stop_order_id, stop_price = excluded.stop_price, "
            "entry_time = excluded.entry_time, peak_price = excluded.peak_price, "
            "pending_exit_order_id = excluded.pending_exit_order_id, "
            "pending_exit_cycles = excluded.pending_exit_cycles",
            (symbol, entry_price, stop_order_id, stop_price, entry_time, peak_price,
             pending_exit_order_id, pending_exit_cycles),
        )


def clear_position_state(symbol):
    set_position_state(symbol, entry_price=None, stop_order_id=None, stop_price=None,
                        entry_time=None, peak_price=None, pending_exit_order_id=None,
                        pending_exit_cycles=0)


# ---------- daily equity baseline / circuit breaker / EOD flatten ----------
def get_equity_baseline(key):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT day_start_equity, day_stamp, breaker_tripped_stamp, eod_flattened_stamp "
            "FROM equity_baseline WHERE key = ?",
            (key,),
        ).fetchone()
    if row:
        return dict(row)
    return {"day_start_equity": None, "day_stamp": None, "breaker_tripped_stamp": None, "eod_flattened_stamp": None}


def set_equity_baseline(key, day_start_equity, day_stamp, breaker_tripped_stamp=None, eod_flattened_stamp=None):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO equity_baseline "
            "(key, day_start_equity, day_stamp, breaker_tripped_stamp, eod_flattened_stamp) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET day_start_equity = excluded.day_start_equity, "
            "day_stamp = excluded.day_stamp, "
            "breaker_tripped_stamp = excluded.breaker_tripped_stamp, "
            "eod_flattened_stamp = excluded.eod_flattened_stamp",
            (key, day_start_equity, day_stamp, breaker_tripped_stamp, eod_flattened_stamp),
        )


def mark_breaker_tripped(key, day_stamp):
    with db_conn() as conn:
        conn.execute(
            "UPDATE equity_baseline SET breaker_tripped_stamp = ? WHERE key = ?",
            (day_stamp, key),
        )


def mark_eod_flattened(key, day_stamp):
    with db_conn() as conn:
        conn.execute(
            "UPDATE equity_baseline SET eod_flattened_stamp = ? WHERE key = ?",
            (day_stamp, key),
        )


# ---------- activity log ----------
def log_activity(symbol, message, print_it=True):
    ts = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (timestamp, symbol, message) VALUES (?, ?, ?)",
            (ts, symbol, message),
        )
        conn.execute("""
            DELETE FROM activity_log WHERE id NOT IN (
                SELECT id FROM activity_log ORDER BY id DESC LIMIT 2000
            )
        """)
    if print_it:
        print(f"[{symbol or '-'}] {message}")


def get_recent_activity(symbol=None, limit=20):
    with db_conn() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT timestamp, symbol, message FROM activity_log "
                "WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT timestamp, symbol, message FROM activity_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


init_db()
