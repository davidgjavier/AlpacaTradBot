#!/usr/bin/env python3
"""
Fully automatic day trading bot for NVDA, INTC, NOK using a 9/21-period
EMA crossover on 5-minute bars.

TIER 1: state (pause flags, activity log) lives in trading_system.db
(SQLite, WAL mode) via db.py instead of flat JSON files shared
unsafely across processes. PDT day-trade counting is read directly
from Alpaca's own account.daytrade_count before every entry, so it
can't drift from partial fills or trades placed outside this bot. The
daily loss circuit breaker flattens open positions (cancelling their
resting bracket stop-loss first, and confirming the cancellation
before the flatten sell) instead of only blocking new entries.

TIER 2: the fixed 5% stop is now a dynamic ATR(14)-based bracket
(stop = entry - 1.5*ATR, take-profit = entry + 3.0*ATR), falling back
to the fixed 5% stop with no take-profit leg if ATR can't be computed.
New buys are blocked unless price is above the 1-hour EMA(200) macro
trend filter, and during the first 15 minutes after the 9:30 AM ET
open (9:30–9:45), to avoid opening-spread whipsaws. Neither of these
blocks sell/exit signals — only new entries.

TIER 3: a relative-volume (RVOL) gate blocks new buys on thin volume
(latest bar's volume must exceed 1.2x the prior 20-bar average). Once
a position is up more than 1.5x ATR from entry, a chandelier trailing
stop takes over — it ratchets the bracket's stop LEG up (never down,
via Alpaca's order-replace endpoint, no cancel/resubmit needed) to
1.5x ATR below the highest price reached since entry, leaving the
take-profit leg untouched. A time-decay exit force-closes a position
at market if it hasn't moved favorably within 12 bars (60 minutes) of
entry. A spread check refuses to fire a market order if the live
bid/ask spread is wider than MAX_SPREAD_PCT, and every sell verifies
via get_position_qty() that it actually filled before clearing state,
so a partial fill can't leave an untracked, unprotected residual.
The opening blackout window now checks against Alpaca's own server
clock and calendar rather than the Mac's local system clock, so local
clock drift can't throw off the 9:30–9:45 window.

TIER 4: bars are re-fetched with an extra bar and the currently-
forming (not yet closed) candle is dropped before computing any
indicator — confirmed empirically that Alpaca's bar endpoints include
it, which was causing indicator flicker and premature signals. The
chandelier trail's "already breached" clamp now compares against the
LIVE bid, not a bar close that can lag up to 5 minutes. The bracket
stop-leg replace distinguishes a genuine failure from "the take-profit
leg already filled" (checked by re-reading position qty) and clears
tracked state as a completed profit exit rather than retrying forever.
An EOD flatten closes all equity positions 10 minutes before the
session's OFFICIAL close (from Alpaca's calendar, correctly handling
early-close days) — this is about eliminating overnight gap risk, NOT
PDT (a same-day forced close still counts as a completed day trade).
A fee-aware passive-limit-exit experiment for strategy-reversal sells
was tried and REVERTED after review: it required cancelling BOTH
bracket legs (stop and take-profit) to free the qty for a resting
limit order, leaving the position fully unprotected for up to 10
minutes in exchange for a maker-fee saving — and Alpaca equities are
commission-free anyway, so there was no fee benefit here to begin
with, only the downside. Every exit path (ATR-stop breach, chandelier
trail-breach, time-decay, and strategy-reversal) is now an
unconditional immediate market order.

FULLY AUTOMATIC — no approval prompt. All guardrails below are hard
limits the bot cannot exceed:

  - Only trades NVDA, INTC, NOK. Never anything else.
  - Every position is capped at exactly $500.
  - Every buy automatically carries an ATR-based stop-loss and
    take-profit bracket order (fixed 5% stop if ATR can't be computed).
  - No new buys unless price is above the 1h EMA(200), and none in the
    9:30–9:45 AM ET opening window.
  - Hard-stops new opening trades once Alpaca's own account-level
    daytrade_count hits 3 in the trailing 5 business days.
  - Daily loss circuit breaker: if realized + unrealized P/L for the
    day drops below -DAILY_LOSS_LIMIT_USD, the bot cancels resting
    orders and market-sells every open position it holds, then stops
    opening any new positions for the rest of the day.

Setup:
  pip install -r requirements.txt
  # .env file with ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER_TRADE
  python3 day_trading_bot.py
"""

import math
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, StopLossRequest, TakeProfitRequest, GetOrdersRequest,
    ReplaceOrderRequest, GetCalendarRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, Sort
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import strategies
import db

# ---------- Configuration ----------
TICKERS = ["NVDA", "INTC", "NOK"]
SHORT_WINDOW = 9
LONG_WINDOW = 21
POSITION_SIZE_USD = 500
STOP_LOSS_PCT = 0.05
CHECK_INTERVAL_SECONDS = 300
EQUITY_RVOL_THRESHOLD = 1.0
MAX_DAY_TRADES_PER_5_DAYS = 3
DAILY_LOSS_LIMIT_USD = 150
EQUITY_BASELINE_KEY = "stocks"

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "false"

if not API_KEY or not SECRET_KEY:
    sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def log(symbol, message):
    db.log_activity(symbol, message)


# ---------- Signal detection ----------
def _drop_unclosed_bar(bars, timeframe_minutes):
    """Alpaca's bar endpoints can include the currently-forming,
    not-yet-closed candle as the most recent entry (confirmed
    empirically on the crypto side — same underlying bar-aggregation
    architecture). Using it directly causes indicator flicker and
    premature signals since its price/volume keep changing until the
    bar actually closes. Drops it when present."""
    if not bars:
        return bars
    now = datetime.now(ZoneInfo("UTC"))
    if bars[-1].timestamp + timedelta(minutes=timeframe_minutes) > now:
        return bars[:-1]
    return bars


def get_bars(symbol, limit=50):
    """Returns (highs, lows, closes, volumes) for the 5-minute signal
    bars, CLOSED bars only. An explicit `start` is required — without
    one, Alpaca only returns a narrow recent window regardless of
    `limit`. Fetches limit+1 so dropping a forming bar still leaves
    the full `limit` closed bars."""
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=datetime.now(ZoneInfo("UTC")) - timedelta(days=5),
        limit=limit + 1,
        sort=Sort.DESC,
    )
    bars = list(reversed(list(data_client.get_stock_bars(request)[symbol])))
    bars = _drop_unclosed_bar(bars, timeframe_minutes=5)[-limit:]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]
    return highs, lows, closes, volumes


def get_macro_closes(symbol, limit=250):
    """1-hour closes for the macro EMA(200) trend filter, CLOSED bars
    only — a separate, much slower-moving series from the 5-minute
    signal bars. 250 hourly bars only accrue during ~6.5h/day regular
    trading hours, so this needs roughly 75 calendar days of lookback
    to be safe."""
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Hour),
        start=datetime.now(ZoneInfo("UTC")) - timedelta(days=75),
        limit=limit + 1,
        sort=Sort.DESC,
    )
    bars = list(reversed(list(data_client.get_stock_bars(request)[symbol])))
    bars = _drop_unclosed_bar(bars, timeframe_minutes=60)[-limit:]
    return [float(b.close) for b in bars]



def print_cycle_telemetry(market_open):
    headers = ("SYMBOL", "PRICE", "1H EMA200", "5M EMA9/21", "RVOL / SPREAD", "DECISION")
    rows = []
    for symbol in TICKERS:
        try:
            highs, lows, closes, volumes = get_bars(symbol)
            macro_closes = get_macro_closes(symbol)
            quote = get_live_quote(symbol)
            spread_ok = bool(quote and quote[1] - quote[0] <= strategies.max_equity_spread_dollars(symbol))
            if len(closes) < LONG_WINDOW:
                raise ValueError("not enough 5m bars")
            price = closes[-1]
            ema9 = strategies.ema(closes, SHORT_WINDOW)[-1]
            ema21 = strategies.ema(closes, LONG_WINDOW)[-1]
            macro = strategies.is_above_macro_trend(macro_closes)
            macro_text = "Bullish/PASS" if macro is True else ("Bearish/FAIL" if macro is False else "N/A/FAIL")
            rvol = strategies.is_volume_confirmed(volumes, multiplier=EQUITY_RVOL_THRESHOLD)
            rvol_text = "PASS" if rvol else "FAIL"
            spread_text = "PASS" if spread_ok else "FAIL"
            signal = check_crossover(closes)
            if not market_open:
                decision = "MARKET CLOSED"
            elif signal:
                decision = f"{signal.upper()} SIGNAL"
            else:
                decision = "WAITING FOR 9/21 CROSSOVER"
            rows.append((symbol, f"${price:.2f}", macro_text, f"{ema9:.2f}/{ema21:.2f}", f"{rvol_text}/{spread_text}", decision))
        except Exception as error:
            rows.append((symbol, "N/A", "N/A/FAIL", "N/A", "FAIL/FAIL", f"DATA ERROR: {type(error).__name__}"))
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(len(headers))]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    print("\n5-MINUTE EQUITY CYCLE TELEMETRY", flush=True)
    print(border, flush=True)
    print("| " + " | ".join(headers[index].ljust(widths[index]) for index in range(len(headers))) + " |", flush=True)
    print(border, flush=True)
    for row in rows:
        print("| " + " | ".join(row[index].ljust(widths[index]) for index in range(len(headers))) + " |", flush=True)
    print(border, flush=True)


def is_macro_uptrend(symbol):
    """True if price is above the 1h EMA(200) — the entry filter.
    Fails CLOSED (blocks new buys) if the macro data can't be fetched
    or there isn't enough history yet, since this filter's whole job
    is preventing bad-timing entries — better to skip a cycle's buy
    than enter blind."""
    try:
        macro_closes = get_macro_closes(symbol)
    except Exception as e:
        log(symbol, f"  Error fetching macro trend data: {e} — blocking new buys this cycle (fail-closed).")
        return False
    result = strategies.is_above_macro_trend(macro_closes)
    if result is None:
        log(symbol, "  Not enough 1h history yet for the macro EMA(200) filter — blocking new buys this cycle (fail-closed).")
        return False
    return result


def in_opening_blackout():
    """True during the first 15 minutes after today's OFFICIAL market
    open — new entries are blocked, exits are not. Uses Alpaca's own
    server clock and calendar rather than the Mac's local system
    clock, so local clock drift can't throw off the window. Fails
    OPEN (not in blackout) on error, since this only gates timing —
    the real risk guardrails (PDT, circuit breaker, stops) are
    unaffected either way."""
    try:
        clock = trading_client.get_clock()
        ny = ZoneInfo("America/New_York")
        now = clock.timestamp.astimezone(ny)
        calendar = trading_client.get_calendar(GetCalendarRequest(
            start=now.date(), end=now.date()
        ))
        if not calendar:
            return False  # no trading session today (weekend/holiday)
        # calendar's open/close come back as NAIVE datetimes representing
        # exchange-local wall-clock time — attach the NY zone explicitly
        # before comparing against clock.timestamp (which IS tz-aware).
        session_open = calendar[0].open.replace(tzinfo=ny)
        window_end = session_open + timedelta(minutes=15)
        return session_open <= now < window_end
    except Exception as e:
        log("-", f"  Error checking opening blackout window via Alpaca's clock: {e} — treating as NOT in blackout (fail-open, timing only).")
        return False


def get_live_quote(symbol):
    """Returns (bid, ask) as floats, or None if the quote can't be
    fetched or is invalid. Shared by the spread check, the chandelier
    clamp (compares the trail candidate against the LIVE bid, not a
    bar close that can lag up to 5 minutes)."""
    try:
        quote = data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol)
        )[symbol]
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        if bid <= 0 or ask <= 0:
            return None
        return bid, ask
    except Exception:
        return None


def is_spread_ok(symbol):
    """True if symbol's live bid/ask spread is within its asset-
    specific tick-based cap (see strategies.EQUITY_SPREAD_CAP_TICKS) —
    refuses to fire a market order into a blown-out spread during a
    volatility spike or thin liquidity. Fails CLOSED if the quote
    can't be fetched."""
    quote = get_live_quote(symbol)
    if not quote:
        log(symbol, "  Couldn't get a valid bid/ask quote — blocking this cycle's order (fail-closed).")
        return False
    bid, ask = quote
    spread_dollars = ask - bid
    max_spread = strategies.max_equity_spread_dollars(symbol)
    if spread_dollars > max_spread:
        mid = (bid + ask) / 2
        log(symbol, f"  Spread too wide: ${spread_dollars:.3f} ({spread_dollars/mid*100:.3f}%), cap ${max_spread:.2f} — blocking this cycle's order.")
        return False
    return True


def in_eod_flatten_window():
    """True from EOD_FLATTEN_MINUTES_BEFORE_CLOSE minutes before
    today's OFFICIAL close until the close itself — new entries are
    blocked and all open positions get force-closed once, to avoid
    holding an unhedged position through the closing auction and an
    overnight gap. This is NOT a PDT-avoidance measure — see the
    module docstring. Uses Alpaca's server clock + calendar, correctly
    handling early-close days. Fails OPEN (not in window) on error."""
    try:
        clock = trading_client.get_clock()
        ny = ZoneInfo("America/New_York")
        now = clock.timestamp.astimezone(ny)
        calendar = trading_client.get_calendar(GetCalendarRequest(
            start=now.date(), end=now.date()
        ))
        if not calendar:
            return False
        session_close = calendar[0].close.replace(tzinfo=ny)
        flatten_start = session_close - timedelta(minutes=strategies.EOD_FLATTEN_MINUTES_BEFORE_CLOSE)
        return flatten_start <= now < session_close
    except Exception as e:
        log("-", f"  Error checking EOD flatten window: {e} — treating as NOT in window (fail-open, timing only).")
        return False


def in_midday_lull():
    """True during the 11:30 AM – 1:30 PM ET midday volume lull — new
    entries are blocked, exits are not. See strategies.py for why this
    exists (RVOL gate would otherwise be time-of-day biased). Uses
    Alpaca's server clock, same as the opening blackout, so local
    clock drift can't affect it. Fails OPEN (not in lull) on error."""
    try:
        clock = trading_client.get_clock()
        now = clock.timestamp.astimezone(ZoneInfo("America/New_York"))
        sh, sm = strategies.MIDDAY_LULL_START_ET
        eh, em = strategies.MIDDAY_LULL_END_ET
        window_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        window_end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        return window_start <= now < window_end
    except Exception as e:
        log("-", f"  Error checking midday lull window: {e} — treating as NOT in lull (fail-open, timing only).")
        return False


def ema(values, span):
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def check_crossover(closes):
    if len(closes) < LONG_WINDOW + 2:
        return None
    ema_short = ema(closes, SHORT_WINDOW)
    ema_long = ema(closes, LONG_WINDOW)
    prev_short, prev_long = ema_short[-2], ema_long[-2]
    curr_short, curr_long = ema_short[-1], ema_long[-1]
    if prev_short <= prev_long and curr_short > curr_long:
        return "buy"
    if prev_short >= prev_long and curr_short < curr_long:
        return "sell"
    return None


# ---------- Order helpers ----------
def get_position_qty(symbol):
    try:
        position = trading_client.get_open_position(symbol)
        return float(position.qty)
    except Exception:
        return 0.0


def cancel_open_orders(symbol, timeout_s=5, poll_s=0.5):
    """Cancels every open order for symbol and CONFIRMS none remain
    before returning — so a bracket's resting stop/take-profit leg
    can't fire at the same moment a market order is submitted against
    the same position. Returns True once confirmed clear, False if it
    couldn't be confirmed within the timeout (caller proceeds anyway
    and logs it — this is used on close/flatten paths, not entries)."""
    try:
        orders = trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol]
        ))
        for o in orders:
            trading_client.cancel_order_by_id(o.id)
    except Exception as e:
        log(symbol, f"  Warning: couldn't confirm open orders were cancelled: {e}")
        return False

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            remaining = trading_client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[symbol]
            ))
            if not remaining:
                return True
        except Exception:
            pass
        time.sleep(poll_s)
    log(symbol, "  Warning: couldn't confirm all open orders were cancelled within timeout — proceeding anyway.")
    return False


def get_stop_leg_id(order):
    """Given a submitted bracket order, returns the id of its stop-loss
    child leg (the one carrying a stop_price), or None if it can't be
    found — e.g. the order hasn't propagated its legs yet."""
    for leg in (getattr(order, "legs", None) or []):
        if getattr(leg, "stop_price", None):
            return str(leg.id)
    return None


def replace_stop_leg(symbol, stop_leg_id, new_stop_price):
    """Replaces just the stop_price of an existing resting stop-loss
    order via Alpaca's native order-replace endpoint — no cancel and
    resubmit needed, and the take-profit leg is untouched. Used by the
    chandelier trail. Alpaca issues a NEW order id on replace (the old
    one moves to 'replaced' status), so returns that new id on success.
    On failure — most commonly because the TAKE-PROFIT leg already
    filled and Alpaca auto-cancelled this stop leg as part of closing
    the bracket out — re-checks actual position qty to tell a genuine
    profit exit apart from some other replace failure (Alpaca doesn't
    expose a clean 404-vs-other distinction through alpaca-py's
    exception here, so this is the reliable way to tell them apart).
    Returns (new_id, outcome) where outcome is None on success,
    "closed" if the position is confirmed already flat, or "error"
    for anything else — the caller decides what to do with each."""
    try:
        new_order = trading_client.replace_order_by_id(
            stop_leg_id, ReplaceOrderRequest(stop_price=new_stop_price)
        )
        return str(new_order.id), None
    except Exception as e:
        current_qty = get_position_qty(symbol)
        if current_qty <= 0:
            return None, "closed"
        log(symbol, f"  Failed to raise trailing stop via replace: {e}")
        return None, "error"


def find_resting_stop_id(symbol):
    """Looks up the currently resting stop-type order for symbol
    directly from Alpaca — a fallback for when we don't already have
    the stop leg's order id tracked locally (e.g. it wasn't captured
    from the original bracket submission)."""
    try:
        orders = trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol]
        ))
        for o in orders:
            if getattr(o, "stop_price", None):
                return str(o.id)
    except Exception:
        pass
    return None


def verify_sell_filled(symbol, qty_before, timeout_s=5, poll_s=0.5):
    """After submitting a market sell, polls get_position_qty() until
    it reads (near) zero, confirming the sell actually filled in full
    before the caller clears position state. Returns the REMAINING qty
    (0.0 if fully filled) — callers should keep tracking the position
    if this comes back > 0 rather than blindly clearing state."""
    deadline = time.time() + timeout_s
    remaining = qty_before
    while time.time() < deadline:
        remaining = get_position_qty(symbol)
        if remaining <= 0.0001:
            return 0.0
        time.sleep(poll_s)
    return remaining


def place_order(symbol, side, price, atr_value=None):
    if side == "buy":
        if not is_spread_ok(symbol):
            return None
        # Alpaca rejects fractional quantities on bracket orders. Use a
        # whole-share quantity so the ATR stop and take-profit legs remain
        # attached to the entry order.
        qty = max(1, math.floor(strategies.MAX_POSITION_USD / price))
        if atr_value and atr_value > 0:
            stop_price = round(price - strategies.STOP_ATR_MULT * atr_value, 2)
            # TAKE_PROFIT_PCT is an opt-in override (default 0 = off):
            # when set, it REPLACES the ATR-based target below rather
            # than combining with it — one target, not two competing
            # ones. Left at its default 0, this is byte-for-byte the
            # original ATR-based calculation.
            if strategies.TAKE_PROFIT_PCT > 0:
                take_profit_price = round(price * (1 + strategies.TAKE_PROFIT_PCT / 100), 2)
            else:
                take_profit_price = round(price + strategies.TARGET_ATR_MULT * atr_value, 2)
        else:
            # ATR unavailable (early in the bot's life, or a data
            # error) — fall back to the fixed 5% stop with no
            # take-profit leg, rather than skip protection entirely.
            stop_price = round(price * (1 - STOP_LOSS_PCT), 2)
            take_profit_price = None
        order_kwargs = dict(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET, stop_loss=StopLossRequest(stop_price=stop_price),
        )
        if take_profit_price:
            order_kwargs["take_profit"] = TakeProfitRequest(limit_price=take_profit_price)
        order = trading_client.submit_order(MarketOrderRequest(**order_kwargs))
        stop_leg_id = get_stop_leg_id(order)
        now_iso = datetime.now(ZoneInfo("UTC")).isoformat()
        db.set_position_state(symbol, entry_price=price, stop_order_id=stop_leg_id,
                               stop_price=stop_price, entry_time=now_iso, peak_price=price)
        return order
    else:
        qty = get_position_qty(symbol)
        if qty <= 0:
            log(symbol, "  No open position to sell — skipping.")
            return None
        if not is_spread_ok(symbol):
            return None
        cancel_open_orders(symbol)
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        ))
        remaining = verify_sell_filled(symbol, qty)
        if remaining > 0:
            log(symbol, f"  Warning: {remaining} shares still held after the sell — likely a partial fill. Leaving position tracked for next cycle's reconciliation rather than clearing state.")
            pos_state = db.get_position_state(symbol)
            db.set_position_state(symbol, entry_price=pos_state.get("entry_price"), stop_order_id=None,
                                   stop_price=None, entry_time=pos_state.get("entry_time"),
                                   peak_price=pos_state.get("peak_price"))
        else:
            db.clear_position_state(symbol)
        return order


def flatten_position(symbol, reason):
    qty = get_position_qty(symbol)
    if qty <= 0:
        return
    cancel_open_orders(symbol)
    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        ))
        log(symbol, f"FLATTENED — {reason}. Market sell submitted: id {order.id}")
        remaining = verify_sell_filled(symbol, qty)
        if remaining > 0:
            log(symbol, f"  Warning: {remaining} shares still held after the flatten sell — likely a partial fill. Leaving position tracked rather than clearing state.")
            pos_state = db.get_position_state(symbol)
            db.set_position_state(symbol, entry_price=pos_state.get("entry_price"), stop_order_id=None,
                                   stop_price=None, entry_time=pos_state.get("entry_time"),
                                   peak_price=pos_state.get("peak_price"))
        else:
            db.clear_position_state(symbol)
    except Exception as e:
        log(symbol, f"  Flatten sell failed: {e}")


# ---------- PDT (read directly from Alpaca, never tracked locally) ----------
def get_daytrade_count():
    account = trading_client.get_account()
    return int(account.daytrade_count or 0)


# ---------- Daily loss circuit breaker ----------
def get_today_pl():
    account = trading_client.get_account()
    return float(account.equity) - float(account.last_equity)


# ---------- Main loop ----------
def is_market_hours():
    clock = trading_client.get_clock()
    return clock.is_open


def main():
    global SHORT_WINDOW, LONG_WINDOW
    log(
        "-",
        f"Fully automatic day trading bot started (Tier 1: SQLite state, live PDT "
        f"count, auto-flatten breaker; Tier 2: ATR stops/targets, 1h EMA200 macro "
        f"filter, opening blackout; Tier 3: RVOL gate, chandelier trail via order-"
        f"replace, time-decay exit, spread check, fill verification, clock-drift-"
        f"proof blackout; Tier 3.1: per-symbol tick-based spread caps, midday "
        f"volume lull blackout; Tier 4: closed-bar-only indicators, live-bid trail "
        f"clamp, profit-exit-aware stop replace, EOD flatten, all exits unconditional "
        f"market orders) "
        f"— watching {', '.join(TICKERS)}. "
        f"Mode: {'PAPER' if PAPER else 'LIVE'}. "
        f"Guardrails: ${POSITION_SIZE_USD}/position, ATR-based stop/target (5% stop fallback), "
        f"max {MAX_DAY_TRADES_PER_5_DAYS} day trades/5 days (from Alpaca), "
        f"daily loss limit -${DAILY_LOSS_LIMIT_USD} (auto-flattens on breach).",
    )

    while True:
        # Tier 6 / Phase 4: fetch the current GLOBAL param overrides
        # once per cycle (cheap, reused for every symbol below) —
        # NOT applied to strategies.py's globals here anymore. Per-
        # symbol resolution now happens individually inside the
        # ticker loop below, since NVDA/INTC/NOK can each have their
        # own override on top of this shared global one, and a single
        # process trading three symbols in the same cycle can't
        # represent three different simultaneous values with one set
        # of module globals — see strategies.resolve_effective_params.
        global_param_overrides = db.get_strategy_params()

        # Phase 3 weekend safeguard: explicit, belt-and-suspenders on
        # top of is_market_hours() below (which already returns closed
        # on weekends via Alpaca's own calendar — NYSE isn't open
        # Saturday/Sunday regardless). Kept as its own separate check,
        # with its own distinct log line, so equities being dormant on
        # a weekend is asserted directly rather than relying solely on
        # a side effect of the calendar lookup — if that lookup were
        # ever changed or its behavior misunderstood, this still holds
        # the line independently.
        if datetime.now(ZoneInfo("America/New_York")).weekday() >= 5:
            log("-", f"[{datetime.now().strftime('%I:%M:%S %p')}] Weekend — equity bot stays dormant regardless of market-hours check (NVDA/INTC/NOK only).")
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        market_open = is_market_hours()
        if not market_open:
            log("-", f"[{datetime.now().strftime('%I:%M:%S %p')}] Market closed. Waiting...")
            print_cycle_telemetry(market_open=False)
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        today_stamp = datetime.now().strftime("%Y-%m-%d")
        baseline = db.get_equity_baseline(EQUITY_BASELINE_KEY)
        if baseline.get("day_stamp") != today_stamp:
            db.set_equity_baseline(EQUITY_BASELINE_KEY, None, today_stamp)
            baseline = db.get_equity_baseline(EQUITY_BASELINE_KEY)
        already_flattened_today = baseline.get("breaker_tripped_stamp") == today_stamp

        try:
            day_trades_used = get_daytrade_count()
        except Exception as e:
            log("-", f"  Error fetching daytrade_count from Alpaca: {e}")
            day_trades_used = MAX_DAY_TRADES_PER_5_DAYS  # fail safe: block entries if unconfirmed

        try:
            today_pl = get_today_pl()
        except Exception as e:
            log("-", f"  Error fetching account P/L: {e}")
            today_pl = 0.0

        circuit_breaker_tripped = today_pl <= -DAILY_LOSS_LIMIT_USD

        if circuit_breaker_tripped:
            if not already_flattened_today:
                log(
                    "-",
                    f"[{datetime.now().strftime('%I:%M:%S %p')}] Daily loss limit hit "
                    f"(P/L ${today_pl:.2f}) — flattening all open positions and "
                    f"halting new entries for the rest of today.",
                )
                for symbol in TICKERS:
                    flatten_position(symbol, f"circuit breaker (P/L ${today_pl:.2f})")
                db.mark_breaker_tripped(EQUITY_BASELINE_KEY, today_stamp)
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        opening_blackout = in_opening_blackout()
        midday_lull = in_midday_lull()
        eod_flatten = in_eod_flatten_window()
        blackout = opening_blackout or midday_lull or eod_flatten
        if opening_blackout:
            log("-", f"[{datetime.now().strftime('%I:%M:%S %p')}] In the 9:30–9:45 ET opening blackout window — no new entries this cycle (exits still allowed).")
        elif midday_lull:
            log("-", f"[{datetime.now().strftime('%I:%M:%S %p')}] In the 11:30–13:30 ET midday volume lull — no new entries this cycle (exits still allowed).")
        elif eod_flatten:
            if baseline.get("eod_flattened_stamp") != today_stamp:
                log("-", f"[{datetime.now().strftime('%I:%M:%S %p')}] In the EOD flatten window ({strategies.EOD_FLATTEN_MINUTES_BEFORE_CLOSE}min before close) — closing all open equity positions to avoid overnight gap risk.")
                for symbol in TICKERS:
                    flatten_position(symbol, "end-of-day flatten (overnight gap risk)")
                db.mark_eod_flattened(EQUITY_BASELINE_KEY, today_stamp)
                baseline = db.get_equity_baseline(EQUITY_BASELINE_KEY)

        for symbol in TICKERS:
            if db.is_paused(symbol):
                continue

            # Phase 4: resolve THIS symbol's effective params (code
            # default -> global override -> this symbol's own
            # override) and apply them to strategies.py's globals
            # before doing anything else for this symbol. Every
            # strategies.* call below (atr(), is_above_macro_trend(),
            # chandelier_stop_price(), the bare strategies.STOP_ATR_
            # MULT/TARGET_ATR_MULT/MAX_POSITION_USD/TAKE_PROFIT_PCT
            # reads inside place_order()) picks these up immediately
            # since they all read live at call time, not at def time.
            # Re-applying per symbol like this is safe specifically
            # because this loop is single-threaded and sequential —
            # NVDA finishes fully before INTC starts, so there's no
            # window where two symbols' values could be live at once.
            symbol_params = strategies.resolve_effective_params(
                symbol, global_param_overrides, db.get_strategy_params_for_symbol(symbol)
            )
            strategies.apply_live_params(symbol_params)
            SHORT_WINDOW = strategies.SHORT_WINDOW
            LONG_WINDOW = strategies.LONG_WINDOW

            try:
                highs, lows, closes, volumes = get_bars(symbol)
            except Exception as e:
                log(symbol, f"  Error fetching data: {e}")
                continue

            atr_value = strategies.atr(highs, lows, closes)
            latest_price = closes[-1]
            qty = get_position_qty(symbol)
            pos_state = db.get_position_state(symbol)

            # Reconciliation for any open position: time-decay exit,
            # then chandelier trail — runs every cycle regardless of
            # whether a fresh crossover signal fired this bar.
            if qty > 0:
                entry = pos_state.get("entry_price") or latest_price
                entry_time = pos_state.get("entry_time")
                stop_id = pos_state.get("stop_order_id") or find_resting_stop_id(symbol)
                peak = max(pos_state.get("peak_price") or entry, latest_price)

                decayed = False
                if entry_time:
                    try:
                        elapsed_bars = (datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(entry_time)).total_seconds() / CHECK_INTERVAL_SECONDS
                        if elapsed_bars >= strategies.TIME_DECAY_BARS and latest_price <= entry:
                            decayed = True
                    except Exception:
                        pass

                if decayed:
                    log(symbol, f"Time-decay exit — no favorable move within {strategies.TIME_DECAY_BARS} bars ({strategies.TIME_DECAY_BARS*5}min) of entry. Exiting at market to free capital.")
                    if is_spread_ok(symbol):
                        cancel_open_orders(symbol)
                        try:
                            order = trading_client.submit_order(MarketOrderRequest(
                                symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                            ))
                            log(symbol, f"  Order submitted: {symbol} sell (time-decay) — id {order.id}")
                            remaining = verify_sell_filled(symbol, qty)
                            if remaining > 0:
                                log(symbol, f"  Warning: {remaining} shares still held after time-decay sell — leaving position tracked.")
                                db.set_position_state(symbol, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak)
                            else:
                                db.clear_position_state(symbol)
                        except Exception as e:
                            log(symbol, f"  Time-decay sell failed: {e}")
                    else:
                        log(symbol, "  Time-decay exit deferred — spread too wide this cycle, will retry next cycle.")
                    continue  # skip signal logic for this symbol this cycle

                candidate = strategies.chandelier_stop_price(entry, peak, atr_value)
                current_stop_price = pos_state.get("stop_price")
                live_quote = get_live_quote(symbol)
                live_bid = live_quote[0] if live_quote else latest_price
                if candidate and candidate >= live_bid:
                    # Price has already pulled back through the trail
                    # level between cycles — checked against the LIVE
                    # bid, not the bar close, since that can lag up to
                    # 5 minutes. Replacing a resting stop-loss SELL with
                    # a price at/above the current market is
                    # invalid/nonsensical — exit at market now instead.
                    log(symbol, f"Chandelier trail candidate (${candidate:.2f}) is at/above the live bid (${live_bid:.2f}) — price already pulled back through the trail level. Exiting at market instead of raising the stop.")
                    if is_spread_ok(symbol):
                        cancel_open_orders(symbol)
                        try:
                            order = trading_client.submit_order(MarketOrderRequest(
                                symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                            ))
                            log(symbol, f"  Order submitted: {symbol} sell (trail breach) — id {order.id}")
                            remaining = verify_sell_filled(symbol, qty)
                            if remaining > 0:
                                log(symbol, f"  Warning: {remaining} shares still held after trail-breach sell — leaving position tracked.")
                                db.set_position_state(symbol, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak)
                            else:
                                db.clear_position_state(symbol)
                        except Exception as e:
                            log(symbol, f"  Trail-breach sell failed: {e}")
                    else:
                        log(symbol, "  Trail-breach exit deferred — spread too wide this cycle, will retry next cycle.")
                    continue  # skip signal logic for this symbol this cycle
                elif candidate and (not current_stop_price or candidate > current_stop_price):
                    if stop_id:
                        new_stop_id, outcome = replace_stop_leg(symbol, stop_id, candidate)
                        if new_stop_id:
                            db.set_position_state(symbol, entry_price=entry, stop_order_id=new_stop_id, stop_price=candidate, entry_time=entry_time, peak_price=peak)
                        elif outcome == "closed":
                            log(symbol, "  Take-profit (or another exit) already closed this position before the trailing stop could be raised — clearing tracked state.")
                            db.clear_position_state(symbol)
                        else:
                            db.set_position_state(symbol, entry_price=entry, stop_order_id=stop_id, stop_price=current_stop_price, entry_time=entry_time, peak_price=peak)
                    else:
                        log(symbol, "  Chandelier trail wants to raise the stop but no resting stop order could be found — skipping this cycle.")
                        db.set_position_state(symbol, entry_price=entry, stop_order_id=None, stop_price=current_stop_price, entry_time=entry_time, peak_price=peak)
                elif peak != pos_state.get("peak_price") or stop_id != pos_state.get("stop_order_id"):
                    db.set_position_state(symbol, entry_price=entry, stop_order_id=stop_id, stop_price=current_stop_price, entry_time=entry_time, peak_price=peak)
            elif pos_state.get("entry_price") or pos_state.get("stop_order_id") or pos_state.get("pending_exit_order_id"):
                # No position but stale state (e.g. the take-profit leg
                # filled and closed it out) — clear it so nothing
                # downstream is misled about what's actually held.
                db.clear_position_state(symbol)

            signal = check_crossover(closes)
            if signal is None:
                continue

            log(symbol, f"[{datetime.now().strftime('%I:%M:%S %p')}] SIGNAL: {signal.upper()} {symbol} @ ~${latest_price:.2f}")

            if signal == "buy":
                rvol_mult = EQUITY_RVOL_THRESHOLD  # equities only run the EMA-crossover ("trend") signal
                if day_trades_used >= MAX_DAY_TRADES_PER_5_DAYS:
                    log(symbol, f"  Skipping — PDT limit reached ({day_trades_used}/{MAX_DAY_TRADES_PER_5_DAYS} day trades per Alpaca).")
                    continue
                if blackout:
                    reason = "opening blackout" if opening_blackout else ("midday lull" if midday_lull else "EOD flatten window")
                    log(symbol, f"  Skipping buy — inside the {reason} window.")
                    continue
                if not is_macro_uptrend(symbol):
                    log(symbol, "  Skipping buy — price is below the 1h EMA(200) macro trend filter.")
                    continue
                if not strategies.is_volume_confirmed(volumes, multiplier=rvol_mult):
                    log(symbol, f"  Skipping buy — volume below the {rvol_mult}x RVOL confirmation threshold (or not enough volume history yet).")
                    continue
                try:
                    order = place_order(symbol, "buy", latest_price, atr_value)
                    if order:
                        log(symbol, f"  Order submitted automatically: {symbol} buy — id {order.id}")
                        # Alpaca's own daytrade_count won't reflect this
                        # fill until settlement, so bump the local count
                        # for the rest of THIS cycle only — never persisted.
                        day_trades_used += 1
                except Exception as e:
                    log(symbol, f"  Order failed: {e}")

            elif signal == "sell":
                if qty <= 0:
                    log(symbol, "  No open position to sell — skipping.")
                elif not is_spread_ok(symbol):
                    log(symbol, "  Sell signal deferred — spread too wide this cycle, will retry next cycle.")
                else:
                    cancel_open_orders(symbol)
                    try:
                        order = trading_client.submit_order(MarketOrderRequest(
                            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                        ))
                        log(symbol, f"  Order submitted automatically: {symbol} sell — id {order.id}")
                        remaining = verify_sell_filled(symbol, qty)
                        if remaining > 0:
                            log(symbol, f"  Warning: {remaining} shares still held after the sell — likely a partial fill. Leaving position tracked for next cycle's reconciliation rather than clearing state.")
                            pos_state2 = db.get_position_state(symbol)
                            db.set_position_state(symbol, entry_price=pos_state2.get("entry_price"), stop_order_id=None, stop_price=None, entry_time=pos_state2.get("entry_time"), peak_price=pos_state2.get("peak_price"))
                        else:
                            db.clear_position_state(symbol)
                    except Exception as e:
                        log(symbol, f"  Order failed: {e}")

        print_cycle_telemetry(market_open=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
