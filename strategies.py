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
RSI_PERIOD = 23                  # was 14 on 5m bars (~70min); rescaled for 3m bars (23*3≈70min)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
BB_PERIOD = 33                   # was 20 on 5m bars (~100min); rescaled for 3m bars (33*3≈100min)
BB_STD = 2
BB_SQUEEZE_BANDWIDTH = 0.04      # squeeze if (upper-lower)/mean < 4%
TREND_SPREAD_THRESHOLD = 0.003   # EMA9 vs EMA21 spread > 0.3% => trending

# ---------- Tier 2: ATR stops + macro trend filter + regime hysteresis ----------
ATR_PERIOD = 23                  # was 14 on 5m bars (~70min); rescaled for 3m bars (23*3≈70min)
STOP_ATR_MULT = 1.5              # stop = entry - 1.5 * ATR
TARGET_ATR_MULT = 3.0            # take-profit = entry + 3.0 * ATR
MACRO_EMA_PERIOD = 200           # higher-timeframe (1h) trend filter
REGIME_CONFIRM_BARS = 2          # bars a new regime must hold before "auto" switches to it

# ---------- Tier 3: volume confirmation, chandelier trail, time-decay exit ----------
RVOL_PERIOD = 33                 # was 20 on 5m bars (~100min); rescaled for 3m bars (33*3≈100min)
TREND_RVOL_THRESHOLD = 1.0
BREAKOUT_RVOL_THRESHOLD = 1.2
RVOL_MULTIPLIER = 1.2            # fallback default when no strategy-specific value applies
# Per-strategy RVOL thresholds (Tier 4): an EMA crossover is a slower
# trend-following signal that doesn't need above-average volume to be
# valid; requiring only "not below average" (1.0x) avoids starving
# legitimate trend entries on ordinary volume days. Reversion signals
# often fire in quieter, choppy conditions, so they get the same
# permissive 1.0x floor as trend. Breakout was originally the
# strictest (1.2x, volume-confirmation being definitional to a real
# breakout) but was found too restrictive in practice on 3-minute
# bars and lowered to 0.8x — now the most permissive of the three,
# not the tightest.
RVOL_MULTIPLIER_BY_STRATEGY = {
    "trend": 1.0,
    "reversion": 1.0,
    "breakout": 0.8,
}
CHANDELIER_ACTIVATION_ATR_MULT = 1.5  # trail only kicks in once profit exceeds 1.5x ATR
CHANDELIER_TRAIL_ATR_MULT = 1.5       # trail sits 1.5x ATR below the peak price since entry
# Phase 7: Momentum Ratchet — once unrealized gain (peak vs entry,
# same basis the trail itself already uses) exceeds this percentage,
# the trail widens from CHANDELIER_TRAIL_ATR_MULT to this wider
# multiplier, giving a big runner more room so an ordinary pullback
# doesn't wick it out. Plain code constants, not LIVE_PARAM_SPECS
# entries — not requested as settings-page fields, so kept fixed
# rather than adding knobs beyond what was asked.
MOMENTUM_RATCHET_GAIN_PCT = 2.5
MOMENTUM_RATCHET_WIDE_TRAIL_MULT = 2.2
# Scalp 50/50 split: once Target 1 (SCALP_TP_PCT) sells the first
# half, the remaining half's stop moves to this multiple of entry —
# 1.005 = entry + 0.5%, covering the spec's "0.50% round-trip fees"
# so the runner half can't turn into a net loss purely from fees if
# it gets stopped immediately after breakeven adjustment. Fixed, not
# a settings field — not requested as one.
SCALP_BREAKEVEN_MULT = 1.005
TIME_DECAY_BARS = 20             # exit at market if no favorable move within 20 bars (60min on 3m bars)

# Explicit top-level declarations for every LIVE_PARAM_SPECS entry
# below, matching every constant above — without these, an attribute
# like strategies.TAKE_PROFIT_PCT or strategies.SCALP_RSI_THRESHOLD
# would only exist AFTER apply_live_params() has run at least once in
# this process (it creates the attribute via globals()[key]=value on
# first call). The bots always call it before reading any of these,
# so this gap was previously harmless in practice — but it's fragile:
# any other code path that imports this module and reads one of these
# names first (a script, a test, a future dashboard handler called
# before refresh_live_params()) would hit an AttributeError. Declaring
# the code default explicitly here, exactly like SHORT_WINDOW/ATR_
# PERIOD/etc. above, closes that gap for good.
TAKE_PROFIT_PCT = 0.0
MAX_POSITION_USD = 500.0
ENABLE_BAND_SCALP = 1
SCALP_TP_PCT = 1.5
SCALP_RSI_THRESHOLD = 35

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


def atr(highs, lows, closes, period=None):
    """Wilder's ATR (same smoothing convention as the RSI above).
    Needs at least period+1 bars of highs/lows/closes; returns None
    if there isn't enough history yet.

    period defaults to the CURRENT value of the module-level
    ATR_PERIOD, read at call time (not bound at def time) — this is
    what lets a live parameter change from the dashboard take effect
    without restarting the bot. See apply_live_params() below."""
    if period is None:
        period = ATR_PERIOD
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


def is_above_macro_trend(closes, period=None):
    """True if the latest price is above the EMA(period) computed on
    the higher-timeframe closes passed in (e.g. 1-hour bars) — the
    macro trend filter for long entries. Returns None (not True/False)
    if there isn't enough history to judge yet; callers should treat
    None as 'can't confirm' and decide their own fail-open/fail-closed
    policy rather than assuming a direction.

    period defaults to the CURRENT MACRO_EMA_PERIOD, read at call
    time — see apply_live_params()."""
    if period is None:
        period = MACRO_EMA_PERIOD
    if len(closes) < period:
        return None
    macro_ema = ema(closes, period)[-1]
    return closes[-1] > macro_ema


def macro_trend_distance_pct(closes, period=None):
    """Percentage distance of the latest close from the macro EMA(period)
    — positive means above (bullish), negative means below (bearish).
    A separate, purely-additive DISPLAY/telemetry number: recomputes
    the same macro_ema is_above_macro_trend() does (deliberately not
    shared/refactored together, so a future change to one can't
    silently change the other's actual trading threshold), but nothing
    in the real entry/exit decision path reads this — it changes
    NOTHING about is_above_macro_trend()'s own behavior. Returns None
    under the same insufficient-history condition."""
    if period is None:
        period = MACRO_EMA_PERIOD
    if len(closes) < period:
        return None
    macro_ema = ema(closes, period)[-1]
    return (closes[-1] - macro_ema) / macro_ema * 100


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


def rvol_ratio(volumes, period=RVOL_PERIOD):
    """The raw relative-volume ratio (latest bar's volume divided by
    the average of the preceding `period` bars) — the actual number
    is_volume_confirmed() computes internally and discards down to a
    boolean. A separate, purely-additive DISPLAY/telemetry number:
    changes NOTHING about is_volume_confirmed()'s own pass/fail
    decision, which any real entry/exit logic still reads exactly as
    before. Returns None under the same insufficient-history
    condition."""
    avg = volume_sma(volumes, period)
    if not avg:
        return None
    return volumes[-1] / avg


def chandelier_stop_price(entry_price, peak_price, atr_value,
                           activation_mult=None,
                           trail_mult=None):
    """Returns the trailing 'chandelier' stop price once price has
    moved favorably at least activation_mult*ATR past entry, else None
    (not yet active — the original fixed/ATR stop should still apply).
    The trail sits trail_mult*ATR below the highest price reached
    since entry (peak_price), so it only ever ratchets up, never down
    — callers must compare against the current resting stop and only
    replace it when this is HIGHER.

    activation_mult/trail_mult default to the CURRENT
    CHANDELIER_ACTIVATION_ATR_MULT/CHANDELIER_TRAIL_ATR_MULT, read at
    call time — see apply_live_params().

    Momentum Ratchet: once unrealized gain (peak vs entry — the same
    basis the activation check above already uses) exceeds
    MOMENTUM_RATCHET_GAIN_PCT, the trail widens to MOMENTUM_RATCHET_
    WIDE_TRAIL_MULT instead of trail_mult, giving a big runner more
    room so an ordinary pullback doesn't wick it out. This can
    LOOSEN what the trail would otherwise be at this peak — the
    caller's own 'only replace if candidate > current resting stop'
    check (see crypto_trading_bot.py) is what keeps the hard
    ratchet-up invariant intact even so: if the narrower trail had
    already tightened past where the wider one would now sit, the
    caller correctly refuses to loosen it. The widening is therefore
    best-effort, bounded by that pre-existing safety rule — which is
    the correct behavior, not a bug to work around."""
    if activation_mult is None:
        activation_mult = CHANDELIER_ACTIVATION_ATR_MULT
    if trail_mult is None:
        trail_mult = CHANDELIER_TRAIL_ATR_MULT
    if not atr_value or atr_value <= 0:
        return None
    if peak_price - entry_price < activation_mult * atr_value:
        return None
    gain_pct = ((peak_price - entry_price) / entry_price * 100) if entry_price else 0
    effective_trail_mult = MOMENTUM_RATCHET_WIDE_TRAIL_MULT if gain_pct > MOMENTUM_RATCHET_GAIN_PCT else trail_mult
    return round(peak_price - effective_trail_mult * atr_value, 2)


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


# ---------- Tier 6: live-adjustable parameters (dashboard settings page) ----------
# Registry of the module-level constants above that the dashboard's
# /settings page exposes for real-time tuning: MA lengths, ATR period
# and multipliers, and the macro-trend filter's EMA period. Scoped
# deliberately narrow — RSI/Bollinger/RVOL/spread-cap/timing constants
# stay code-only, unrelated to this feature and lower-risk left alone.
#
# "Real-time" here means: the bot processes read strategy_params from
# the DB once per loop cycle and call apply_live_params() to update
# these globals in place — no bot restart required, and a change
# takes effect on the NEXT cycle (~one bar interval), not mid-cycle.
LIVE_PARAM_SPECS = {
    "SHORT_WINDOW": {
        "default": 9, "type": int, "min": 2, "max": 100,
        "label": "Short MA length (bars)", "group": "Moving Averages",
    },
    "LONG_WINDOW": {
        "default": 21, "type": int, "min": 3, "max": 300,
        "label": "Long MA length (bars)", "group": "Moving Averages",
    },
    "ATR_PERIOD": {
        "default": 23, "type": int, "min": 2, "max": 100,
        "label": "ATR period (bars)", "group": "ATR",
    },
    "STOP_ATR_MULT": {
        "default": 1.5, "type": float, "min": 0.1, "max": 10.0,
        "label": "Stop-loss ATR multiplier", "group": "ATR",
    },
    "TARGET_ATR_MULT": {
        "default": 3.0, "type": float, "min": 0.1, "max": 20.0,
        "label": "Take-profit ATR multiplier", "group": "ATR",
    },
    "CHANDELIER_ACTIVATION_ATR_MULT": {
        "default": 1.5, "type": float, "min": 0.1, "max": 10.0,
        "label": "Chandelier trail activation (x ATR profit)", "group": "ATR",
    },
    "CHANDELIER_TRAIL_ATR_MULT": {
        "default": 1.5, "type": float, "min": 0.1, "max": 10.0,
        "label": "Chandelier trail distance (x ATR below peak)", "group": "ATR",
    },
    "MACRO_EMA_PERIOD": {
        "default": 200, "type": int, "min": 20, "max": 500,
        "label": "Macro trend filter EMA period (1h bars)", "group": "Macro Trend Filter",
    },
    "RVOL_PERIOD": {
        "default": 33, "type": int, "min": 10, "max": 50,
        "label": "RVOL lookback period (bars)", "group": "Volume / Regime",
    },
    "TREND_RVOL_THRESHOLD": {
        "default": 1.0, "type": float, "min": 0.8, "max": 2.0,
        "label": "Trend RVOL threshold (x average volume)", "group": "Volume / Regime",
    },
    "BREAKOUT_RVOL_THRESHOLD": {
        "default": 1.2, "type": float, "min": 1.0, "max": 3.0,
        "label": "Breakout RVOL threshold (x average volume)", "group": "Volume / Regime",
    },
    "TAKE_PROFIT_PCT": {
        "default": 0.0, "type": float, "min": 0.0, "max": 50.0,
        "label": "Take-profit target (%, 0 = use ATR-based target instead)",
        "group": "Risk & Sizing",
    },
    "MAX_POSITION_USD": {
        "default": 500.0, "type": float, "min": 10.0, "max": 100000.0,
        "label": "Max capital per trade ($)", "group": "Risk & Sizing",
    },
    "ENABLE_BAND_SCALP": {
        "default": 1, "type": int, "min": 0, "max": 1,
        "label": "Enable Mean-Reversion Scalp entry (1 = on, 0 = off)",
        "group": "Mean-Reversion Scalp",
    },
    "SCALP_TP_PCT": {
        "default": 1.5, "type": float, "min": 0.1, "max": 20.0,
        "label": "Scalp Target 1 (%, sells 50% of the position)", "group": "Mean-Reversion Scalp",
    },
    "SCALP_RSI_THRESHOLD": {
        "default": 35, "type": int, "min": 5, "max": 50,
        "label": "Scalp RSI oversold threshold (enter when RSI is below this)",
        "group": "Mean-Reversion Scalp",
    },
}

# Preserves LIVE_PARAM_SPECS's insertion order (Moving Averages, ATR,
# Macro Trend Filter, Risk & Sizing, then Mean-Reversion Scalp) for the
# settings page to render grouped sections in a sensible order without
# re-sorting.
LIVE_PARAM_GROUPS = ["Moving Averages", "ATR", "Macro Trend Filter", "Volume / Regime", "Risk & Sizing", "Mean-Reversion Scalp"]

# The scalp's stop-loss ATR multiplier is deliberately a plain code
# constant, NOT in LIVE_PARAM_SPECS — the requested settings list was
# enable_band_scalp / scalp_tp_pct / scalp_rsi_threshold only, so this
# stays fixed at the spec's 1.2x rather than inventing a fourth
# live-adjustable field beyond what was actually asked for.
SCALP_STOP_ATR_MULT = 1.2


def get_effective_params():
    """Current live value of every registered param (defaults merged
    with whatever apply_live_params() has already applied this
    process)."""
    return {key: globals()[key] for key in LIVE_PARAM_SPECS}


def resolve_effective_params(symbol=None, global_overrides=None, symbol_overrides=None):
    """Pure three-tier resolution — does NOT read or write anything
    itself, doesn't touch module globals, has no dependency on call
    order relative to other symbols. Callers fetch the two override
    dicts (db.get_strategy_params() and, if trading a specific symbol,
    db.get_strategy_params_for_symbol(symbol)) and pass them in.

    Precedence, low to high: code default (LIVE_PARAM_SPECS['default'])
    -> global_overrides -> symbol_overrides (only applied when symbol
    is given). Returns a fully-populated dict — every LIVE_PARAM_SPECS
    key present, always — so the result can be passed straight to
    apply_live_params() with no further merging needed, and it's
    trivial to unit test in isolation from the DB or any live bot
    state.

    This function existing separately from apply_live_params() is
    what makes per-symbol settings possible at all: a single Python
    process (the equity bot) trades several symbols in the same
    cycle, potentially each with different overrides, so 'the live
    value of ATR_PERIOD right now' can't be a single module-level
    global for the whole process — it has to be resolved freshly for
    EACH symbol right before that symbol is processed. apply_live_
    params() still does the actual mutation (so every existing
    call-time-default read pattern in atr()/is_above_macro_trend()/
    chandelier_stop_price() keeps working unchanged); this function
    only decides WHAT to mutate it to."""
    global_overrides = global_overrides or {}
    symbol_overrides = symbol_overrides or {}
    result = {}
    for key, spec in LIVE_PARAM_SPECS.items():
        value = spec["default"]
        if key in global_overrides:
            value = global_overrides[key]
        if symbol and key in symbol_overrides:
            value = symbol_overrides[key]
        result[key] = spec["type"](value)
    return result


def validate_params(overrides):
    """Validates a dict of {PARAM_NAME: raw_value} against
    LIVE_PARAM_SPECS (type/range) plus the one cross-field invariant
    (short MA length must stay below long MA length — an inverted
    crossover is silently meaningless, not just suboptimal). Returns
    (cleaned_dict, errors_list); cleaned_dict only contains entries
    that passed. Unknown keys are rejected rather than silently
    dropped, since a typo'd key from a future UI change should be
    visible, not swallowed."""
    cleaned = {}
    errors = []
    for key, raw_value in overrides.items():
        spec = LIVE_PARAM_SPECS.get(key)
        if not spec:
            errors.append(f"unknown parameter: {key}")
            continue
        try:
            value = spec["type"](raw_value)
        except (TypeError, ValueError):
            errors.append(f"{spec['label']}: not a valid number")
            continue
        if value < spec["min"] or value > spec["max"]:
            errors.append(f"{spec['label']}: must be between {spec['min']} and {spec['max']}")
            continue
        cleaned[key] = value

    if "SHORT_WINDOW" in cleaned or "LONG_WINDOW" in cleaned:
        short = cleaned.get("SHORT_WINDOW", globals()["SHORT_WINDOW"])
        long_ = cleaned.get("LONG_WINDOW", globals()["LONG_WINDOW"])
        if short >= long_:
            errors.append("Short MA length must be less than Long MA length")

    return cleaned, errors


def write_master_spec_snapshot(spec_path="ARCHITECTURE_AND_STRATEGY_SPEC.md"):
    """Write the current active strategy values into the master architecture spec.
    Keeps the runtime snapshot synchronized with the live production configuration.
    """
    from datetime import datetime
    from pathlib import Path

    spec_path = Path(spec_path)
    root = spec_path.parent
    root.mkdir(parents=True, exist_ok=True)

    active_values = [
        ("SHORT_WINDOW", SHORT_WINDOW),
        ("LONG_WINDOW", LONG_WINDOW),
        ("ATR_PERIOD", ATR_PERIOD),
        ("STOP_ATR_MULT", STOP_ATR_MULT),
        ("TARGET_ATR_MULT", TARGET_ATR_MULT),
        ("MACRO_EMA_PERIOD", MACRO_EMA_PERIOD),
        ("RVOL_PERIOD", RVOL_PERIOD),
        ("TREND_RVOL_THRESHOLD", TREND_RVOL_THRESHOLD),
        ("BREAKOUT_RVOL_THRESHOLD", BREAKOUT_RVOL_THRESHOLD),
        ("BB_PERIOD", BB_PERIOD),
        ("RSI_PERIOD", RSI_PERIOD),
        ("SCALP_TP_PCT", SCALP_TP_PCT),
        ("TIME_DECAY_BARS", TIME_DECAY_BARS),
        ("CRYPTO_SPREAD_CAP_PCT", CRYPTO_SPREAD_CAP_PCT),
        ("POSITION_SIZE_USD", globals().get("POSITION_SIZE_USD", 500)),
        ("DAILY_LOSS_LIMIT_USD", globals().get("DAILY_LOSS_LIMIT_USD", 150)),
    ]

    snapshot_lines = [
        "## Auto-generated active runtime snapshot",
        "",
        "This section is refreshed automatically whenever strategy parameters are updated through the project.",
        "",
        f"- Last synced: {datetime.utcnow().strftime('%Y-%m-%d')}",
        "- Source of truth: `strategies.py` + SQLite override tables",
        "- File path: `ARCHITECTURE_AND_STRATEGY_SPEC.md`",
        "",
        "### Current active strategy values",
    ]
    for key, value in active_values:
        snapshot_lines.append(f"- `{key} = {value}`")

    if spec_path.exists():
        original = spec_path.read_text(encoding='utf-8')
        marker = "## Auto-generated active runtime snapshot"
        if marker in original:
            prefix = original.split(marker)[0]
            updated = prefix.rstrip() + "\n\n" + "\n".join(snapshot_lines) + "\n"
        else:
            updated = original.rstrip() + "\n\n" + "\n".join(snapshot_lines) + "\n"
    else:
        updated = "# Master Architecture & Strategy Specification: Alpaca Algorithmic Trading System\n\n" + "\n".join(snapshot_lines) + "\n"

    spec_path.write_text(updated, encoding='utf-8')
    return spec_path


def refresh_master_spec_snapshot(spec_path="ARCHITECTURE_AND_STRATEGY_SPEC.md"):
    return write_master_spec_snapshot(spec_path)


def apply_live_params(overrides, spec_path="ARCHITECTURE_AND_STRATEGY_SPEC.md"):
    """Applies an already-validated dict of {PARAM_NAME: value} onto
    this module's own globals, so every function above that reads
    e.g. ATR_PERIOD or STOP_ATR_MULT as a bare name (or via a
    period=None-style call-time default — see atr(), is_above_macro_
    trend(), chandelier_stop_price() above) picks up the new value on
    its NEXT call, no restart needed. Intended to be called once per
    bot loop cycle with db.get_strategy_params() (which only contains
    explicitly-overridden keys — anything absent keeps whatever this
    function last set it to, or the code default if never overridden).

    Re-casts every value to its LIVE_PARAM_SPECS type on every call —
    NOT optional. sqlite's strategy_params.value column is REAL, so an
    int param like ATR_PERIOD round-trips through the DB as e.g. 14.0;
    left as a float, `trs[:period]` inside atr() raises TypeError
    (list slice indices must be int). Re-casting here, at the single
    point where DB values become live globals, fixes it once for every
    caller instead of pushing int()-casts out to each call site.

    FULLY AUTHORITATIVE, not incremental: every registered key is set
    on every call — either from overrides (if present) or back to its
    spec default (if not). This is deliberate, not an oversight. The
    naive version ("only touch keys present in overrides") looks
    right but has a real bug: once a param has been overridden at
    least once in this process's lifetime, clearing the DB override
    later (e.g. the dashboard's "Reset to Defaults" button, which
    calls db.clear_strategy_params()) makes db.get_strategy_params()
    return {} for that key — and an incremental apply would then
    leave the already-mutated global frozen at its last value
    forever, silently ignoring the reset. Recomputing every key from
    scratch each call closes that hole.

    Silently ignores unknown keys in `overrides` (defensive; callers
    should validate with validate_params() before persisting, so this
    should never see anything invalid in practice)."""
    for key, spec in LIVE_PARAM_SPECS.items():
        if key in overrides:
            globals()[key] = spec["type"](overrides[key])
        else:
            globals()[key] = spec["default"]

    refresh_master_spec_snapshot(spec_path)
