#!/usr/bin/env python3
"""
Fully automatic crypto trading bot for BTC/USD.

TIER 1: state lives in trading_system.db (SQLite, WAL mode) via
db.py instead of flat JSON files, shared safely with the sibling
processes (day_trading_bot.py, dashboard.py). The stop-loss is a
native GTC stop-limit order resting on Alpaca's own order book
immediately after every buy fill, rather than a 5-minute software
poll — protection holds even if this process crashes or the Mac
sleeps. Each cycle reconciles: confirms the resting stop is actually
open and re-places it if it's missing.

TIER 2: the fixed 5% stop is now a dynamic ATR(14)-based stop
(entry - 1.5*ATR) on the primary EMA9/21 strategy and whichever
"auto" sub-strategy is active. A macro trend filter blocks new BUY
signals unless price is above the EMA(200) computed on 1-hour bars
(exits/sells are never blocked by this — it only gates new entries).
"Auto" mode's regime detector now requires 2 consecutive bars of
agreement before switching strategies, to stop rapid regime flips
from churning fees. Crypto does NOT get a resting take-profit order
in this pass — Alpaca has no native OCO/bracket for crypto, so a
take-profit would need a second resting order manually reconciled
against the stop; take-profit for crypto is still just the strategy's
own sell signal, as before.

TIER 3: a relative-volume (RVOL) gate blocks new buys on thin volume
(latest bar's volume must exceed 1.2x the prior 20-bar average). Once
a position is up more than 1.5x ATR from entry, a chandelier trailing
stop takes over — it ratchets the resting stop up (never down) to
1.5x ATR below the highest price reached since entry, by cancelling
and replacing the resting stop order (no native crypto OCO, so this
is done by hand each cycle). A time-decay exit force-closes a
position at market if it hasn't moved favorably within 12 bars (60
minutes) of entry, to free up capital rather than wait indefinitely.
A spread check refuses to fire a market order if BTC's live bid/ask
spread is wider than MAX_SPREAD_PCT, and every sell verifies via
get_position_qty() that it actually filled before clearing state, so
a partial fill can't leave an untracked, unprotected residual position.

TIER 4: bars are re-fetched with an extra bar and the currently-
forming (not yet closed) candle is dropped before computing any
indicator — confirmed empirically that Alpaca's bar endpoints include
it. The chandelier trail's "already breached" clamp compares against
the LIVE bid rather than a bar close that can lag up to 5 minutes.
RVOL threshold is now per-strategy (1.0x trend/reversion, 1.2x
breakout). A fee-aware passive-limit-exit experiment for strategy-
reversal sells was tried and REVERTED after review: it left the
position fully unprotected for up to 10 minutes (both legs of
protection cancelled to free the qty for a resting limit order) in
exchange for a small maker-fee saving — not a good trade for a
signal that's already telling you the position has turned against
you. Every exit path (ATR-stop breach, chandelier trail-breach,
time-decay, and strategy-reversal) is now an unconditional immediate
market order.

TIER 5: two targeted fixes. First, repairing a MISSING resting stop
now restores the already-tracked stop_price (preserving any
chandelier-trail progress) instead of recomputing from entry + the
CURRENT atr_value — ATR isn't frozen at entry, so a fresh calculation
during a volatility spike could regress an already-trailed stop back
down, or place a wider/worse stop than originally intended. Second, a
gap-escalation safety net: the resting stop-limit's order STATUS can
read as fine (still open) even when price has gapped through both the
stop and the limit leg in one move — a limit sell can never fill
below its own limit price, so it would otherwise sit hung while the
position keeps bleeding. Checked independently of order status every
cycle; if hung, cancels it and dispatches an emergency market sell
immediately, skipping the spread check (a bad fill beats no fill).

Runs independently of the stock bot (day_trading_bot.py) — separate
process, log entries tagged BTC/USD in the shared activity log,
separate guardrails. Crypto trades 24/7 and is NOT subject to the
pattern-day-trader rule.

Guardrails (hard limits):
  - Only trades BTC/USD. Never anything else.
  - Every position capped at exactly $500 notional.
  - ATR-based stop-loss (1.5x ATR14, falls back to 5% if ATR can't be
    computed) placed as a native resting order on Alpaca immediately
    after every buy fill — not dependent on this process staying alive.
  - New buys blocked unless price is above the 1-hour EMA(200).
  - Daily loss circuit breaker: if today's account equity drops $150+
    below the day's starting equity, no new buys, AND any open BTC
    position is flattened immediately (resting stop cancelled, market
    sell submitted) rather than just blocking new entries.

Setup:
  pip install -r requirements.txt
  # uses the same .env as day_trading_bot.py
  python3 crypto_trading_bot.py
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest, Sort
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import strategies
import db

# ---------- Configuration ----------
SYMBOL = "BTC/USD"
SHORT_WINDOW = 9
LONG_WINDOW = 21
POSITION_SIZE_USD = 500
STOP_LOSS_PCT = 0.05
# The resting stop-limit's limit price sits this far below the stop
# price, so the order still has room to actually fill during a fast
# drop instead of just sitting unfilled below the market.
STOP_LIMIT_SLIPPAGE_PCT = 0.005
CHECK_INTERVAL_SECONDS = 300
DAILY_LOSS_LIMIT_USD = 150
EQUITY_BASELINE_KEY = "crypto"
MAX_SPREAD_PCT = strategies.CRYPTO_SPREAD_CAP_PCT  # single source of truth in strategies.py

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "false"

if not API_KEY or not SECRET_KEY:
    sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = CryptoHistoricalDataClient()


def log(message):
    db.log_activity(SYMBOL, message)


# ---------- Signal detection ----------
def _drop_unclosed_bar(bars, timeframe_minutes):
    """Alpaca's bar endpoints can include the currently-forming,
    not-yet-closed candle as the most recent entry (confirmed
    empirically against live BTC/USD 5-min bars: the last bar's volume
    reads a tiny fraction of a normal complete bar's). Using it
    directly causes indicator flicker and premature signals since its
    price/volume keep changing until the bar actually closes. Drops it
    when present."""
    if not bars:
        return bars
    now = datetime.now(timezone.utc)
    if bars[-1].timestamp + timedelta(minutes=timeframe_minutes) > now:
        return bars[:-1]
    return bars


def get_bars(limit=50):
    """Returns (highs, lows, closes, volumes) for the 5-minute signal
    bars, CLOSED bars only. An explicit `start` is required — without
    one, Alpaca only returns a narrow recent window regardless of
    `limit`. Fetches limit+1 so dropping a forming bar still leaves
    the full `limit` closed bars."""
    request = CryptoBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=3),
        limit=limit + 1,
        sort=Sort.DESC,
    )
    bars = list(reversed(list(data_client.get_crypto_bars(request)[SYMBOL])))
    bars = _drop_unclosed_bar(bars, timeframe_minutes=5)[-limit:]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]
    return highs, lows, closes, volumes


def get_macro_closes(limit=250):
    """1-hour closes for the macro EMA(200) trend filter, CLOSED bars
    only — a separate, much slower-moving series from the 5-minute
    signal bars. 250 hourly bars needs roughly 2 weeks of lookback,
    well past the default recent-only window, hence the explicit
    `start`."""
    request = CryptoBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame(1, TimeFrameUnit.Hour),
        start=datetime.now(timezone.utc) - timedelta(days=20),
        limit=limit + 1,
        sort=Sort.DESC,
    )
    bars = list(reversed(list(data_client.get_crypto_bars(request)[SYMBOL])))
    bars = _drop_unclosed_bar(bars, timeframe_minutes=60)[-limit:]
    return [float(b.close) for b in bars]


def is_macro_uptrend():
    """True if price is above the 1h EMA(200) — the entry filter.
    Fails CLOSED (blocks new buys) if the macro data can't be fetched
    or there isn't enough history yet, since this filter's whole job
    is preventing bad-timing entries — better to skip a cycle's buy
    than enter blind."""
    try:
        macro_closes = get_macro_closes()
    except Exception as e:
        log(f"  Error fetching macro trend data: {e} — blocking new buys this cycle (fail-closed).")
        return False
    result = strategies.is_above_macro_trend(macro_closes)
    if result is None:
        log("  Not enough 1h history yet for the macro EMA(200) filter — blocking new buys this cycle (fail-closed).")
        return False
    return result


def get_live_quote():
    """Returns (bid, ask) as floats, or None if the quote can't be
    fetched or is invalid. Shared by the spread check, the chandelier
    clamp (compares the trail candidate against the LIVE bid, not a
    bar close that can lag up to 5 minutes)."""
    try:
        quote = data_client.get_crypto_latest_quote(
            CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOL)
        )[SYMBOL]
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        if bid <= 0 or ask <= 0:
            return None
        return bid, ask
    except Exception:
        return None


def is_spread_ok():
    """True if BTC's live bid/ask spread is within MAX_SPREAD_PCT of
    the mid price — refuses to fire a market order into a blown-out
    spread during a volatility spike or thin liquidity. Fails CLOSED
    (treats the spread as too wide) if the quote can't be fetched,
    since this check exists specifically to avoid bad executions."""
    quote = get_live_quote()
    if not quote:
        log("  Couldn't get a valid bid/ask quote — blocking this cycle's order (fail-closed).")
        return False
    bid, ask = quote
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid
    if spread_pct > MAX_SPREAD_PCT:
        log(f"  Spread too wide: {spread_pct*100:.3f}% (max {MAX_SPREAD_PCT*100:.2f}%) — blocking this cycle's order.")
        return False
    return True


def get_position_qty():
    try:
        position = trading_client.get_open_position(SYMBOL.replace("/", ""))
        return float(position.qty)
    except Exception:
        return 0.0


# ---------- Order helpers ----------
def cancel_order_if_open(order_id):
    if not order_id:
        return
    try:
        trading_client.cancel_order_by_id(order_id)
    except Exception:
        pass  # already filled or cancelled — nothing to do


def stop_order_still_open(order_id):
    if not order_id:
        return False
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status.value in ("new", "accepted", "held", "pending_new")
    except Exception:
        return False


def is_stop_hung(tracked_stop_price):
    """True if a resting stop is nominally 'still open' per its order
    status (which would otherwise read as fine — not cancelled, not
    failed), but the live bid has fallen through the order's LIMIT
    leg far enough that it can no longer realistically fill — e.g. BTC
    gapped through both the stop price and the limit price in one
    move. A stop-LIMIT order (unlike stop-market) can sit open
    indefinitely in this case, since a resting limit sell never fills
    below its own limit price while the position keeps bleeding.
    Checked independently of order status. Returns False (not hung) if
    there's no tracked stop_price or the live quote can't be fetched —
    this check only ever ADDS a safety net, never removes the normal
    reconciliation path."""
    if not tracked_stop_price:
        return False
    quote = get_live_quote()
    if not quote:
        return False
    live_bid, _ = quote
    limit_price = tracked_stop_price * (1 - STOP_LIMIT_SLIPPAGE_PCT)
    return live_bid < limit_price


def get_open_order_count(symbol=SYMBOL):
    """Returns the number of open orders for symbol, or None if the
    count couldn't be confirmed (caller must treat that as unknown,
    never as zero)."""
    try:
        orders = trading_client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol]
        ))
        return len(orders)
    except Exception:
        return None


def cancel_and_confirm(order_id, symbol=SYMBOL, timeout_s=5, poll_s=0.5):
    """Cancels order_id (if any) and polls until Alpaca reports zero
    open orders for symbol, so a still-resting stop can't execute at
    the same moment we submit a market order against the same
    position. Returns True once confirmed clear, False if it couldn't
    be confirmed within the timeout — caller should log that and
    proceed cautiously rather than block indefinitely."""
    cancel_order_if_open(order_id)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        count = get_open_order_count(symbol)
        if count == 0:
            return True
        time.sleep(poll_s)
    return False


def place_protective_stop(qty, entry_price, atr_value=None):
    """Places a native GTC stop-limit sell resting on Alpaca's book —
    fires even if this process is down. Uses an ATR-based stop
    (entry - 1.5*ATR) when atr_value is available, falling back to the
    fixed STOP_LOSS_PCT if it isn't (early in the bot's life before
    there's enough bar history, or if the ATR fetch failed). Returns
    (order_id, stop_price) — order_id is None if the order failed to
    place (caller should treat the position as unprotected until the
    next cycle's reconciliation retries this)."""
    if atr_value and atr_value > 0:
        stop_price = round(entry_price - strategies.STOP_ATR_MULT * atr_value, 2)
    else:
        stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    limit_price = round(stop_price * (1 - STOP_LIMIT_SLIPPAGE_PCT), 2)
    try:
        order = trading_client.submit_order(StopLimitOrderRequest(
            symbol=SYMBOL, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price, limit_price=limit_price,
        ))
        log(f"Protective stop placed: stop ${stop_price:.2f} / limit ${limit_price:.2f} — order id {order.id}")
        return str(order.id), stop_price
    except Exception as e:
        log(f"  Failed to place protective stop: {e} — position is UNPROTECTED until next cycle retries this.")
        return None, stop_price


def replace_protective_stop(qty, new_stop_price):
    """Cancels the current resting stop and places a fresh stop-limit
    at new_stop_price — used by the chandelier trail to ratchet the
    stop up over time. Caller is responsible for cancelling/confirming
    the OLD order first (via cancel_and_confirm) before calling this,
    same as any other stop replacement. Returns (order_id, stop_price)
    like place_protective_stop; order_id is None on failure."""
    limit_price = round(new_stop_price * (1 - STOP_LIMIT_SLIPPAGE_PCT), 2)
    try:
        order = trading_client.submit_order(StopLimitOrderRequest(
            symbol=SYMBOL, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=new_stop_price, limit_price=limit_price,
        ))
        log(f"Trailing stop raised: stop ${new_stop_price:.2f} / limit ${limit_price:.2f} — order id {order.id}")
        return str(order.id), new_stop_price
    except Exception as e:
        log(f"  Failed to raise trailing stop: {e} — the old stop level no longer rests on the book after cancellation; next cycle's reconciliation will re-place a stop.")
        return None, None


def verify_sell_filled(qty_before, timeout_s=5, poll_s=0.5):
    """After submitting a market sell, polls get_position_qty() until
    it reads (near) zero, confirming the sell actually filled in full
    before the caller clears position state. Returns the REMAINING qty
    (0.0 if fully filled) — callers should keep tracking the position
    if this comes back > 0 rather than blindly clearing state, so a
    partial fill can't leave an untracked, unprotected residual."""
    deadline = time.time() + timeout_s
    remaining = qty_before
    while time.time() < deadline:
        remaining = get_position_qty()
        if remaining <= 0.0001:
            return 0.0
        time.sleep(poll_s)
    return remaining


def place_buy():
    return trading_client.submit_order(MarketOrderRequest(
        symbol=SYMBOL, notional=POSITION_SIZE_USD,
        side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
    ))


def place_market_sell(qty):
    return trading_client.submit_order(MarketOrderRequest(
        symbol=SYMBOL, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
    ))


def flatten_position(qty, stop_order_id, reason):
    """Cancels any resting protective stop and CONFIRMS it's actually
    gone before market-selling the full qty — otherwise the resting
    stop could still fill at the same moment as this market sell and
    the order would get rejected (or worse, both would execute).
    Verifies the sell actually filled in full before clearing state —
    a partial fill leaves the residual tracked, not silently dropped.
    Used by the circuit breaker."""
    confirmed = cancel_and_confirm(stop_order_id)
    if not confirmed:
        log("  Warning: couldn't confirm the resting stop was fully cancelled before flattening — proceeding with the market sell anyway since this is an emergency flatten.")
    try:
        order = place_market_sell(qty)
        log(f"FLATTENED — {reason}. Market sell submitted: id {order.id}")
        remaining = verify_sell_filled(qty)
        if remaining > 0:
            log(f"  Warning: {remaining} BTC still shows as held after the flatten sell — likely a partial fill. Re-establishing a protective stop on the residual rather than clearing state.")
            prior_entry = db.get_position_state(SYMBOL).get("entry_price") or 0
            try:
                latest = data_client.get_crypto_latest_quote(
                    CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOL))[SYMBOL]
                current_price = float(latest.ask_price)
            except Exception:
                current_price = prior_entry
            new_stop_id, new_stop_price = place_protective_stop(remaining, current_price)
            db.set_position_state(SYMBOL, entry_price=current_price, stop_order_id=new_stop_id, stop_price=new_stop_price)
        else:
            db.clear_position_state(SYMBOL)
    except Exception as e:
        log(f"  Flatten sell failed: {e}")


# ---------- Daily loss circuit breaker ----------
def reset_day_if_needed():
    today_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    baseline = db.get_equity_baseline(EQUITY_BASELINE_KEY)
    if baseline["day_stamp"] != today_stamp:
        account = trading_client.get_account()
        db.set_equity_baseline(EQUITY_BASELINE_KEY, float(account.equity), today_stamp)
        return {
            "day_start_equity": float(account.equity),
            "day_stamp": today_stamp,
            "breaker_tripped_stamp": None,
        }
    return baseline


def get_today_pl(baseline):
    account = trading_client.get_account()
    start = baseline.get("day_start_equity")
    if start is None:
        return 0.0
    return float(account.equity) - start


# ---------- Main loop ----------
def main():
    log(
        f"Fully automatic crypto bot started (Tier 1: SQLite state, native stop orders; "
        f"Tier 2: ATR stop/1h macro filter/regime hysteresis; Tier 3: RVOL gate, "
        f"chandelier trail, time-decay exit, spread check, fill verification; "
        f"Tier 4: closed-bar-only indicators, live-bid trail clamp, per-strategy RVOL, "
        f"all exits unconditional market orders). "
        f"Mode: {'PAPER' if PAPER else 'LIVE'}. "
        f"Guardrails: ${POSITION_SIZE_USD}/position, ATR-based stop (5% fallback), "
        f"daily loss limit -${DAILY_LOSS_LIMIT_USD} (auto-flattens on breach)."
    )

    while True:
        baseline = reset_day_if_needed()
        today_stamp = baseline["day_stamp"]

        try:
            today_pl = get_today_pl(baseline)
        except Exception as e:
            log(f"  Error fetching account P/L: {e}")
            today_pl = 0.0

        circuit_breaker_tripped = today_pl <= -DAILY_LOSS_LIMIT_USD
        already_flattened_today = baseline.get("breaker_tripped_stamp") == today_stamp

        qty = get_position_qty()
        pos_state = db.get_position_state(SYMBOL)

        if circuit_breaker_tripped:
            if not already_flattened_today:
                log(
                    f"Daily loss limit hit (P/L ${today_pl:.2f}) — flattening BTC "
                    f"position and halting new buys until tomorrow (UTC)."
                )
                if qty > 0:
                    flatten_position(
                        qty, pos_state.get("stop_order_id"),
                        f"circuit breaker (P/L ${today_pl:.2f})",
                    )
                db.mark_breaker_tripped(EQUITY_BASELINE_KEY, today_stamp)
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        try:
            highs, lows, closes, volumes = get_bars()
            latest_price = closes[-1]
        except Exception as e:
            log(f"  Error fetching BTC data: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        atr_value = strategies.atr(highs, lows, closes)

        # Reconciliation: if we hold a position, make sure a protective
        # stop is actually resting on the book. Re-place it if it's
        # missing — but re-verify right here, not just with the qty
        # read at the top of this cycle, since the position may have
        # closed out (e.g. the stop already filled) in the meantime.
        # Also refuse to place a second stop if something is already
        # resting for this symbol, to avoid a duplicate.
        if qty > 0:
            stop_id = pos_state.get("stop_order_id")
            entry = pos_state.get("entry_price") or latest_price
            entry_time = pos_state.get("entry_time")
            peak = max(pos_state.get("peak_price") or entry, latest_price)

            # Gap-escalation safety net: the resting stop-limit's order
            # STATUS can read as perfectly fine ("still open" — not
            # cancelled, not failed) even when price has gapped through
            # both the stop and the limit leg in one move, leaving a
            # limit sell resting at a price the market has already
            # fallen below — it will never fill from there. Checked
            # independently of order status, BEFORE anything else this
            # cycle, since this is an active, ongoing loss with zero
            # working protection. Deliberately skips the spread check
            # that gates every other exit — a bad fill here is still
            # far better than no fill at all while the position bleeds.
            if stop_order_still_open(stop_id) and is_stop_hung(pos_state.get("stop_price")):
                log(f"EMERGENCY: protective stop appears hung — the live bid has fallen through its limit price (price likely gapped through both stop and limit). Cancelling and dispatching an emergency market sell.")
                cancel_order_if_open(stop_id)
                try:
                    order = place_market_sell(qty)
                    log(f"  Emergency market sell submitted: id {order.id}")
                    remaining = verify_sell_filled(qty)
                    if remaining > 0:
                        log(f"  Warning: {remaining} BTC still held after the emergency sell — leaving position tracked for next cycle rather than clearing state.")
                        db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak)
                    else:
                        db.clear_position_state(SYMBOL)
                except Exception as e:
                    log(f"  Emergency market sell failed: {e}")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Time-decay exit: force a market close if this position
            # hasn't moved favorably within TIME_DECAY_BARS bars —
            # frees up capital rather than waiting indefinitely for a
            # stop or a reversal signal that may never come.
            decayed = False
            if entry_time:
                try:
                    elapsed_bars = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_time)).total_seconds() / CHECK_INTERVAL_SECONDS
                    if elapsed_bars >= strategies.TIME_DECAY_BARS and latest_price <= entry:
                        decayed = True
                except Exception:
                    pass
            if decayed:
                log(f"Time-decay exit — no favorable move within {strategies.TIME_DECAY_BARS} bars ({strategies.TIME_DECAY_BARS*5}min) of entry. Exiting at market to free capital.")
                if is_spread_ok():
                    confirmed = cancel_and_confirm(stop_id)
                    if not confirmed:
                        log("  Warning: couldn't confirm stop cancellation before time-decay exit — proceeding anyway.")
                    try:
                        order = place_market_sell(qty)
                        log(f"  Order submitted: BTC sell (time-decay) — id {order.id}")
                        remaining = verify_sell_filled(qty)
                        if remaining > 0:
                            log(f"  Warning: {remaining} BTC still held after time-decay sell — leaving position tracked for next cycle rather than clearing state.")
                            db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak)
                        else:
                            db.clear_position_state(SYMBOL)
                    except Exception as e:
                        log(f"  Time-decay sell failed: {e}")
                else:
                    log("  Time-decay exit deferred — spread too wide this cycle, will retry next cycle.")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            if not stop_order_still_open(stop_id):
                current_qty = get_position_qty()
                open_count = get_open_order_count()
                if current_qty <= 0:
                    db.clear_position_state(SYMBOL)
                elif open_count == 0:
                    # Repairing a MISSING stop should restore the
                    # already-tracked stop_price (which may reflect
                    # chandelier-trail progress already achieved), NOT
                    # recompute fresh from entry + the CURRENT atr_value.
                    # ATR isn't frozen at entry — if volatility has
                    # spiked since, a fresh calculation could both erase
                    # trailing progress AND land wider (worse) than what
                    # was already in place. Only fall back to a fresh
                    # entry+ATR calculation if no stop_price was ever
                    # tracked at all (e.g. the very first placement
                    # attempt failed outright).
                    tracked_stop_price = pos_state.get("stop_price")
                    if tracked_stop_price:
                        new_stop_id, new_stop_price = replace_protective_stop(current_qty, tracked_stop_price)
                    else:
                        new_stop_id, new_stop_price = place_protective_stop(current_qty, entry, atr_value)
                    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_stop_id, stop_price=new_stop_price, entry_time=entry_time, peak_price=peak)
                elif open_count is None:
                    log("  Reconciliation: couldn't confirm open order count — skipping stop replacement this cycle, will retry next.")
                # else: current_qty > 0 and open_count > 0 — something
                # is already resting for this symbol; leave it alone.
            else:
                # A stop IS resting — check whether the chandelier trail
                # has moved above it (only ever ratchets up, never down).
                candidate = strategies.chandelier_stop_price(entry, peak, atr_value)
                current_stop_price = pos_state.get("stop_price")
                live_quote = get_live_quote()
                live_bid = live_quote[0] if live_quote else latest_price
                if candidate and candidate >= live_bid:
                    # Price has already pulled back through the trail
                    # level between cycles (e.g. a fast drop right after
                    # a new peak) — checked against the LIVE bid, not
                    # the bar close, since that can lag up to 5 minutes.
                    # A stop-limit SELL priced at/above the current
                    # market is invalid/nonsensical — exit at market now
                    # instead of attempting to place it.
                    log(f"Chandelier trail candidate (${candidate:.2f}) is at/above the live bid (${live_bid:.2f}) — price already pulled back through the trail level. Exiting at market instead of raising the stop.")
                    if is_spread_ok():
                        confirmed = cancel_and_confirm(stop_id)
                        if not confirmed:
                            log("  Warning: couldn't confirm stop cancellation before this trail-triggered exit — proceeding anyway.")
                        try:
                            order = place_market_sell(qty)
                            log(f"  Order submitted: BTC sell (trail breach) — id {order.id}")
                            remaining = verify_sell_filled(qty)
                            if remaining > 0:
                                log(f"  Warning: {remaining} BTC still held after trail-breach sell — leaving position tracked rather than clearing state.")
                                db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak)
                            else:
                                db.clear_position_state(SYMBOL)
                        except Exception as e:
                            log(f"  Trail-breach sell failed: {e}")
                    else:
                        log("  Trail-breach exit deferred — spread too wide this cycle, will retry next cycle.")
                elif candidate and (not current_stop_price or candidate > current_stop_price):
                    confirmed = cancel_and_confirm(stop_id)
                    if confirmed:
                        new_id, new_price = replace_protective_stop(qty, candidate)
                        if new_id:
                            db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_id, stop_price=new_price, entry_time=entry_time, peak_price=peak)
                        else:
                            db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak)
                    else:
                        log("  Couldn't confirm cancellation before raising the trailing stop — will retry next cycle.")
                elif peak != pos_state.get("peak_price"):
                    # No trail update needed yet, but still persist the
                    # new peak so next cycle's activation check is accurate.
                    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=stop_id, stop_price=current_stop_price, entry_time=entry_time, peak_price=peak)
        elif pos_state.get("entry_price") or pos_state.get("stop_order_id"):
            # No position but stale state (e.g. the resting stop filled
            # since our last check) — clear it so nothing downstream is
            # misled about what's actually held.
            db.clear_position_state(SYMBOL)

        active_strategy = db.get_active_strategy()

        if db.is_paused(SYMBOL):
            log(
                "Manual override active — bot disconnected from BTC/USD, "
                "skipping automated signal check. (Protective stop above still runs.)"
            )
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        # "auto" mode picks its sub-strategy through the hysteresis
        # wrapper (2 consecutive bars of agreement before switching),
        # rather than a fresh detect_regime() read every cycle.
        if active_strategy == "auto":
            regime_state = db.get_regime_state()
            stable_regime, new_regime_state = strategies.detect_regime_stable(closes, regime_state)
            db.set_regime_state(**new_regime_state)
            signal = strategies.STRATEGIES[stable_regime](closes)
            strategy_label = f"auto ({stable_regime})"
            effective_strategy_name = stable_regime
        else:
            signal = strategies.get_signal(closes, active_strategy)
            strategy_label = active_strategy
            effective_strategy_name = active_strategy

        if signal:
            log(f"SIGNAL: {signal.upper()} BTC @ ~${latest_price:.2f} — strategy: {strategy_label}")

            if signal == "buy":
                rvol_mult = strategies.rvol_multiplier_for(effective_strategy_name)
                if qty > 0:
                    log("  Already holding a BTC position — skipping buy.")
                elif not is_macro_uptrend():
                    log("  Skipping buy — price is below the 1h EMA(200) macro trend filter.")
                elif not strategies.is_volume_confirmed(volumes, multiplier=rvol_mult):
                    log(f"  Skipping buy — volume below the {rvol_mult}x RVOL confirmation threshold for '{effective_strategy_name}' (or not enough volume history yet).")
                elif not is_spread_ok():
                    pass  # is_spread_ok() already logs the reason
                else:
                    try:
                        order = place_buy()
                        log(f"  Order submitted automatically: BTC buy — id {order.id}")
                        time.sleep(2)  # let the market order fill before we size the stop
                        filled_qty = get_position_qty()
                        if filled_qty > 0:
                            stop_id, stop_price = place_protective_stop(filled_qty, latest_price, atr_value)
                            now_iso = datetime.now(timezone.utc).isoformat()
                            db.set_position_state(SYMBOL, entry_price=latest_price, stop_order_id=stop_id, stop_price=stop_price, entry_time=now_iso, peak_price=latest_price)
                        else:
                            log("  Buy submitted but no position detected yet — stop will be placed next cycle's reconciliation.")
                    except Exception as e:
                        log(f"  Order failed: {e}")

            elif signal == "sell":
                if qty > 0:
                    if not is_spread_ok():
                        log("  Sell signal deferred — spread too wide this cycle, will retry next cycle.")
                    else:
                        confirmed = cancel_and_confirm(pos_state.get("stop_order_id"))
                        if not confirmed:
                            log("  Warning: couldn't confirm the resting stop was cancelled before selling — proceeding anyway.")
                        try:
                            order = place_market_sell(qty)
                            log(f"  Order submitted automatically: BTC sell — id {order.id}")
                            remaining = verify_sell_filled(qty)
                            if remaining > 0:
                                log(f"  Warning: {remaining} BTC still held after the sell — likely a partial fill. Leaving position tracked for next cycle's reconciliation rather than clearing state.")
                                db.set_position_state(SYMBOL, entry_price=pos_state.get("entry_price"), stop_order_id=None, stop_price=None, entry_time=pos_state.get("entry_time"), peak_price=pos_state.get("peak_price"))
                            else:
                                db.clear_position_state(SYMBOL)
                        except Exception as e:
                            log(f"  Order failed: {e}")
                else:
                    log("  No open BTC position to sell — skipping.")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
