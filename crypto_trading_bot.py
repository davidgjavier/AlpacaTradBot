#!/usr/bin/env python3
"""
Fully automatic crypto trading bot for BTC/USD.

TIER 1: state lives in trading_system.db (SQLite, WAL mode) via
db.py instead of flat JSON files, shared safely with the sibling
processes (day_trading_bot.py, dashboard.py). The stop-loss is a
native GTC stop-limit order resting on Alpaca's own order book
immediately after every buy fill, rather than a 3-minute software
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
(latest bar's volume must exceed a per-strategy multiple of the prior
RVOL_PERIOD-bar average — see Tier 4). Once
a position is up more than 1.5x ATR from entry, a chandelier trailing
stop takes over — it ratchets the resting stop up (never down) to
1.5x ATR below the highest price reached since entry, by cancelling
and replacing the resting stop order (no native crypto OCO, so this
is done by hand each cycle). A time-decay exit force-closes a
position at market if it hasn't moved favorably within TIME_DECAY_BARS
bars (60 minutes) of entry, to free up capital rather than wait indefinitely.
A spread check refuses to fire a market order if BTC's live bid/ask
spread is wider than MAX_SPREAD_PCT, and every sell verifies via
get_position_qty() that it actually filled before clearing state, so
a partial fill can't leave an untracked, unprotected residual position.

TIER 4: bars are re-fetched with an extra bar and the currently-
forming (not yet closed) candle is dropped before computing any
indicator — confirmed empirically that Alpaca's bar endpoints include
it. The chandelier trail's "already breached" clamp compares against
the LIVE bid rather than a bar close that can lag up to 5 minutes.
RVOL threshold is now per-strategy (1.0x trend/reversion, 0.8x
breakout — breakout was originally the strictest at 1.2x, but was
found too restrictive in practice and lowered). A fee-aware passive-limit-exit experiment for strategy-
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

import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest, Sort
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import strategies
import db

ACTIVITY_JSON_PATH = os.path.join(os.path.dirname(__file__), "crypto_bot_activity.json")
MAX_ACTIVITY_EVENTS = 500
ACTIVITY_TIMEZONE = ZoneInfo("America/Los_Angeles")

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
CHECK_INTERVAL_SECONDS = 180
DAILY_LOSS_LIMIT_USD = 150
EQUITY_BASELINE_KEY = "crypto"
MAX_SPREAD_PCT = strategies.CRYPTO_SPREAD_CAP_PCT  # single source of truth in strategies.py
CRYPTO_ESTIMATED_FEE_RATE = float(os.environ.get("CRYPTO_ESTIMATED_FEE_RATE", "0.0025"))
CRYPTO_EXIT_SLIPPAGE_BUFFER = float(os.environ.get("CRYPTO_EXIT_SLIPPAGE_BUFFER", "0.001"))

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "false"

# ---------- Phase 3: dedicated live-trading toggle (BTC only) ----------
# Deliberately SEPARATE from ALPACA_PAPER_TRADE above, which still
# governs the equity bot and this bot's fallback. LIVE_TRADING_ENABLED
# defaults OFF: unset, empty, or anything other than exactly "true"
# leaves this bot on the existing paper behavior, unchanged. Turning
# it on requires ALPACA_LIVE_KEY and ALPACA_LIVE_SECRET to BOTH be
# present — if the switch is on but credentials are missing, this
# refuses to start rather than silently guessing (falling back to
# paper would hide that the switch didn't actually do anything;
# falling back to some default credential would be worse). paper=False
# on Alpaca's SDK routes to https://api.alpaca.markets automatically —
# no separate URL to configure, same idiom this file already uses for
# paper.
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
if LIVE_TRADING_ENABLED:
    LIVE_API_KEY = os.environ.get("ALPACA_LIVE_KEY")
    LIVE_SECRET_KEY = os.environ.get("ALPACA_LIVE_SECRET")
    if not (LIVE_API_KEY and LIVE_SECRET_KEY):
        sys.exit(
            "LIVE_TRADING_ENABLED=true but ALPACA_LIVE_KEY / ALPACA_LIVE_SECRET "
            "are not both set in .env — refusing to start rather than guess "
            "which account to trade real money on."
        )
    API_KEY, SECRET_KEY, PAPER = LIVE_API_KEY, LIVE_SECRET_KEY, False

if not API_KEY or not SECRET_KEY:
    sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = CryptoHistoricalDataClient()


def log(message):
    timestamp = datetime.now(ACTIVITY_TIMEZONE).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    db.log_activity(SYMBOL, message)
    try:
        try:
            with open(ACTIVITY_JSON_PATH, "r", encoding="utf-8") as activity_file:
                events = json.load(activity_file)
            if not isinstance(events, list):
                events = []
        except (FileNotFoundError, json.JSONDecodeError):
            events = []
        events.append({"timestamp": timestamp, "message": message})
        events = events[-MAX_ACTIVITY_EVENTS:]
        directory = os.path.dirname(ACTIVITY_JSON_PATH)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as temp_file:
            json.dump(events, temp_file, indent=2)
            temp_file.write("\n")
            temp_path = temp_file.name
        os.replace(temp_path, ACTIVITY_JSON_PATH)
    except OSError:
        pass


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
    """Returns (highs, lows, closes, volumes) for the 3-minute signal
    bars, CLOSED bars only. An explicit `start` is required — without
    one, Alpaca only returns a narrow recent window regardless of
    `limit`. Fetches limit+1 so dropping a forming bar still leaves
    the full `limit` closed bars."""
    request = CryptoBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame(3, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=3),
        limit=limit + 1,
        sort=Sort.DESC,
    )
    bars = list(reversed(list(data_client.get_crypto_bars(request)[SYMBOL])))
    bars = _drop_unclosed_bar(bars, timeframe_minutes=3)[-limit:]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]
    return highs, lows, closes, volumes


def get_macro_closes(limit=250):
    """1-hour closes for the macro EMA(200) trend filter, CLOSED bars
    only — a separate, much slower-moving series from the 3-minute
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


def print_cycle_telemetry(closes, volumes, strategy_label, signal, strategy_name):
    try:
        price = closes[-1]
        ema9 = strategies.ema(closes, strategies.SHORT_WINDOW)[-1]
        ema21 = strategies.ema(closes, strategies.LONG_WINDOW)[-1]
        macro = strategies.is_above_macro_trend(get_macro_closes())
        rvol = strategies.is_volume_confirmed(
            volumes, multiplier=strategies.rvol_multiplier_for(strategy_name)
        )
        spread = is_spread_ok()
        # Numeric companions to the PASS/FAIL gates above — same inputs,
        # just the raw ratio/percentage instead of the boolean each gate
        # collapses to. Purely additive: appended as NEW columns after
        # the existing ones, so troubleshoot_bridge.py's regex parser
        # (which only anchors on the first five columns) keeps matching
        # unchanged, and neither of these numbers feeds back into any
        # actual entry/exit decision.
        rvol_num = strategies.rvol_ratio(volumes)
        macro_pct = strategies.macro_trend_distance_pct(get_macro_closes())
        macro_text = "Bullish/PASS" if macro is True else ("Bearish/FAIL" if macro is False else "N/A/FAIL")
        decision = f"{signal.upper()} SIGNAL" if signal else "WAITING FOR ENTRY SIGNAL"
        rows = [(SYMBOL, f"${price:.2f}", macro_text, f"{ema9:.2f}/{ema21:.2f}",
                 f"{'PASS' if rvol else 'FAIL'}/{'PASS' if spread else 'FAIL'}",
                 f"{decision} ({strategy_label})",
                 f"{rvol_num:.2f}x" if rvol_num is not None else "N/A",
                 f"{macro_pct:+.2f}%" if macro_pct is not None else "N/A")]
    except Exception as error:
        rows = [(SYMBOL, "N/A", "N/A/FAIL", "N/A", "FAIL/FAIL",
                 f"DATA ERROR: {type(error).__name__}", "N/A", "N/A")]

    headers = ("SYMBOL", "PRICE", "1H EMA200", "3M EMA9/21", "RVOL / SPREAD", "DECISION", "RVOL", "EMA200 DIST")
    widths = [max(len(headers[index]), len(rows[0][index])) for index in range(len(headers))]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    print("\n3-MINUTE CRYPTO CYCLE TELEMETRY", flush=True)
    print(border, flush=True)
    print("| " + " | ".join(headers[index].ljust(widths[index]) for index in range(len(headers))) + " |", flush=True)
    print(border, flush=True)
    print("| " + " | ".join(rows[0][index].ljust(widths[index]) for index in range(len(headers))) + " |", flush=True)
    print(border, flush=True)


def is_profit_after_costs(entry_price):
    """Allow only normal strategy exits that clear estimated round-trip costs.

    Stop, time-decay, trail-breach, and circuit-breaker exits intentionally
    bypass this check because risk reduction takes priority over fees.
    """
    quote = get_live_quote()
    if not quote or entry_price <= 0:
        log("  Couldn't verify net profitability — deferring normal sell (fail-closed).")
        return False
    bid, _ = quote
    gross_return = (bid - entry_price) / entry_price
    required_return = (2 * CRYPTO_ESTIMATED_FEE_RATE) + CRYPTO_EXIT_SLIPPAGE_BUFFER
    if gross_return <= required_return:
        log(
            f"  Normal sell deferred — estimated gross return {gross_return*100:.3f}% "
            f"does not clear estimated fees/slippage {required_return*100:.3f}%."
        )
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


# Alpaca statuses in which an order can still execute and still reserves
# quantity. Previously partially_filled / pending_cancel / pending_replace were
# missing, so a still-working order read as "not open" (audit P0-1).
ORDER_OPEN_STATUSES = (
    "new", "accepted", "held", "pending_new", "partially_filled",
    "pending_cancel", "pending_replace", "accepted_for_bidding", "calculated",
)


def stop_order_still_open(order_id):
    """True if order_id can still execute. NOTE: a lookup error still returns
    False here (unchanged behaviour, tracked separately as audit P0-2). Scalp
    Target-1 logic does NOT use this function; it uses get_order_fill_state()."""
    if not order_id:
        return False
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status.value in ORDER_OPEN_STATUSES
    except Exception:
        return False


def _is_not_found(e):
    """True only for a STRUCTURED broker 'does not exist' answer: HTTP 404 with
    Alpaca error code 40410000 (alpaca.common.exceptions.APIError exposes
    .status_code and .code). Free text is not evidence: a proxy or client error
    whose message happens to say "not found" is UNKNOWN, never a confirmed zero.
    Assumption (to verify in paper contract tests): Alpaca returns 404/40410000
    for "position does not exist" and for an unknown client_order_id."""
    status = getattr(e, "status_code", None)
    try:
        code = getattr(e, "code", None)
    except Exception:          # APIError.code json-parses the body; malformed -> unknown
        return False
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        return False
    return status == 404 and code == 40410000


def _order_lookup(ref):
    """ref is a broker order id, or 'cid:<client_order_id>' for an order whose
    submission response may never have arrived."""
    if isinstance(ref, str) and ref.startswith("cid:"):
        return trading_client.get_order_by_client_id(ref[4:])
    return trading_client.get_order_by_id(ref)


def get_order_fill_state(order_id):
    """Explicit order outcome — the ONLY basis for scalp Target-1 decisions.

    state: NONE      no order reference recorded
           OPEN      can still execute (may be partially filled)
           FILLED    terminal, fully filled
           TERMINAL  canceled / expired / rejected (filled_qty may be > 0)
           NOT_FOUND broker positively says no such order (404)
           UNKNOWN   lookup failed, unrecognised status, or unparseable fill qty
    filled_qty is the broker's CUMULATIVE filled quantity. "Not open" is never
    interpreted as "filled": only filled_qty > 0 counts as a sale.
    avg_price is None unless the broker reported a finite, positive
    filled_avg_price — quantity evidence and price evidence are separate."""
    if not order_id:
        return {"state": "NONE", "status": None, "qty": None, "filled_qty": 0.0, "avg_price": None}
    try:
        order = _order_lookup(order_id)
        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        filled = float(order.filled_qty or 0)
        qty = float(order.qty) if getattr(order, "qty", None) not in (None, "") else None
        avg_raw = getattr(order, "filled_avg_price", None)
        oid = str(getattr(order, "id", "") or "") or None
    except Exception as e:
        return {"state": "NOT_FOUND" if _is_not_found(e) else "UNKNOWN", "status": None, "qty": None,
                "filled_qty": None, "avg_price": None, "error": str(e)[:200]}
    try:
        avg = float(avg_raw) if avg_raw not in (None, "") else None
    except (TypeError, ValueError):
        avg = None
    if avg is not None and (not math.isfinite(avg) or avg <= 0):
        avg = None
    if not math.isfinite(filled) or filled < 0:
        return {"state": "UNKNOWN", "status": status, "qty": qty, "filled_qty": None, "avg_price": None, "id": oid}
    if status in ORDER_OPEN_STATUSES:
        state = "OPEN"
    elif status == "filled":
        state = "FILLED"
    elif status in ("canceled", "expired", "rejected"):
        state = "TERMINAL"
    else:
        state = "UNKNOWN"
    return {"state": state, "status": status, "qty": qty, "filled_qty": filled, "avg_price": avg, "id": oid}


def _position_qty_strict():
    """Broker position qty; 0.0 only on a positive 'position does not exist';
    None when the broker's answer is unknown (NOT flat — see audit P0-2)."""
    try:
        return float(trading_client.get_open_position(SYMBOL.replace("/", "")).qty)
    except Exception as e:
        return 0.0 if _is_not_found(e) else None


def _open_sell_reserved_strict():
    """BTC still reserved by OPEN sell orders (qty - cumulative filled), or None if unknown."""
    try:
        orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL]))
        total = 0.0
        for o in orders:
            side = o.side.value if hasattr(o.side, "value") else str(o.side)
            if side == "sell":
                total += max(0.0, float(o.qty or 0) - float(o.filled_qty or 0))
        return total
    except Exception:
        return None


def _floor_qty(q, dp=6):
    """Round a BTC quantity DOWN so a sell/stop can never exceed what is held."""
    f = 10 ** dp
    return math.floor(float(q) * f + 1e-9) / f


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


def _submit_stop_limit_sell(qty, stop_price):
    """Submits the actual resting stop-limit sell order at stop_price
    and returns (order_id, stop_price) — order_id is None on failure.
    Extracted out of place_protective_stop() below so both it (Path A/
    TREND, ATR-based via STOP_ATR_MULT) and the scalp entry path
    (Path B, a fixed 1.2x ATR per its own spec — see SCALP_STOP_ATR_
    MULT in strategies.py) submit through one order-placement
    implementation rather than duplicating it."""
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
    return _submit_stop_limit_sell(qty, stop_price)


def place_take_profit_limit(qty, tp_price):
    """Places a resting GTC limit sell at tp_price — the take-profit
    leg of a SYNTHETIC bracket for the Mean-Reversion Scalp strategy.
    Alpaca crypto orders only support OrderClass.SIMPLE — bracket/OCO/
    OTO order classes are equity-only (confirmed against Alpaca's own
    enum reference and community reports of bracket submission
    silently failing for crypto pairs) — so this is a second,
    independent resting order, NOT a real bracket. The main loop's
    reconciliation (see the SCALP branch of the qty>0 block) is
    responsible for detecting whichever of the stop-limit or this
    take-profit order fills first and cancelling the other, since
    Alpaca will not do that automatically for crypto the way it does
    for equity bracket orders. Returns the order id, or None on
    failure (caller should treat the position as missing its
    take-profit protection until next cycle's reconciliation retries
    this — mirrors place_protective_stop()'s own failure handling)."""
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=SYMBOL, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, limit_price=tp_price,
        ))
        log(f"[STRATEGY: SCALP] Take-profit limit placed: ${tp_price:.2f} — order id {order.id}")
        return str(order.id)
    except Exception as e:
        log(f"[STRATEGY: SCALP]   Failed to place take-profit limit: {e} — position is missing its take-profit leg until next cycle's reconciliation retries this.")
        return None


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
    # Stage 3 (P0-2 remainder): an UNREADABLE position is not a fill. Only a
    # confirmed read counts; if no read confirms anything, return None
    # ("exit unconfirmed") so callers keep state and log no trade.
    # Stage 5: ONLY the LATEST read counts. A successful read followed by a failed
    # read is UNKNOWN (None), never the earlier (stale) quantity -- a stale residual
    # must not size a protective stop or another exit order.
    deadline = time.time() + timeout_s
    remaining = None
    while time.time() < deadline:
        q = _position_qty_strict()
        if q is not None and q <= 0.0001:
            return 0.0
        remaining = q                       # None when this read failed: invalidates earlier reads
        time.sleep(poll_s)
    return remaining


def get_actual_fill_price(order_id, fallback_price):
    """Queries Alpaca for an order's actual filled_avg_price — used to
    compute REAL slippage (vs. the price the strategy intended to exit
    at) rather than trivially reporting zero by comparing an estimate
    against itself. Deliberately never raises: trade-history logging
    and slippage tracking must never be able to interfere with the
    actual exit already in progress by the time this is called, so any
    failure here just falls back to the caller's own estimate (the
    logged trade still gets an exit_price either way — this only
    affects whether slippage is a real number or None)."""
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.filled_avg_price is not None:
            return float(order.filled_avg_price)
    except Exception as e:
        log(f"  Couldn't fetch actual fill price for order {order_id} (using estimate instead): {e}")
    return fallback_price


def place_buy():
    return trading_client.submit_order(MarketOrderRequest(
        symbol=SYMBOL, notional=strategies.MAX_POSITION_USD,
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
    Used by the circuit breaker.

    Also cancels a resting take-profit order if one exists — a SCALP
    position has one, a TREND position never does (cancel_order_if_open
    is a no-op on None), so this one function correctly flattens
    either kind without the caller needing to know which it's dealing
    with. entry_strategy is read fresh from position_state and
    preserved into the partial-fill remnant-tracking branch so a
    flattened-but-not-fully-filled SCALP position doesn't silently get
    relabeled TREND."""
    pos_state_at_start = db.get_position_state(SYMBOL)
    take_profit_order_id = pos_state_at_start.get("take_profit_order_id")
    entry_strategy = pos_state_at_start.get("entry_strategy") or "TREND"
    confirmed = cancel_and_confirm(stop_order_id)
    if take_profit_order_id:
        cancel_order_if_open(take_profit_order_id)
    if not confirmed:
        log("  Warning: couldn't confirm the resting stop was fully cancelled before flattening — proceeding with the market sell anyway since this is an emergency flatten.")
    try:
        order = place_market_sell(qty)
        log(f"FLATTENED — {reason}. Market sell submitted: id {order.id}")
        remaining = verify_sell_filled(qty)
        if remaining is None:
            log("  EXIT UNCONFIRMED — the sell was submitted but no position read confirmed the "
                "outcome. State preserved, no trade logged; next cycle re-checks.")
            return "UNCONFIRMED"
        elif remaining > 0:
            log(f"  Warning: {remaining} BTC still shows as held after the flatten sell — likely a partial fill. Re-establishing a protective stop on the residual rather than clearing state.")
            prior_entry = db.get_position_state(SYMBOL).get("entry_price") or 0
            try:
                latest = data_client.get_crypto_latest_quote(
                    CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOL))[SYMBOL]
                current_price = float(latest.ask_price)
            except Exception:
                current_price = prior_entry
            new_stop_id, new_stop_price = place_protective_stop(remaining, current_price)
            db.set_position_state(SYMBOL, entry_price=current_price, stop_order_id=new_stop_id, stop_price=new_stop_price, entry_strategy=entry_strategy)
            return "PARTIAL"
        else:
            db.clear_position_state(SYMBOL)
            return "FLAT"
    except Exception as e:
        log(f"  Flatten sell failed: {e}")
        return "FAILED"


# ---------- Stage 5: persisted breaker liquidation ----------
# A breaker flatten is a LIQUIDATION that stays pending (db liquidation_state) until a
# strict position read CONFIRMS flat. Each cycle, before any other position logic and
# regardless of the day's breaker status: reconcile our own last liquidation sell (by id,
# or by deterministic client id if the id write was lost) -> if it can still execute or
# its state is unknown, WAIT (never repeat a sell) -> confirm every other open sell is
# cancelled (they reserve quantity) -> persist the intent -> sell exactly the quantity
# confirmed THIS cycle -> bounded in-cycle confirmation. New entries are blocked while
# pending. Flat uses the existing 0.0001 BTC threshold (verify_sell_filled).
#
# Protection gap -- stated accurately: while liquidating, the residual has NO resting
# protective stop (stops are cancelled so the sell is not rejected for reserved balance).
# NOTHING here bounds the duration or the loss of that exposure: a working market sell
# may not execute, the next cycle may not run (process stopped, outages), sells can be
# rejected, and while position/order reads fail nothing is sold or protected. Sizing any
# order without a confirmed quantity is what this stage forbids; choosing a different
# unknown-quantity policy is pending decision D8.
LIQ_FLAT_BTC = 0.0001


def _open_sell_orders_strict():
    """Open SELL orders for SYMBOL, or None if the broker's answer is unknown."""
    try:
        orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL]))
    except Exception:
        return None
    out = []
    for o in orders:
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        if side == "sell":
            out.append(o)
    return out


def _complete_liquidation(liq, qty):
    log(f"LIQUIDATION COMPLETE — flat confirmed by a strict position read (qty {qty}). "
        f"Reason: {liq.get('reason')}; {liq.get('attempt', 0)} sell attempt(s).")
    db.clear_position_state(SYMBOL)
    db.clear_liquidation_state(SYMBOL)
    db.mark_breaker_tripped(EQUITY_BASELINE_KEY, liq.get("day_stamp"))
    return "FLAT"


def _liquidation_step(qty, reason=None, day_stamp=None):
    """One reconcile-then-act step. Order (Stage 5b): (1) reconcile our own last sell,
    (2) cancel and CONFIRM every other open sell, (3) RE-READ the position -- the only read
    used to complete or to size, since fills can happen during cancellation -- then
    (4) complete only if that read confirms flat, else sell exactly that quantity.
    Any UNKNOWN (order state, open-order list, position) keeps the liquidation pending
    and submits nothing. `qty` (the cycle-top read) is informational only.
    Returns "FLAT" (confirmed, completed) or "PENDING"."""
    liq = db.get_liquidation_state(SYMBOL) or {}
    if not liq:
        liq = {"reason": reason, "day_stamp": day_stamp, "attempt": 0, "order_id": None,
               "client_order_id": None, "started": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")}
        db.set_liquidation_state(SYMBOL, **liq)
    # 1. Reconcile our own last liquidation sell.
    ref = liq.get("order_id") or (("cid:" + liq["client_order_id"]) if liq.get("client_order_id") else None)
    if ref:
        fs = get_order_fill_state(ref)
        if fs["state"] == "OPEN" and qty <= LIQ_FLAT_BTC:
            # Position already reads flat but our sell can still execute: cancel it and re-check.
            cancel_order_if_open(fs.get("id") or liq.get("order_id"))
            fs = get_order_fill_state(ref)
        if fs["state"] == "OPEN":
            log(f"  Liquidation sell {ref} still working (filled {fs.get('filled_qty')}) — waiting; no repeat sell.")
            return "PENDING"
        if fs["state"] == "UNKNOWN":
            log(f"  Liquidation sell {ref} state UNKNOWN — waiting to reconcile; no repeat sell, no completion.")
            return "PENDING"
        if fs["state"] == "NOT_FOUND" and liq.get("order_id"):
            log(f"  Liquidation sell {ref} not found by id — waiting to reconcile; no repeat sell, no completion.")
            return "PENDING"
    # 2. Every other open sell can still execute (and reserves quantity): cancel, then CONFIRM gone.
    opens = _open_sell_orders_strict()
    if opens is None:
        log("  Open orders UNKNOWN — not selling or completing this cycle.")
        return "PENDING"
    if opens:
        for o in opens:
            cancel_order_if_open(str(o.id))
        still = _open_sell_orders_strict()
        if still is None or still:
            log("  Could not confirm open sell orders cancelled — not selling or completing this cycle.")
            return "PENDING"
    # 3. Re-read AFTER reconciliation/cancellation (a stop may have filled during cancellation).
    q = _position_qty_strict()
    if q is None:
        log("  Position UNKNOWN after reconciliation — liquidation stays pending; nothing submitted.")
        return "PENDING"
    if q <= LIQ_FLAT_BTC:
        return _complete_liquidation(liq, q)
    # 4. Persist intent BEFORE submitting, then sell exactly the post-reconciliation quantity.
    attempt = int(liq.get("attempt") or 0) + 1
    cid = f"liq-{liq.get('started')}-{attempt}"
    liq.update(attempt=attempt, client_order_id=cid, order_id=None, last_confirmed_qty=q)
    db.set_liquidation_state(SYMBOL, **liq)
    sell_qty = _floor_qty(q)
    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=SYMBOL, qty=sell_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            client_order_id=cid))
    except Exception as e:
        log(f"  Liquidation sell attempt {attempt} submit failed/unknown ({e}); reconciling by client id next cycle.")
        return "PENDING"
    liq["order_id"] = str(order.id)
    db.set_liquidation_state(SYMBOL, **liq)
    log(f"LIQUIDATION sell attempt {attempt}: {sell_qty} BTC (confirmed after reconciliation) — id {order.id}")
    # 5. Bounded in-cycle check. Completion is NOT declared here: the next cycle must reconcile
    #    this sell as terminal and confirm flat (step 1-3) -- unless it is already confirmed
    #    FILLED now and a latest read confirms zero.
    remaining = verify_sell_filled(sell_qty)
    if remaining == 0.0 and get_order_fill_state(str(order.id))["state"] == "FILLED":
        opens = _open_sell_orders_strict()
        if opens == []:
            return _complete_liquidation(liq, 0.0)
    log(f"  Liquidation still PENDING (latest read: {'UNKNOWN' if remaining is None else remaining}); "
        "breaker NOT marked done; new entries blocked; next cycle reconciles before acting.")
    return "PENDING"


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
# ---------- P0-1: scalp Target 1 without BTC oversubscription ----------
# Alpaca crypto has no OCO/bracket, and a resting stop-limit sell reserves the
# whole BTC quantity, so a full-qty stop and a resting half-qty TP cannot
# coexist. Design: the full-qty stop is the only resting order. When the live
# bid reaches the Target-1 price, the stop is cancelled (confirmed), an IOC
# limit sell for half is sent at the target price (never sells below it), and
# the phase advances ONLY by the broker-reported cumulative filled_qty.
#
# Protection gap — stated accurately: from the confirmed stop cancel until a
# replacement stop is accepted, the position has NO resting broker stop. The
# code does not bound that duration: requests can stall, the process can stop,
# and replacement can fail. TP_FILL_POLL_S bounds only how long this call waits
# for the IOC's outcome. Recovery relies on the persisted intent (client order
# id + state) being reconciled on the next cycle or after a restart.
# Target 1 is observed at cycle granularity.
TP_FILL_POLL_S = 5


def _stop_qty(stop_id):
    try:
        return float(trading_client.get_order_by_id(stop_id).qty)
    except Exception:
        return None


def _scalp_persist_phase1(entry, entry_time, peak, stop_id, stop_price, tp_id, tp_price, original_qty):
    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=stop_id, stop_price=stop_price,
                           entry_time=entry_time, peak_price=peak, take_profit_order_id=tp_id,
                           take_profit_price=tp_price, entry_strategy="SCALP",
                           target1_filled=False, original_qty=original_qty)


def _place_validated_stop(qty, stop_price, context):
    """Validates the FINAL stop candidate against a fresh bid before submitting.

    Returns (stop_order_id, stop_price, action):
      STOP              stop placed strictly below the bid
      STOP_UNVALIDATED  no usable bid: the given (pre-existing, never widened)
                        floor is placed, and flagged as unvalidated
      RISK_EXIT         the floor is at/above the bid, i.e. the risk threshold
                        is already breached: a stop there would trigger at once
                        and its limit could sit above a falling bid (no
                        effective protection). A market sell is submitted.
                        The floor is NOT lowered to make an order admissible.
      RISK_EXIT_UNKNOWN the market sell's outcome is unknown; the position is
                        re-read next cycle, never assumed flat
      NONE              nothing to protect
    NOTE: get_live_quote() carries no timestamp, so a STALE quote cannot be
    detected here (audit P1-5, still open)."""
    qty = _floor_qty(qty)
    if qty <= 0:
        return None, stop_price, "NONE"
    quote = get_live_quote()
    bid = quote[0] if quote else None
    if bid is None or not math.isfinite(bid) or bid <= 0:
        log(f"[STRATEGY: SCALP]  No usable bid ({context}) — placing the existing ${stop_price:.2f} floor UNVALIDATED "
            f"(not widened); it will be re-checked next cycle.")
        sid, spx = _submit_stop_limit_sell(qty, stop_price)
        return sid, spx, "STOP_UNVALIDATED"
    if stop_price < bid:
        sid, spx = _submit_stop_limit_sell(qty, stop_price)
        return sid, spx, "STOP"
    import uuid
    cid = f"p01x-{uuid.uuid4().hex[:24]}"
    log(f"[STRATEGY: SCALP] RISK EXIT ({context}): stop floor ${stop_price:.2f} is at/above the bid ${bid:.2f} — "
        f"the risk threshold is already breached, so a stop there would trigger immediately and could rest unfilled. "
        f"Market-selling {qty:.6f} BTC instead; the floor is NOT widened. client id {cid}.")
    try:
        order = trading_client.submit_order(MarketOrderRequest(
            symbol=SYMBOL, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=cid,
        ))
        log(f"[STRATEGY: SCALP]  Risk-exit market sell submitted — id {order.id}.")
        return None, stop_price, "RISK_EXIT"
    except Exception as e:
        log(f"[STRATEGY: SCALP]  Risk-exit submission outcome UNKNOWN ({e}) — NOT assumed filled or rejected; "
            f"the position is re-read and re-protected next cycle.")
        return None, stop_price, "RISK_EXIT_UNKNOWN"


def _ensure_stop_covers(stop_id, qty, stop_price):
    """Keep the resting stop covering `qty`. Re-place only if it's missing or
    short AND the old one is confirmed cancelled (never double-reserve)."""
    if qty <= 0:
        return stop_id, stop_price
    if stop_id and stop_order_still_open(stop_id):
        sq = _stop_qty(stop_id)
        if sq is None or sq >= qty - 1e-9:
            return stop_id, stop_price           # covered (or can't tell: leave it)
        if not cancel_and_confirm(stop_id):
            log("[STRATEGY: SCALP]  Stop covers less than the position but its cancellation couldn't be confirmed — leaving it; retry next cycle.")
            return stop_id, stop_price
    sid, spx, _ = _place_validated_stop(qty, stop_price, "stop coverage")
    return sid, spx


def _scalp_advance_after_target1(pos_state, entry, entry_time, peak, stop_id, tp_ref, fs):
    """Advance to phase 2 using the CONFIRMED filled quantity only.

    The remaining position is taken ONLY from the broker. If it cannot be read,
    nothing happens this call: no trade row, no phase change, no stop cancel or
    placement, and persisted state (incl. the TP reference) is untouched, so the
    next cycle re-runs this exact reconciliation. This makes Target-1 recording
    retryable across failed reads. Durable execution keys below prevent duplicate
    trade rows across restarts; broker side effects still need recovery. (No arithmetic fallback: a
    later cycle's observed qty is already net of the fill, so subtracting the
    cumulative fill again understated the position — review of 51bbd91.)"""
    broker_order_id = fs.get("id")
    if fs.get("state") not in ("FILLED", "TERMINAL") or not broker_order_id:
        log("[STRATEGY: SCALP] Cannot record Target 1 without a terminal outcome and canonical broker order ID; reconciliation remains pending.")
        return
    execution_key = f"alpaca:order:{broker_order_id}:target1"
    actual = _position_qty_strict()
    if actual is None:
        log(f"[STRATEGY: SCALP] POSITION UNKNOWN after Target-1 order {tp_ref} reported {fs.get('filled_qty')} filled — "
            f"not recording, not advancing, not sizing any order; will reconcile when the position is readable.")
        return
    sold = float(fs["filled_qty"])
    tp_price = pos_state.get("take_profit_price") or entry
    reason = "target1" if fs["state"] == "FILLED" else "target1_partial"
    avg = fs.get("avg_price")
    if avg is not None:
        exit_price, slippage, note = avg, abs(avg - tp_price), ""
    else:
        # Quantity is confirmed, price is not. Never record the intended target
        # as a verified execution price.
        exit_price, slippage = tp_price, None
        reason += "_price_unconfirmed"
        note = (" EXIT PRICE UNCONFIRMED: broker reported no valid filled_avg_price, so the row uses the TARGET "
                "price as an ESTIMATE, P&L is an estimate and slippage is unknown — reconcile against broker activities.")
    trade = db.log_trade(SYMBOL, "SCALP", entry_price=entry, exit_price=exit_price, qty=sold,
                          entry_time=entry_time, exit_time=datetime.now(timezone.utc).isoformat(),
                          exit_reason=reason, slippage=slippage, execution_key=execution_key)
    if trade.get("replayed"):
        log(f"[STRATEGY: SCALP] Target 1 already recorded for {broker_order_id}; reusing row {trade.get('id')}, not another sale."
            + (" Price evidence differs; stored accounting retained pending reconciliation." if trade.get("price_discrepancy") else ""))
    else:
        log(f"[STRATEGY: SCALP] Target 1 CONFIRMED — order {fs.get('id') or tp_ref} {fs.get('status')}, filled {sold:.6f} BTC "
            f"{'@ $' + format(avg, '.2f') if avg is not None else '(price unconfirmed)'}. "
            f"Trade logged ({reason}) — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}.{note}")
    if stop_id and stop_order_still_open(stop_id) and not cancel_and_confirm(stop_id):
        log("[STRATEGY: SCALP]  Couldn't confirm the old stop was cancelled — keeping it (it still protects its quantity); breakeven stop will be placed next cycle.")
        db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=stop_id, stop_price=pos_state.get("stop_price"),
                               entry_time=entry_time, peak_price=peak, take_profit_order_id=None, take_profit_price=None,
                               entry_strategy="SCALP", target1_filled=True, original_qty=pos_state.get("original_qty"))
        return
    remaining_qty = _floor_qty(actual)                  # from the broker, read before any action above
    if remaining_qty <= 0:
        db.clear_position_state(SYMBOL)
        return
    breakeven_price = round(entry * strategies.SCALP_BREAKEVEN_MULT, 2)
    prior_stop = pos_state.get("stop_price")
    quote = get_live_quote()
    bid = quote[0] if quote else None
    if bid is not None and breakeven_price < bid:
        candidate = breakeven_price
    elif prior_stop:
        candidate = prior_stop                          # existing floor; validated below, never widened
        log(f"[STRATEGY: SCALP]  Breakeven ${breakeven_price:.2f} is not below the "
            f"{'bid $' + format(bid, '.2f') if bid is not None else 'unavailable bid'} — candidate is the prior stop ${prior_stop:.2f}.")
    else:
        candidate = breakeven_price                     # no prior floor exists; validated below
    new_stop_id, new_stop_price, action = _place_validated_stop(remaining_qty, candidate, "post-Target-1")
    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_stop_id, stop_price=candidate,
                           entry_time=entry_time, peak_price=peak, take_profit_order_id=None, take_profit_price=None,
                           entry_strategy="SCALP", target1_filled=True, original_qty=pos_state.get("original_qty"))
    log(f"[STRATEGY: SCALP] Remaining {remaining_qty:.6f} BTC: {action} (floor ${candidate:.2f}).")


def _scalp_reconcile_tp(pos_state, entry, entry_time, peak, stop_id, stop_price, tp_ref, fs,
                        observed_qty, tp_qty, tp_price, original_qty, just_submitted):
    """Decide the next action from the TP order's reconciled state, the ACTUAL
    broker position and ACTUAL open-order reservations — never from the
    pre-submission quantity alone. UNKNOWN stays UNKNOWN."""
    st = fs["state"]
    if st in ("FILLED", "TERMINAL") and (fs.get("filled_qty") or 0) > 0:
        _scalp_advance_after_target1(pos_state, entry, entry_time, peak, stop_id, tp_ref, fs)
        return
    if st == "TERMINAL" or (st == "NOT_FOUND" and not just_submitted):
        # Definitively nothing sold (terminal with 0 filled), or the broker still
        # has no such order a full cycle after submission: restore full cover.
        what = f"ended {fs.get('status')} with 0 filled" if st == "TERMINAL" else "does not exist at the broker"
        log(f"[STRATEGY: SCALP] Target-1 order {tp_ref} {what} — NOT Target 1; keeping full protection.")
        if just_submitted:
            # The stop was cancelled for this TP: size the replacement from a fresh
            # broker read, not from the pre-submission quantity.
            pos = _position_qty_strict()
            if pos is None:
                log(f"[STRATEGY: SCALP] POSITION UNKNOWN after Target-1 order {tp_ref} ended unfilled — no stop sized "
                    f"from the pre-submission quantity; reference kept, reconciling next cycle. POSITION UNPROTECTED meanwhile.")
                _scalp_persist_phase1(entry, entry_time, peak, None, stop_price, tp_ref, tp_price, original_qty)
                return
            new_id, new_px, _ = _place_validated_stop(pos, stop_price, "TP ended unfilled")
        else:
            # observed_qty was read from the broker earlier THIS cycle (strict).
            new_id, new_px = _ensure_stop_covers(stop_id, _floor_qty(observed_qty), stop_price)
        _scalp_persist_phase1(entry, entry_time, peak, new_id, new_px, None, tp_price, original_qty)
        return
    # OPEN, UNKNOWN, or NOT_FOUND right after submission (may not be visible yet):
    # never a fill. Protect exactly what the broker shows as held and unreserved.
    pos = _position_qty_strict()
    reserved = _open_sell_reserved_strict()
    if pos is None:
        log(f"[STRATEGY: SCALP] Target-1 order {tp_ref} is {st} AND the position cannot be read — "
            f"NOT placing orders from stale quantities. PROTECTION STATUS UNKNOWN; retrying next cycle.")
        _scalp_persist_phase1(entry, entry_time, peak, stop_id, stop_price, tp_ref, tp_price, original_qty)
        return
    if reserved is None:
        reserved = tp_qty
        log("[STRATEGY: SCALP]  Open-order reservations unreadable — conservatively assuming the TP still reserves its full quantity.")
    unreserved = _floor_qty(pos - reserved)
    new_id, new_px = stop_id, stop_price
    if unreserved > 0 and not (stop_id and stop_order_still_open(stop_id)):
        new_id, new_px, _ = _place_validated_stop(unreserved, stop_price, f"TP {st}")
    _scalp_persist_phase1(entry, entry_time, peak, new_id, new_px, tp_ref, tp_price, original_qty)
    filled_note = f" ({fs.get('filled_qty') or 0:.6f} filled so far)" if st == "OPEN" else ""
    log(f"[STRATEGY: SCALP] Target-1 order {tp_ref} is {st}{filled_note} — NOT treated as filled; broker shows "
        f"{pos:.6f} BTC held, {reserved:.6f} reserved; protecting {max(unreserved, 0):.6f}; reconciling next cycle.")


def _scalp_execute_target1(pos_state, entry, entry_time, peak, stop_id, stop_price, current_qty, tp_qty, tp_price, original_qty):
    if not cancel_and_confirm(stop_id):
        log("[STRATEGY: SCALP] Target 1 reached, but the full-qty stop's cancellation could not be confirmed — NOT sending the TP (it would oversubscribe BTC / race the stop). Retrying next cycle.")
        return
    import uuid
    cid = f"p01t1-{uuid.uuid4().hex[:24]}"
    tp_ref = "cid:" + cid
    # Intent (client order id) is persisted BEFORE submission, so a lost
    # response, crash or restart can always be reconciled by that identity.
    _scalp_persist_phase1(entry, entry_time, peak, None, stop_price, tp_ref, tp_price, original_qty)
    try:
        order = trading_client.submit_order(LimitOrderRequest(
            symbol=SYMBOL, qty=tp_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.IOC, limit_price=round(tp_price, 2), client_order_id=cid,
        ))
        # Response received: track by the broker's own order id (no dependence on
        # client-id lookup in the normal path); the client id stays the fallback.
        tp_ref = str(order.id)
        _scalp_persist_phase1(entry, entry_time, peak, None, stop_price, tp_ref, tp_price, original_qty)
        log(f"[STRATEGY: SCALP] Target 1 reached — IOC limit sell {tp_qty:.6f} BTC @ ${tp_price:.2f} submitted — id {order.id}, client id {cid}.")
    except Exception as e:
        log(f"[STRATEGY: SCALP] Target-1 submission outcome UNKNOWN ({e}) — NOT treated as rejected; reconciling by client id {cid}.")
    deadline = time.time() + TP_FILL_POLL_S
    while True:
        fs = get_order_fill_state(tp_ref)
        if fs["state"] in ("FILLED", "TERMINAL") or time.time() >= deadline:
            break
        time.sleep(0.5)
    _scalp_reconcile_tp(pos_state, entry, entry_time, peak, None, stop_price, tp_ref, fs,
                        current_qty, tp_qty, tp_price, original_qty, just_submitted=True)


def _scalp_phase1_manage(pos_state, entry, entry_time, peak, latest_price, stop_id, tp_id, current_qty):
    """Phase 1 (full size held, current_qty > 0). Target 1 advances ONLY on a
    confirmed positive cumulative filled_qty; rejected, missing, canceled,
    still-working, not-found and unknown TP orders never count as fills."""
    tp_price = pos_state.get("take_profit_price")
    stop_price = pos_state.get("stop_price") or entry
    original_qty = pos_state.get("original_qty") or current_qty
    tp_qty = _floor_qty(min(original_qty / 2.0, current_qty))

    if tp_id:
        fs = get_order_fill_state(tp_id)
        _scalp_reconcile_tp(pos_state, entry, entry_time, peak, stop_id, stop_price, tp_id, fs,
                            current_qty, tp_qty, tp_price, original_qty, just_submitted=False)
        return

    # No TP order in flight: full-qty stop must rest; fire Target 1 if reached.
    new_id, new_px = _ensure_stop_covers(stop_id, _floor_qty(current_qty), stop_price)
    if new_id != stop_id:
        _scalp_persist_phase1(entry, entry_time, peak, new_id, new_px, None, tp_price, original_qty)
        stop_id, stop_price = new_id, new_px
    if not stop_id:
        return                                   # unprotected: never add a sell on top; retry next cycle
    quote = get_live_quote()
    bid = quote[0] if quote else None
    if tp_price and bid is not None and bid >= tp_price and tp_qty > 0:
        _scalp_execute_target1(pos_state, entry, entry_time, peak, stop_id, stop_price,
                               current_qty, tp_qty, tp_price, original_qty)


def main():
    global SHORT_WINDOW, LONG_WINDOW
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
        # Tier 6: pick up any live parameter overrides saved from the
        # dashboard's /settings page since last cycle (see the matching
        # comment in day_trading_bot.py's main loop for the full
        # explanation). Phase 4: resolved per-symbol (global override
        # -> this symbol's own override, same precedence as the
        # equity bot) even though this bot only trades one symbol
        # today — SYMBOL is a plain "BTC/USD" string, not hardcoded
        # into this call, so a second crypto symbol added later picks
        # up its own per-symbol settings with no code change here.
        # SHORT_WINDOW/LONG_WINDOW local sync is for this file's own
        # now-unused module-level copies (signal generation goes
        # through strategies.get_signal(), which reads strategies.
        # SHORT_WINDOW/LONG_WINDOW directly) — kept for consistency.
        symbol_params = strategies.resolve_effective_params(
            SYMBOL, db.get_strategy_params(), db.get_strategy_params_for_symbol(SYMBOL)
        )
        strategies.apply_live_params(symbol_params)
        SHORT_WINDOW = strategies.SHORT_WINDOW
        LONG_WINDOW = strategies.LONG_WINDOW

        baseline = reset_day_if_needed()
        today_stamp = baseline["day_stamp"]

        try:
            today_pl = get_today_pl(baseline)
        except Exception as e:
            log(f"  Error fetching account P/L: {e}")
            today_pl = 0.0

        circuit_breaker_tripped = today_pl <= -DAILY_LOSS_LIMIT_USD
        already_flattened_today = baseline.get("breaker_tripped_stamp") == today_stamp

        # P0-2: an unreadable position is UNKNOWN, not zero. Treating it as zero
        # used to skip all protection logic, clear persisted state (losing
        # pending order references) and allow a NEW ENTRY. Now nothing that
        # depends on the position runs this cycle; state is left untouched.
        qty = _position_qty_strict()
        pos_state = db.get_position_state(SYMBOL)
        if qty is None:
            log("POSITION UNKNOWN — broker position read failed (not a confirmed zero). Skipping all "
                "position-dependent actions and new entries this cycle; state and pending order references preserved.")
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        # Stage 5: a pending liquidation takes precedence over everything else (including
        # a new UTC day, when the breaker itself is no longer tripped) and blocks entries.
        if db.get_liquidation_state(SYMBOL):
            log("LIQUIDATION PENDING — reconciling before any other action; new entries blocked.")
            _liquidation_step(qty)
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        if circuit_breaker_tripped:
            if not already_flattened_today:
                log(
                    f"Daily loss limit hit (P/L ${today_pl:.2f}) — liquidating BTC "
                    f"position and halting new buys until tomorrow (UTC)."
                )
                if qty > LIQ_FLAT_BTC:
                    # Breaker is marked done ONLY when flat is confirmed (inside
                    # _liquidation_step); PARTIAL/UNCONFIRMED/FAILED stay pending.
                    _liquidation_step(qty, reason=f"circuit breaker (P/L ${today_pl:.2f})", day_stamp=today_stamp)
                else:
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
            # Phase 6: which entry path opened this position. Untagged
            # rows (entry_strategy is None) predate this feature and are
            # always TREND — the only kind of position that could exist
            # before Mean-Reversion Scalp was added.
            entry_strategy = pos_state.get("entry_strategy") or "TREND"
            tp_id = pos_state.get("take_profit_order_id")

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
            # Universal: applies to TREND and SCALP positions alike —
            # a gapping stop threatens both the same way.
            if stop_order_still_open(stop_id) and is_stop_hung(pos_state.get("stop_price")):
                log(f"EMERGENCY: protective stop appears hung — the live bid has fallen through its limit price (price likely gapped through both stop and limit). Cancelling and dispatching an emergency market sell.")
                cancel_order_if_open(stop_id)
                if entry_strategy == "SCALP":
                    cancel_order_if_open(tp_id)
                try:
                    order = place_market_sell(qty)
                    log(f"  Emergency market sell submitted: id {order.id}")
                    remaining = verify_sell_filled(qty)
                    if remaining is None:
                        log("  EXIT UNCONFIRMED — the sell was submitted but no position read confirmed the "
                            "outcome. State preserved, no trade logged; next cycle re-checks.")
                        pass  # state intentionally untouched: exit unconfirmed
                    elif remaining > 0:
                        log(f"  Warning: {remaining} BTC still held after the emergency sell — leaving position tracked for next cycle rather than clearing state.")
                        db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak, entry_strategy=entry_strategy)
                    else:
                        target_price = pos_state.get("stop_price") or entry
                        fill_price = get_actual_fill_price(order.id, target_price)
                        trade = db.log_trade(SYMBOL, entry_strategy, entry_price=entry, exit_price=fill_price,
                                              qty=qty, entry_time=entry_time,
                                              exit_time=datetime.now(timezone.utc).isoformat(),
                                              exit_reason="emergency_hung_stop",
                                              slippage=abs(fill_price - target_price))
                        log(f"  Trade logged — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}, slippage ${trade['slippage']:.2f}.")
                        db.clear_position_state(SYMBOL)
                except Exception as e:
                    log(f"  Emergency market sell failed: {e}")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Phase 6: Mean-Reversion Scalp positions are reconciled
            # entirely separately from here on, and never fall through
            # to the TREND-only time-decay/chandelier-trail logic below
            # — Alpaca has no native crypto bracket (order_class=
            # bracket/oco/oto is equity-only), so every leg here is a
            # SYNTHETIC, hand-reconciled order.
            #
            # Phase 8: Scalp 50/50 split. A scalp position now has two
            # phases, tracked by target1_filled:
            #   False — full size held. A full-qty stop-limit AND a
            #     half-qty Target-1 limit sell both rest at once.
            #   True  — half size held (Target 1 already sold). The
            #     stop has been moved to breakeven (SCALP_BREAKEVEN_
            #     MULT) and the remaining half now trails with the
            #     SAME ratcheting chandelier stop TREND positions use
            #     (Momentum Ratchet widening included), reimplemented
            #     separately here rather than sharing the TREND block's
            #     code — deliberate, to avoid any risk of regressing
            #     that already-verified logic while adding this.
            if entry_strategy == "SCALP" and not pos_state.get("target1_filled"):
                current_qty = _position_qty_strict()
                if current_qty is None:
                    log("POSITION UNKNOWN — position re-read failed mid-cycle; no close recorded, state preserved, retrying next cycle.")
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue
                if current_qty <= 0:
                    # Full-qty stop fired before Target 1 was ever hit
                    # (Target 1 is only half-qty — it can't zero the
                    # position out alone), or both raced/were already
                    # cleaned up.
                    stop_still_open = stop_order_still_open(stop_id)
                    tp_still_open = stop_order_still_open(tp_id)
                    exit_price_estimate = pos_state.get("stop_price") or entry
                    exit_reason = "stop_loss_before_target1"
                    if tp_still_open:
                        cancel_order_if_open(tp_id)
                        log("[STRATEGY: SCALP] Stop-loss hit (before Target 1) — position closed. Cancelled the now-orphaned take-profit order.")
                    elif stop_still_open:
                        cancel_order_if_open(stop_id)
                        log("[STRATEGY: SCALP] Take-profit-style close before Target 1 tracking updated — clearing state.")
                    else:
                        log("[STRATEGY: SCALP] Position closed before Target 1, neither leg reads as still-open — clearing state defensively.")
                        cancel_order_if_open(stop_id)
                        cancel_order_if_open(tp_id)
                    sold_qty = pos_state.get("original_qty") or qty
                    trade = db.log_trade(SYMBOL, "SCALP", entry_price=entry, exit_price=exit_price_estimate,
                                          qty=sold_qty, entry_time=entry_time,
                                          exit_time=datetime.now(timezone.utc).isoformat(), exit_reason=exit_reason)
                    log(f"[STRATEGY: SCALP] Trade logged — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}.")
                    db.clear_position_state(SYMBOL)
                else:
                    # P0-1: Target 1 is decided ONLY from the TP order's
                    # confirmed cumulative filled_qty (see
                    # _scalp_phase1_manage). "Order not open" is never a fill.
                    _scalp_phase1_manage(pos_state, entry, entry_time, peak, latest_price,
                                         stop_id, tp_id, current_qty)
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            elif entry_strategy == "SCALP" and pos_state.get("target1_filled"):
                # Phase 2: runner half, breakeven stop already set,
                # trailing with the ratcheting chandelier stop —
                # deliberately reimplemented here rather than reusing
                # the TREND block below (see the comment above this
                # whole section for why).
                current_qty = _position_qty_strict()
                if current_qty is None:
                    log("POSITION UNKNOWN — position re-read failed mid-cycle; no close recorded, state preserved, retrying next cycle.")
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue
                if current_qty <= 0:
                    exit_price_estimate = pos_state.get("stop_price") or entry
                    # The runner's qty is whatever's left after Target
                    # 1's sale — this cycle's own `qty` (read at the top
                    # of the reconciliation block, before this check)
                    # already reflects exactly that.
                    trade = db.log_trade(SYMBOL, "SCALP", entry_price=entry, exit_price=exit_price_estimate,
                                          qty=qty, entry_time=entry_time,
                                          exit_time=datetime.now(timezone.utc).isoformat(), exit_reason="runner_stop")
                    log(f"[STRATEGY: SCALP] Runner closed — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}.")
                    db.clear_position_state(SYMBOL)
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue

                if not stop_order_still_open(stop_id):
                    tracked_stop_price = pos_state.get("stop_price") or entry
                    # P0-1 review: validate against the bid; a breached floor means risk exit, not an above-market stop.
                    new_stop_id, _, _action = _place_validated_stop(current_qty, tracked_stop_price, "runner stop missing")
                    new_stop_price = tracked_stop_price
                    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_stop_id, stop_price=new_stop_price,
                                           entry_time=entry_time, peak_price=peak, entry_strategy="SCALP",
                                           target1_filled=True, original_qty=pos_state.get("original_qty"))
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue

                candidate = strategies.chandelier_stop_price(entry, peak, atr_value)
                current_stop_price = pos_state.get("stop_price")
                live_quote = get_live_quote()
                live_bid = live_quote[0] if live_quote else latest_price
                if candidate and candidate >= live_bid:
                    log(f"[STRATEGY: SCALP] Runner's chandelier trail candidate (${candidate:.2f}) is at/above the live bid (${live_bid:.2f}) — exiting the runner at market instead of raising the stop.")
                    if is_spread_ok():
                        confirmed = cancel_and_confirm(stop_id)
                        if not confirmed:
                            log("[STRATEGY: SCALP]  Warning: couldn't confirm stop cancellation before this trail-triggered exit — proceeding anyway.")
                        try:
                            order = place_market_sell(current_qty)
                            log(f"[STRATEGY: SCALP]  Order submitted: runner sell (trail breach) — id {order.id}")
                            remaining = verify_sell_filled(current_qty)
                            if remaining is None:
                                log("  EXIT UNCONFIRMED — the sell was submitted but no position read confirmed the "
                                    "outcome. State preserved, no trade logged; next cycle re-checks.")
                                pass  # state intentionally untouched: exit unconfirmed
                            elif remaining > 0:
                                log(f"[STRATEGY: SCALP]  Warning: {remaining} BTC still held after trail-breach sell — leaving position tracked rather than clearing state.")
                                db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None,
                                                       entry_time=entry_time, peak_price=peak, entry_strategy="SCALP",
                                                       target1_filled=True, original_qty=pos_state.get("original_qty"))
                            else:
                                trade = db.log_trade(SYMBOL, "SCALP", entry_price=entry, exit_price=live_bid, qty=current_qty,
                                                      entry_time=entry_time, exit_time=datetime.now(timezone.utc).isoformat(),
                                                      exit_reason="runner_trail_breach")
                                log(f"[STRATEGY: SCALP]  Trade logged — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}.")
                                db.clear_position_state(SYMBOL)
                        except Exception as e:
                            log(f"[STRATEGY: SCALP]  Trail-breach sell failed: {e}")
                    else:
                        log("[STRATEGY: SCALP]  Trail-breach exit deferred — spread too wide this cycle, will retry next cycle.")
                elif candidate and (not current_stop_price or candidate > current_stop_price):
                    confirmed = cancel_and_confirm(stop_id)
                    if confirmed:
                        new_id, new_price = _submit_stop_limit_sell(current_qty, candidate)
                        db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_id, stop_price=new_price,
                                               entry_time=entry_time, peak_price=peak, entry_strategy="SCALP",
                                               target1_filled=True, original_qty=pos_state.get("original_qty"))
                    else:
                        log("[STRATEGY: SCALP]  Couldn't confirm cancellation before raising the trailing stop — will retry next cycle.")
                elif peak != pos_state.get("peak_price"):
                    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=stop_id, stop_price=current_stop_price,
                                           entry_time=entry_time, peak_price=peak, entry_strategy="SCALP",
                                           target1_filled=True, original_qty=pos_state.get("original_qty"))
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Time-decay exit: force a market close if this position
            # hasn't moved favorably within TIME_DECAY_BARS bars —
            # frees up capital rather than waiting indefinitely for a
            # stop or a reversal signal that may never come. TREND-only
            # from here down — SCALP positions already `continue`d above.
            decayed = False
            if entry_time:
                try:
                    elapsed_bars = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_time)).total_seconds() / CHECK_INTERVAL_SECONDS
                    if elapsed_bars >= strategies.TIME_DECAY_BARS and latest_price <= entry:
                        decayed = True
                except Exception:
                    pass
            if decayed:
                log(f"Time-decay exit — no favorable move within {strategies.TIME_DECAY_BARS} bars ({strategies.TIME_DECAY_BARS*3}min) of entry. Exiting at market to free capital.")
                if is_spread_ok():
                    confirmed = cancel_and_confirm(stop_id)
                    if not confirmed:
                        log("  Warning: couldn't confirm stop cancellation before time-decay exit — proceeding anyway.")
                    try:
                        order = place_market_sell(qty)
                        log(f"  Order submitted: BTC sell (time-decay) — id {order.id}")
                        remaining = verify_sell_filled(qty)
                        if remaining is None:
                            log("  EXIT UNCONFIRMED — the sell was submitted but no position read confirmed the "
                                "outcome. State preserved, no trade logged; next cycle re-checks.")
                            pass  # state intentionally untouched: exit unconfirmed
                        elif remaining > 0:
                            log(f"  Warning: {remaining} BTC still held after time-decay sell — leaving position tracked for next cycle rather than clearing state.")
                            db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak, entry_strategy="TREND")
                        else:
                            # No resting limit/stop level to compare
                            # against for a time-decay market exit — the
                            # last observed price at decision time is the
                            # natural slippage reference instead.
                            fill_price = get_actual_fill_price(order.id, latest_price)
                            trade = db.log_trade(SYMBOL, "TREND", entry_price=entry, exit_price=fill_price,
                                                  qty=qty, entry_time=entry_time,
                                                  exit_time=datetime.now(timezone.utc).isoformat(),
                                                  exit_reason="time_decay",
                                                  slippage=abs(fill_price - latest_price))
                            log(f"  Trade logged — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}, slippage ${trade['slippage']:.2f}.")
                            db.clear_position_state(SYMBOL)
                    except Exception as e:
                        log(f"  Time-decay sell failed: {e}")
                else:
                    log("  Time-decay exit deferred — spread too wide this cycle, will retry next cycle.")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            if not stop_order_still_open(stop_id):
                current_qty = _position_qty_strict()
                if current_qty is None:
                    log("POSITION UNKNOWN — position re-read failed mid-cycle; no close recorded, state preserved, retrying next cycle.")
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue
                open_count = get_open_order_count()
                if current_qty <= 0:
                    # The resting stop-limit filled on its own — Alpaca
                    # closed it natively, so this is the ONE reconciliation
                    # branch that previously logged nothing to trade_history
                    # at all for TREND positions, even though it's the most
                    # common way a TREND position actually closes. The
                    # stop's OWN price (what the strategy intended to exit
                    # at) is the target for slippage; get_actual_fill_price()
                    # queries what it really filled at.
                    target_price = pos_state.get("stop_price") or entry
                    fill_price = get_actual_fill_price(stop_id, target_price)
                    trade = db.log_trade(SYMBOL, entry_strategy, entry_price=entry, exit_price=fill_price,
                                          qty=qty, entry_time=entry_time,
                                          exit_time=datetime.now(timezone.utc).isoformat(),
                                          exit_reason="stop_loss",
                                          slippage=abs(fill_price - target_price))
                    log(f"Protective stop filled — position closed. Trade logged — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}, slippage ${trade['slippage']:.2f}.")
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
                    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_stop_id, stop_price=new_stop_price, entry_time=entry_time, peak_price=peak, entry_strategy="TREND")
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
                            if remaining is None:
                                log("  EXIT UNCONFIRMED — the sell was submitted but no position read confirmed the "
                                    "outcome. State preserved, no trade logged; next cycle re-checks.")
                                pass  # state intentionally untouched: exit unconfirmed
                            elif remaining > 0:
                                log(f"  Warning: {remaining} BTC still held after trail-breach sell — leaving position tracked rather than clearing state.")
                                db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak, entry_strategy="TREND")
                            else:
                                # `candidate` (the chandelier trail level
                                # that was breached) is the natural target
                                # here — it's the price level the strategy
                                # would have moved the stop to, had the
                                # market not already blown through it.
                                fill_price = get_actual_fill_price(order.id, candidate)
                                trade = db.log_trade(SYMBOL, "TREND", entry_price=entry, exit_price=fill_price,
                                                      qty=qty, entry_time=entry_time,
                                                      exit_time=datetime.now(timezone.utc).isoformat(),
                                                      exit_reason="chandelier_trail_breach",
                                                      slippage=abs(fill_price - candidate))
                                log(f"  Trade logged — gross ${trade['gross_pnl']:.2f}, fees ${trade['fees_paid']:.2f}, net ${trade['net_pnl']:.2f}, slippage ${trade['slippage']:.2f}.")
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
                            db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=new_id, stop_price=new_price, entry_time=entry_time, peak_price=peak, entry_strategy="TREND")
                        else:
                            db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=None, stop_price=None, entry_time=entry_time, peak_price=peak, entry_strategy="TREND")
                    else:
                        log("  Couldn't confirm cancellation before raising the trailing stop — will retry next cycle.")
                elif peak != pos_state.get("peak_price"):
                    # No trail update needed yet, but still persist the
                    # new peak so next cycle's activation check is accurate.
                    db.set_position_state(SYMBOL, entry_price=entry, stop_order_id=stop_id, stop_price=current_stop_price, entry_time=entry_time, peak_price=peak, entry_strategy="TREND")
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

        path_a_bought_this_cycle = False
        print_cycle_telemetry(closes, volumes, strategy_label, signal, effective_strategy_name)

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
                            db.set_position_state(SYMBOL, entry_price=latest_price, stop_order_id=stop_id, stop_price=stop_price, entry_time=now_iso, peak_price=latest_price, entry_strategy="TREND")
                            path_a_bought_this_cycle = True
                        else:
                            log("  Buy submitted but no position detected yet — stop will be placed next cycle's reconciliation.")
                    except Exception as e:
                        log(f"  Order failed: {e}")

            elif signal == "sell":
                if qty > 0:
                    entry_price = pos_state.get("entry_price") or latest_price
                    if not is_spread_ok():
                        log("  Sell signal deferred — spread too wide this cycle, will retry next cycle.")
                    elif not is_profit_after_costs(entry_price):
                        pass
                    else:
                        confirmed = cancel_and_confirm(pos_state.get("stop_order_id"))
                        if not confirmed:
                            log("  Warning: couldn't confirm the resting stop was cancelled before selling — proceeding anyway.")
                        try:
                            order = place_market_sell(qty)
                            log(f"  Order submitted automatically: BTC sell — id {order.id}")
                            remaining = verify_sell_filled(qty)
                            if remaining is None:
                                log("  EXIT UNCONFIRMED — the sell was submitted but no position read confirmed the "
                                    "outcome. State preserved, no trade logged; next cycle re-checks.")
                                pass  # state intentionally untouched: exit unconfirmed
                            elif remaining > 0:
                                log(f"  Warning: {remaining} BTC still held after the sell — likely a partial fill. Leaving position tracked for next cycle's reconciliation rather than clearing state.")
                                db.set_position_state(SYMBOL, entry_price=pos_state.get("entry_price"), stop_order_id=None, stop_price=None, entry_time=pos_state.get("entry_time"), peak_price=pos_state.get("peak_price"), entry_strategy="TREND")
                            else:
                                db.clear_position_state(SYMBOL)
                        except Exception as e:
                            log(f"  Order failed: {e}")
                else:
                    log("  No open BTC position to sell — skipping.")

        # --- Entry Path B: Mean-Reversion Scalp (Phase 6) ---
        # Independent of the primary strategy signal above, and only
        # ever considered when we're still flat AND Path A didn't just
        # take an entry this cycle — Path A retains full precedence
        # exactly as it had before this feature existed; this is
        # purely additive, never a replacement or a race against it.
        # Reuses the SAME 3-minute bars already fetched this cycle
        # ("current timeframe", per spec) rather than fetching a
        # separate 15m series — a materially bigger change than what
        # was asked for. No macro-disable toggle was requested, so
        # this gate is unconditional, matching Path A's own always-on
        # macro filter.
        if qty == 0 and not path_a_bought_this_cycle and strategies.ENABLE_BAND_SCALP:
            bb = strategies.bollinger(closes)
            rsi_value = strategies.rsi(closes)
            if bb is None or rsi_value is None:
                log("[STRATEGY: SCALP] Evaluation — not enough closed-bar history yet for BB(20)/RSI(14).")
            else:
                # Unconditional per-cycle line — deliberate, not noise:
                # without this, a quiet cycle (no candidate this bar)
                # would leave zero evidence in the log that the scalp
                # check ran at all, which is exactly what's being
                # verified on first deploy.
                log(f"[STRATEGY: SCALP] Evaluation — RSI {rsi_value:.1f} (enter below {strategies.SCALP_RSI_THRESHOLD}), price ${latest_price:.2f} vs lower BB ${bb['lower']:.2f}")
                latest_low = lows[-1]
                touched_or_closed_below_lower_band = (latest_low <= bb["lower"]) or (latest_price <= bb["lower"])
                rsi_oversold = rsi_value < strategies.SCALP_RSI_THRESHOLD
                if touched_or_closed_below_lower_band and rsi_oversold:
                    log(f"[STRATEGY: SCALP] Candidate — close ${latest_price:.2f} vs lower BB ${bb['lower']:.2f}, RSI {rsi_value:.1f} < {strategies.SCALP_RSI_THRESHOLD}")
                    if not is_macro_uptrend():
                        log("[STRATEGY: SCALP] Skipping — price is below the 1h EMA(200) macro trend filter.")
                    elif not is_spread_ok():
                        pass  # is_spread_ok() already logs the reason
                    else:
                        entry_price_estimate = latest_price
                        if atr_value and atr_value > 0:
                            scalp_stop_price = round(entry_price_estimate - strategies.SCALP_STOP_ATR_MULT * atr_value, 2)
                        else:
                            scalp_stop_price = round(entry_price_estimate * (1 - STOP_LOSS_PCT), 2)
                        scalp_tp1_price = round(entry_price_estimate * (1 + strategies.SCALP_TP_PCT / 100), 2)
                        try:
                            order = place_buy()
                            log(f"[STRATEGY: SCALP] Order submitted: BTC buy — id {order.id}")
                            time.sleep(2)  # let the market order fill before we size the stop/TP
                            filled_qty = get_position_qty()
                            now_iso = datetime.now(timezone.utc).isoformat()
                            if filled_qty > 0:
                                # 50/50 split: the FULL qty is protected
                                # by the stop until Target 1 fills — only
                                # HALF the qty rests as the Target-1 limit
                                # sell. Rounded to 6dp; Alpaca's own min
                                # trade increment (0.0001 for BTC/USD)
                                # governs actual acceptance — a rejection
                                # here is caught, logged, and retried by
                                # next cycle's reconciliation, same as any
                                # other failed order placement in this file.
                                # P0-1 reservation fix: ONLY the full-qty
                                # stop rests. A crypto stop-limit sell
                                # reserves the whole quantity, so a second
                                # resting TP sell is rejected ("insufficient
                                # balance ... available: 0" — cryptobot.log:934).
                                # Target 1 is executed later by
                                # _scalp_execute_target1() as an IOC limit
                                # sell, after the stop is confirmed cancelled.
                                stop_id, stop_price = _submit_stop_limit_sell(filled_qty, scalp_stop_price)
                                db.set_position_state(SYMBOL, entry_price=entry_price_estimate, stop_order_id=stop_id, stop_price=stop_price,
                                                       entry_time=now_iso, peak_price=entry_price_estimate, take_profit_order_id=None,
                                                       take_profit_price=scalp_tp1_price, entry_strategy="SCALP",
                                                       target1_filled=False, original_qty=filled_qty)
                                log(f"[STRATEGY: SCALP] Entry ${entry_price_estimate:.2f} ({filled_qty:.6f} BTC) — full-qty stop ${stop_price:.2f} (1.2x ATR). Target 1 ${scalp_tp1_price:.2f} ({strategies.SCALP_TP_PCT}%) on {_floor_qty(filled_qty / 2):.6f} BTC will be sent as an IOC limit once the bid reaches it (no resting TP: it cannot coexist with the full-qty stop).")
                            else:
                                # Fill not detected within the wait — still
                                # record a SCALP-tagged, price-computed
                                # position_state (no order ids yet) so next
                                # cycle's reconciliation repairs it with the
                                # correct scalp stop/TP prices, rather than
                                # silently defaulting to TREND-style repair
                                # (an untagged row defaults entry_strategy
                                # to "TREND" — see the reconciliation block
                                # above). original_qty is left unset here
                                # since the actual filled qty isn't known
                                # yet — next cycle's repair path re-reads
                                # get_position_qty() directly rather than
                                # depending on it.
                                db.set_position_state(SYMBOL, entry_price=entry_price_estimate, stop_order_id=None, stop_price=scalp_stop_price,
                                                       entry_time=now_iso, peak_price=entry_price_estimate, take_profit_order_id=None,
                                                       take_profit_price=scalp_tp1_price, entry_strategy="SCALP", target1_filled=False)
                                log("[STRATEGY: SCALP] Buy submitted but no position detected yet — protective orders will be placed next cycle's reconciliation.")
                        except Exception as e:
                            log(f"[STRATEGY: SCALP] Order failed: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
