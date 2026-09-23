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
            ("take_profit_order_id", "TEXT"), ("take_profit_price", "REAL"),
            ("entry_strategy", "TEXT"), ("target1_filled", "INTEGER NOT NULL DEFAULT 0"),
            ("original_qty", "REAL"),
        ):
            try:
                conn.execute(f"ALTER TABLE position_state ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists — this file predates it
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy TEXT,
                entry_price REAL,
                exit_price REAL,
                qty REAL,
                entry_time TEXT,
                exit_time TEXT,
                exit_reason TEXT,
                gross_pnl REAL,
                fees_paid REAL,
                net_pnl REAL
            )
        """)
        try:
            conn.execute("ALTER TABLE trade_history ADD COLUMN slippage REAL")
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
            CREATE TABLE IF NOT EXISTS strategy_params (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_params_by_symbol (
                symbol TEXT NOT NULL,
                key TEXT NOT NULL,
                value REAL NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (symbol, key)
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


# ---------- strategy params (live-adjustable MA lengths / ATR multipliers / macro filter) ----------
# Only the KEYS the user has explicitly overridden from the dashboard
# live here — anything not present falls back to strategies.py's own
# hardcoded default (see strategies.LIVE_PARAM_SPECS). This mirrors
# the "only override what's changed" shape of the rest of this file
# rather than duplicating every default into the DB.
def get_strategy_params():
    with db_conn() as conn:
        rows = conn.execute("SELECT key, value FROM strategy_params").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_strategy_params(params):
    ts = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        for key, value in params.items():
            conn.execute(
                "INSERT INTO strategy_params (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, ts),
            )


def clear_strategy_params():
    with db_conn() as conn:
        conn.execute("DELETE FROM strategy_params")


# ---------- per-symbol strategy param overrides ----------
# Same "only overridden keys live here" shape as the global table
# above, plus a symbol dimension. Resolution order (see
# strategies.resolve_effective_params): code default -> global
# override -> per-symbol override, so a per-symbol value here always
# wins over the global table, which always wins over the hardcoded
# default. Deliberately a SEPARATE table rather than a nullable
# symbol column on strategy_params — no migration of existing global
# rows needed, and "global" vs "per-symbol" stay two distinct,
# unambiguous concepts rather than one column with a magic NULL/''
# meaning "global".
def get_strategy_params_for_symbol(symbol):
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM strategy_params_by_symbol WHERE symbol = ?", (symbol,)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_strategy_params_for_symbol(symbol, params):
    ts = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        for key, value in params.items():
            conn.execute(
                "INSERT INTO strategy_params_by_symbol (symbol, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (symbol, key, value, ts),
            )


def clear_strategy_params_for_symbol(symbol):
    with db_conn() as conn:
        conn.execute("DELETE FROM strategy_params_by_symbol WHERE symbol = ?", (symbol,))


def get_all_strategy_params_by_symbol():
    """Every per-symbol override, grouped by symbol — {symbol: {key:
    value}}. Symbols with no overrides at all are simply absent
    (never an empty-dict entry), so callers can tell 'never
    customized' apart from 'customized then reset' the same way, by
    checking membership — used by the settings page to show which
    symbols have any per-symbol customization at a glance without a
    separate query per symbol."""
    with db_conn() as conn:
        rows = conn.execute("SELECT symbol, key, value FROM strategy_params_by_symbol").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["symbol"], {})[r["key"]] = r["value"]
    return out


# ---------- position state (entry price/time, stop order, peak price for trailing) ----------
def get_position_state(symbol):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT entry_price, stop_order_id, stop_price, entry_time, peak_price, "
            "pending_exit_order_id, pending_exit_cycles, take_profit_order_id, "
            "take_profit_price, entry_strategy, target1_filled, original_qty "
            "FROM position_state WHERE symbol = ?",
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
            "take_profit_order_id": row["take_profit_order_id"],
            "take_profit_price": row["take_profit_price"],
            "entry_strategy": row["entry_strategy"],
            "target1_filled": bool(row["target1_filled"]),
            "original_qty": row["original_qty"],
        }
    return {
        "entry_price": None, "stop_order_id": None, "stop_price": None, "entry_time": None,
        "peak_price": None, "pending_exit_order_id": None, "pending_exit_cycles": 0,
        "take_profit_order_id": None, "take_profit_price": None, "entry_strategy": None,
        "target1_filled": False, "original_qty": None,
    }


def set_position_state(symbol, entry_price=None, stop_order_id=None, stop_price=None,
                        entry_time=None, peak_price=None, pending_exit_order_id=None,
                        pending_exit_cycles=0, take_profit_order_id=None,
                        take_profit_price=None, entry_strategy=None, target1_filled=False,
                        original_qty=None):
    """entry_strategy tags which entry path opened this position
    ("TREND" or "SCALP" — see crypto_trading_bot.py) so the main loop
    knows whether to run the trend-following exit stack (chandelier
    trail, time-decay) or the scalp's fixed take-profit/stop-loss
    reconciliation. take_profit_order_id/take_profit_price exist only
    for scalp positions still in their pre-Target-1 phase — a trend
    position has no take-profit order at all, same as before this
    feature existed, and a scalp position past Target 1 clears these
    back to None (see target1_filled). target1_filled distinguishes a
    SCALP position's two phases: False = full size, Target-1 limit
    sell and full-size stop both resting; True = half size remaining,
    Target 1 already sold, now on a breakeven-adjusted stop trailing
    with the ratcheting chandelier stop (Momentum Ratchet — see
    strategies.chandelier_stop_price). original_qty is the qty bought
    at entry (SCALP only) — needed to compute exactly how much sold
    at Target 1 by subtracting the post-fill qty from it, rather than
    assuming a clean 50/50 split (rounding on a fractional BTC qty
    means the actual halves may not be perfectly equal). This function
    always fully overwrites every field (no partial-patch semantics),
    so every caller must pass its own correct value for these fields
    rather than relying on a default — see the call sites in
    crypto_trading_bot.py for how each is scoped to only ever touch
    its own strategy's positions."""
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO position_state "
            "(symbol, entry_price, stop_order_id, stop_price, entry_time, peak_price, "
            "pending_exit_order_id, pending_exit_cycles, take_profit_order_id, "
            "take_profit_price, entry_strategy, target1_filled, original_qty) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET entry_price = excluded.entry_price, "
            "stop_order_id = excluded.stop_order_id, stop_price = excluded.stop_price, "
            "entry_time = excluded.entry_time, peak_price = excluded.peak_price, "
            "pending_exit_order_id = excluded.pending_exit_order_id, "
            "pending_exit_cycles = excluded.pending_exit_cycles, "
            "take_profit_order_id = excluded.take_profit_order_id, "
            "take_profit_price = excluded.take_profit_price, "
            "entry_strategy = excluded.entry_strategy, "
            "target1_filled = excluded.target1_filled, "
            "original_qty = excluded.original_qty",
            (symbol, entry_price, stop_order_id, stop_price, entry_time, peak_price,
             pending_exit_order_id, pending_exit_cycles, take_profit_order_id,
             take_profit_price, entry_strategy, int(bool(target1_filled)), original_qty),
        )


def clear_position_state(symbol):
    set_position_state(symbol, entry_price=None, stop_order_id=None, stop_price=None,
                        entry_time=None, peak_price=None, pending_exit_order_id=None,
                        pending_exit_cycles=0, take_profit_order_id=None,
                        take_profit_price=None, entry_strategy=None, target1_filled=False,
                        original_qty=None)


# ---------- trade history (fee-adjusted P&L per closed trade/partial) ----------
# Fee estimates (0.25% taker on both entry and exit, per spec) are
# recorded PER ROW — each partial exit (e.g. a scalp's Target 1 fill)
# gets its own row rather than waiting for the whole position to
# close, so a partial's realized P&L is captured accurately rather
# than approximated later from a blended average.
TAKER_FEE_RATE = 0.0025  # 0.25%, matches the spec's "0.25% taker entry and exit"


def log_trade(symbol, strategy, entry_price, exit_price, qty, entry_time, exit_time, exit_reason, slippage=None):
    gross_pnl = (exit_price - entry_price) * qty
    fees_paid = (entry_price + exit_price) * qty * TAKER_FEE_RATE
    net_pnl = gross_pnl - fees_paid
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO trade_history "
            "(symbol, strategy, entry_price, exit_price, qty, entry_time, exit_time, "
            "exit_reason, gross_pnl, fees_paid, net_pnl, slippage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, strategy, entry_price, exit_price, qty, entry_time, exit_time,
             exit_reason, gross_pnl, fees_paid, net_pnl, slippage),
        )
    return {"gross_pnl": gross_pnl, "fees_paid": fees_paid, "net_pnl": net_pnl, "slippage": slippage}


def get_recent_trades(symbol=None, limit=50):
    with db_conn() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trade_history WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_profit_factor(symbol=None, limit=200):
    """gross profit / gross loss over the last `limit` closed trades
    (net-of-fees), the standard profit-factor definition. Returns None
    if there are no losing trades yet (undefined/infinite) or no
    trades at all — callers should treat None as 'not enough data',
    never as 0 or a bad value."""
    trades = get_recent_trades(symbol=symbol, limit=limit)
    if not trades:
        return None
    gross_profit = sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0)
    gross_loss = -sum(t["net_pnl"] for t in trades if t["net_pnl"] < 0)
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def get_avg_slippage(symbol=None, limit=50):
    """Average per-trade slippage over the last `limit` closed trades
    (same symbol/limit scoping as get_recent_trades(), via a subquery
    so LIMIT applies to the row set being averaged, not to the single
    aggregate result row). SQL's AVG() ignores NULLs on its own, so
    trades logged before the slippage column existed (or any exit
    path that still passes slippage=None) are automatically excluded
    rather than pulling the average toward zero. Returns None if there
    are no trades at all, or if every trade in the window has a NULL
    slippage — both read the same to a caller: 'no slippage data to
    report', never a fabricated 0.0."""
    with db_conn() as conn:
        if symbol:
            row = conn.execute(
                "SELECT AVG(slippage) AS avg_slippage FROM ("
                "SELECT slippage FROM trade_history WHERE symbol = ? ORDER BY id DESC LIMIT ?"
                ")",
                (symbol, limit),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT AVG(slippage) AS avg_slippage FROM ("
                "SELECT slippage FROM trade_history ORDER BY id DESC LIMIT ?"
                ")",
                (limit,),
            ).fetchone()
    return row["avg_slippage"] if row and row["avg_slippage"] is not None else None


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
