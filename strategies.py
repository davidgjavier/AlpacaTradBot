#!/usr/bin/env python3
"""
Strategy library for crypto_trading_bot.py.

Each strategy function takes a list of closing prices (oldest -> newest,
5-minute bars) and returns "buy", "sell", or None.

  - trend      EMA 9/21 crossover (the bot's original strategy).
               Best in strong directional moves.
  - reversion  RSI(14) mean reversion — buy when RSI crosses back up
               above 30, sell when it crosses back down below 70.
               Best in choppy, sideways markets.
  - breakout   Bollinger Band(20,2) squeeze breakout — buy when price
               breaks above the upper band right after a low-volatility
               squeeze. Built to catch a spike as it ignites.
  - auto       Picks trend / reversion / breakout each cycle based on
               a simple volatility/trend-strength read of the market.

These are standard, documented approaches (EMA crossover, RSI mean
reversion, Bollinger squeeze breakout) — not curve-fit or backtested
against this account's own trade history. Treat "auto" as a
reasonable starting rule, not a guarantee. Watch it for a while
before trusting it unattended.
"""

SHORT_WINDOW = 9
LONG_WINDOW = 21
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
BB_PERIOD = 20
BB_STD = 2
BB_SQUEEZE_BANDWIDTH = 0.04      # squeeze if (upper-lower)/mean < 4%
TREND_SPREAD_THRESHOLD = 0.003   # EMA9 vs EMA21 spread > 0.3% => trending

# ---------- Tier 2: ATR stops + macro trend filter + regime hysteresis ----------
ATR_PERIOD = 14
STOP_ATR_MULT = 1.5              # stop = entry - 1.5 * ATR
TARGET_ATR_MULT = 3.0            # take-profit = entry + 3.0 * ATR
MACRO_EMA_PERIOD = 200           # higher-timeframe (1h) trend filter
REGIME_CONFIRM_BARS = 2          # bars a new regime must hold before "auto" switches to it

# ---------- Tier 3: volume confirmation, chandelier trail, time-decay exit ----------
RVOL_PERIOD = 20
RVOL_MULTIPLIER = 1.2            # fallback default when no strategy-specific value applies
# Per-strategy RVOL thresholds (Tier 4): a Bollinger squeeze breakout
# is only credible WITH a volume surge — that's the definition of a
# breakout — so it keeps the stricter 1.2x bar. An EMA crossover is a
# slower trend-following signal that doesn't need above-average volume
# to be valid; requiring only "not below average" (1.0x) avoids
# starving legitimate trend entries on ordinary volume days. Reversion
# signals often fire in quieter, choppy conditions, so they get the
# same permissive 1.0x floor as trend rather than the breakout bar.
RVOL_MULTIPLIER_BY_STRATEGY = {
    "trend": 1.0,
    "reversion": 1.0,
    "breakout": 1.2,
}
CHANDELIER_ACTIVATION_ATR_MULT = 1.5  # trail only kicks in once profit exceeds 1.5x ATR
CHANDELIER_TRAIL_ATR_MULT = 1.5       # trail sits 1.5x ATR below the peak price since entry
TIME_DECAY_BARS = 12             # exit at market if no favorable move within 12 bars (60min on 5m bars)
# These three are intentionally asset-agnostic. ATR is computed fresh
# per symbol from that symbol's own high/low/close data, so "1.5x ATR"
# already scales to each asset's actual volatility in its own price
# units — that's the point of using ATR instead of a fixed % or dollar
# stop. What would justify a per-symbol OVERRIDE on top of that is a
# known asset-specific behavioral quirk (e.g. NOK gaps more/less than
# its ATR implies) — not asserted here without real evidence. Same
# logic for TIME_DECAY_BARS: it's about how much patience the strategy
# gives a trade, not about volatility (which ATR stops already handle
# separately), so a uniform value across symbols is the defensible
# default absent a specific reason to differ.

# ---------- Tier 3.1: asset-specific microstructure (spread caps) ----------
# Equities trade in $0.01 ticks; a flat spread % cap across NVDA/INTC/
# NOK is wrong because the same % means wildly different tick counts
# at different price levels. NVDA at ~$225 has a 1-tick spread of
# ~0.004% — a 0.5% cap lets through spreads ~100+ ticks wide, nowhere
# near "normal" liquidity. NOK at ~$11 has a 1-tick spread of ~0.09%,
# so the SAME flat cap that's far too loose for NVDA can be uncomfortably
# tight for NOK. Capping in absolute cents (a tick count) instead of a
# single %, calibrated per symbol, fixes both ends at once.
EQUITY_TICK_SIZE = 0.01
EQUITY_SPREAD_CAP_TICKS = {
    "NVDA": 8,   # ~$225 — 8 ticks = $0.08 (~0.035%): tight, matches this name's normal 1-3 tick spread with headroom
    "INTC": 8,   # ~$121 — 8 ticks = $0.08 (~0.066%): similarly liquid, similar treatment
    "NOK": 6,    # ~$11 — 6 ticks = $0.06 (~0.55%): wider in % terms since ticks are proportionally bigger here
}
EQUITY_SPREAD_CAP_TICKS_DEFAULT = 6  # fallback for any symbol not listed above

# Crypto's spread is inherently expressed as a % of price (no fixed
# tick grid the way equities have), so a flat percentage remains the
# right model here — BTC/USD's normal spread on Alpaca's crypto venue
# runs well under this, so it functions as a volatility-spike ceiling,
# not a normal-operating threshold.
CRYPTO_SPREAD_CAP_PCT = 0.003

# Equity intraday volume commonly troughs mid-session — the RVOL gate
# would inconsistently pass/fail through this window based on time of
# day rather than genuine conviction. Rather than invent an unverified
# seasonal RVOL curve, new equity entries are simply blocked during
# this window (exits are unaffected) — see day_trading_bot.py's
# in_midday_lull().
MIDDAY_LULL_START_ET = (11, 30)
MIDDAY_LULL_END_ET = (13, 30)


def max_equity_spread_dollars(symbol):
    """The max bid/ask spread, in dollars, allowed before an equity
    market order is refused — see EQUITY_SPREAD_CAP_TICKS above."""
    ticks = EQUITY_SPREAD_CAP_TICKS.get(symbol, EQUITY_SPREAD_CAP_TICKS_DEFAULT)
    return ticks * EQUITY_TICK_SIZE


def rvol_multiplier_for(strategy_name):
    """Looks up the RVOL threshold for a given strategy label (see
    RVOL_MULTIPLIER_BY_STRATEGY above). Falls back to RVOL_MULTIPLIER
    for any name not in the table."""
    return RVOL_MULTIPLIER_BY_STRATEGY.get(strategy_name, RVOL_MULTIPLIER)


# ---------- Tier 4: EOD flatten, fee-aware exits, look-ahead-safe bars ----------
# How long before the session's OFFICIAL close (from Alpaca's own
# calendar, not a hardcoded wall-clock time — this correctly handles
# early-close days) to flatten all equity positions. This is about
# eliminating OVERNIGHT GAP RISK, not PDT: a same-day forced close
# actually counts as a completed day trade (buy and sell same day),
# so this doesn't reduce day-trade usage — if anything a position that
# would otherwise have been carried overnight and sold the NEXT day
# would NOT have counted as a day trade at all. The justification here
# is purely "don't hold an unhedged position through the closing
# auction and an overnight gap," not PDT avoidance.
EOD_FLATTEN_MINUTES_BEFORE_CLOSE = 10

# Fee-aware exits (non-urgent strategy-reversal sells only — NEVER the
# ATR-stop breach or time-decay exit, which exist specifically to
# guarantee an exit happens and would be undermined by a passive order
# that might not fill) get a resting limit order priced at the current
# ASK, not the bid — a sell limit priced AT the bid crosses the spread
# immediately and fills like a market order, taker fee and all. If it
# hasn't filled after this many cycles, fall back to a market order
# rather than wait indefinitely.
PENDING_EXIT_MAX_CYCLES = 2

VALID_STRATEGIES = ["trend", "reversion", "breakout", "auto"]


def ema(values, span):
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, period=RSI_PERIOD):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_series(values, period=RSI_PERIOD):
    """RSI at each point from `period` onward — used to detect a
    cross back over 30/70, not just a momentary touch."""
    out = []
    for i in range(period + 1, len(values) + 1):
        out.append(rsi(values[:i], period))
    return out


def bollinger(values, period=BB_PERIOD, num_std=BB_STD):
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    std = variance ** 0.5
    upper = mean + num_std * std
    lower = mean - num_std * std
    bandwidth = (upper - lower) / mean if mean else 0
    return {"mean": mean, "upper": upper, "lower": lower, "bandwidth": bandwidth}


def true_range_series(highs, lows, closes):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return trs


def atr(highs, lows, closes, period=ATR_PERIOD):
    """Wilder's ATR (same smoothing convention as the RSI above).
    Needs at least period+1 bars of highs/lows/closes; returns None
    if there isn't enough history yet."""
    trs = true_range_series(highs, lows, closes)
    if len(trs) < period:
        return None
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


# ---------- Individual strategies ----------

def signal_trend(closes):
    """EMA 9/21 crossover — the bot's original approach."""
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


def signal_reversion(closes):
    """RSI(14) mean reversion — buy on a cross back above 30 (oversold
    recovering), sell on a cross back below 70 (overbought rolling
    over)."""
    series = rsi_series(closes)
    if len(series) < 2:
        return None
    prev, curr = series[-2], series[-1]
    if prev <= RSI_OVERSOLD < curr:
        return "buy"
    if prev >= RSI_OVERBOUGHT > curr:
        return "sell"
    return None


def signal_breakout(closes):
    """Bollinger Band(20,2) squeeze breakout — buy when price closes
    above the upper band right after a squeeze (low bandwidth), sell
    when price falls back through the middle band (move exhausted)."""
    if len(closes) < BB_PERIOD + 1:
        return None
    bb_now = bollinger(closes)
    bb_prev = bollinger(closes[:-1])
    if not bb_now or not bb_prev:
        return None
    price = closes[-1]
    was_squeezed = bb_prev["bandwidth"] < BB_SQUEEZE_BANDWIDTH
    broke_upper = price > bb_now["upper"] and closes[-2] <= bb_prev["upper"]
    if was_squeezed and broke_upper:
        return "buy"
    if price < bb_now["mean"] and closes[-2] >= bb_prev["mean"]:
        return "sell"
    return None


def detect_regime(closes):
    """Simple heuristic regime label used by 'auto' mode:
    tight Bollinger bandwidth => 'breakout' (coiled, waiting to pop),
    wide EMA spread => 'trend' (already moving directionally),
    otherwise => 'reversion' (choppy, range-bound)."""
    bb = bollinger(closes)
    if not bb:
        return "trend"
    ema_short = ema(closes, SHORT_WINDOW)[-1]
    ema_long = ema(closes, LONG_WINDOW)[-1]
    spread = abs(ema_short - ema_long) / ema_long if ema_long else 0
    if bb["bandwidth"] < BB_SQUEEZE_BANDWIDTH:
        return "breakout"
    if spread > TREND_SPREAD_THRESHOLD:
        return "trend"
    return "reversion"


def detect_regime_stable(closes, state, confirm_bars=REGIME_CONFIRM_BARS):
    """Hysteresis wrapper around detect_regime(): a freshly-detected
    regime only becomes the ACTIVE one after showing up confirm_bars
    times in a row, so 'auto' mode doesn't flip strategies (and churn
    fees) on a single noisy bar. `state` is a dict with
    current_regime/candidate_regime/candidate_count (as returned by
    db.get_regime_state()); returns (active_regime, new_state) — the
    caller persists new_state back to storage.
    """
    raw = detect_regime(closes)
    current = state.get("current_regime") or raw
    candidate = state.get("candidate_regime")
    candidate_count = state.get("candidate_count") or 0

    if raw == current:
        return current, {"current_regime": current, "candidate_regime": None, "candidate_count": 0}

    if raw == candidate:
        candidate_count += 1
    else:
        candidate = raw
        candidate_count = 1

    if candidate_count >= confirm_bars:
        return candidate, {"current_regime": candidate, "candidate_regime": None, "candidate_count": 0}

    return current, {"current_regime": current, "candidate_regime": candidate, "candidate_count": candidate_count}


def is_above_macro_trend(closes, period=MACRO_EMA_PERIOD):
    """True if the latest price is above the EMA(period) computed on
    the higher-timeframe closes passed in (e.g. 1-hour bars) — the
    macro trend filter for long entries. Returns None (not True/False)
    if there isn't enough history to judge yet; callers should treat
    None as 'can't confirm' and decide their own fail-open/fail-closed
    policy rather than assuming a direction."""
    if len(closes) < period:
        return None
    macro_ema = ema(closes, period)[-1]
    return closes[-1] > macro_ema


def volume_sma(volumes, period=RVOL_PERIOD):
    """Average volume over the `period` bars BEFORE the latest one —
    the latest bar is excluded so a volume spike is compared against
    the baseline that preceded it, not blended into its own average."""
    if len(volumes) < period + 1:
        return None
    window = volumes[-period - 1:-1]
    return sum(window) / period


def is_volume_confirmed(volumes, multiplier=RVOL_MULTIPLIER, period=RVOL_PERIOD):
    """True if the latest bar's volume exceeds `multiplier` times the
    average of the preceding `period` bars — a lightweight relative-
    volume (RVOL) filter to screen out thin-volume breakouts that are
    more prone to failing/whipsawing. Returns None if there isn't
    enough volume history to judge yet."""
    avg = volume_sma(volumes, period)
    if not avg:
        return None
    return volumes[-1] > avg * multiplier


def chandelier_stop_price(entry_price, peak_price, atr_value,
                           activation_mult=CHANDELIER_ACTIVATION_ATR_MULT,
                           trail_mult=CHANDELIER_TRAIL_ATR_MULT):
    """Returns the trailing 'chandelier' stop price once price has
    moved favorably at least activation_mult*ATR past entry, else None
    (not yet active — the original fixed/ATR stop should still apply).
    The trail sits trail_mult*ATR below the highest price reached
    since entry (peak_price), so it only ever ratchets up, never down
    — callers must compare against the current resting stop and only
    replace it when this is HIGHER."""
    if not atr_value or atr_value <= 0:
        return None
    if peak_price - entry_price < activation_mult * atr_value:
        return None
    return round(peak_price - trail_mult * atr_value, 2)


def signal_auto(closes):
    regime = detect_regime(closes)
    if regime == "trend":
        return signal_trend(closes)
    if regime == "breakout":
        return signal_breakout(closes)
    return signal_reversion(closes)


STRATEGIES = {
    "trend": signal_trend,
    "reversion": signal_reversion,
    "breakout": signal_breakout,
    "auto": signal_auto,
}


def get_signal(closes, strategy_name):
    fn = STRATEGIES.get(strategy_name, signal_trend)
    return fn(closes)
