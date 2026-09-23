"""P0-1 regression tests: scalp take-profit state handling and BTC quantity reservation.

Run (fixed code):      /usr/bin/python3 -m unittest tests.test_p0_1_scalp_tp -v
Run against baseline:  BOT_SOURCE=/path/to/baseline/crypto_trading_bot.py /usr/bin/python3 -m unittest tests.test_p0_1_scalp_tp -v

Core invariant under test: Target 1 is recorded ONLY from a confirmed, positive,
cumulative filled quantity reported for the take-profit order. "Order not open"
(rejected, missing, canceled, unknown, still working) is never treated as a fill.
Every test also asserts the fake broker saw no "insufficient balance" rejection,
i.e. the bot never tried to reserve more BTC than it holds.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H  # noqa: E402

ENTRY, ORIG, STOP_PX, TP_PX = 100.0, 1.0, 98.8, 101.5


def phase1(ns, tp_id=None, stop_id="s1"):
    ns["db"].states["BTC/USD"] = dict(
        entry_price=ENTRY, stop_order_id=stop_id, stop_price=STOP_PX,
        entry_time=datetime.now(timezone.utc).isoformat(), peak_price=ENTRY,
        take_profit_order_id=tp_id, take_profit_price=TP_PX, entry_strategy="SCALP",
        target1_filled=False, original_qty=ORIG)


class Base(unittest.TestCase):
    def state(self, ns):
        return ns["db"].states.get("BTC/USD", {})

    def assertNoFalseTarget(self, ns):
        st = self.state(ns)
        self.assertFalse(st.get("target1_filled"), "phase advanced without a confirmed fill")
        self.assertEqual([t for t in ns["db"].trades if str(t.get("exit_reason", "")).startswith("target1")], [],
                         "a Target-1 trade was logged without a confirmed fill")
        for t in ns["db"].trades:
            self.assertGreater(t["qty"], 0, "zero-quantity trade logged")

    def assertNoOversubscription(self, ns):
        self.assertEqual(ns["_broker"].rejections, [], "bot tried to sell/reserve more BTC than available")

    def assertStopsCover(self, ns, qty, max_price=None):
        stops = ns["_broker"].open_stops()
        self.assertAlmostEqual(sum(float(o.qty) for o in stops), qty, places=9,
                               msg=f"open stop qty {[(o.id, o.qty) for o in stops]} != {qty}")
        if max_price is not None:
            for o in stops:
                self.assertLessEqual(float(o.stop_price), max_price)


# ---------------------------------------------------------------------------
# A. Reconciling a take-profit order already recorded in state.
#    Each case fixes the broker's reported order status and cumulative fill.
# ---------------------------------------------------------------------------
class A_TakeProfitStateReconciliation(Base):
    def test_A1_rejected_tp_is_not_a_fill(self):
        ns = H.load_bot(qty=1.0, bid=100.0)
        b = ns["_broker"]
        b.add_order("s1", "stop_limit", 1.0, stop_price=STOP_PX, limit_price=98.3)
        b.add_order("tp1", "limit", 0.5, status="rejected")
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)

    def test_A2_missing_tp_is_not_a_fill(self):
        """The logged incident: TP id None (placement rejected), price below target."""
        ns = H.load_bot(qty=1.0, bid=100.0)
        ns["_broker"].add_order("s1", "stop_limit", 1.0, stop_price=STOP_PX, limit_price=98.3)
        phase1(ns, tp_id=None)
        H.cycle(ns)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)

    def test_A3_canceled_unfilled_tp_is_not_a_fill(self):
        ns = H.load_bot(qty=1.0, bid=100.0)
        b = ns["_broker"]
        b.add_order("s1", "stop_limit", 1.0, stop_price=STOP_PX, limit_price=98.3)
        b.add_order("tp1", "limit", 0.5, status="canceled", filled=0.0)
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)

    def test_A4_partially_filled_still_working_is_not_complete(self):
        """0.2 of 0.5 filled, remainder still working: phase must NOT advance yet,
        and nothing may be submitted against the 0.3 BTC the TP still reserves."""
        ns = H.load_bot(qty=0.8, bid=101.6)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="partially_filled", filled=0.2, avg=TP_PX)
        b.add_order("s1", "stop_limit", 0.5, stop_price=STOP_PX, limit_price=98.3)
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertEqual(b.orders["tp1"].status.value, "partially_filled")

    def test_A5_partial_fill_then_terminal_advances_with_exact_filled_qty(self):
        ns = H.load_bot(qty=0.8, bid=101.6)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="canceled", filled=0.2, avg=101.55)
        b.add_order("s1", "stop_limit", 0.5, stop_price=STOP_PX, limit_price=98.3)
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        st = self.state(ns)
        self.assertTrue(st.get("target1_filled"))
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertEqual(len(t1), 1)
        self.assertAlmostEqual(t1[0]["qty"], 0.2, places=9)
        self.assertAlmostEqual(t1[0]["exit_price"], 101.55, places=6)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.8)

    def test_A6_fully_filled_tp_advances(self):
        ns = H.load_bot(qty=0.5, bid=101.6)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="filled", filled=0.5, avg=TP_PX)
        b.add_order("s1", "stop_limit", 0.5, stop_price=STOP_PX, limit_price=98.3)
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        self.assertTrue(self.state(ns).get("target1_filled"))
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertEqual(len(t1), 1)
        self.assertAlmostEqual(t1[0]["qty"], 0.5, places=9)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.5)

    def test_A7_unknown_tp_state_is_not_a_fill(self):
        ns = H.load_bot(qty=1.0, bid=100.0)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="new")
        b.unknown_ids.add("tp1")
        b.add_order("s1", "stop_limit", 0.5, stop_price=STOP_PX, limit_price=98.3)
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)


# ---------------------------------------------------------------------------
# B. Executing Target 1 when price reaches it (no TP rests beside the full stop).
# ---------------------------------------------------------------------------
class B_TargetExecution(Base):
    def setup(self, bid=101.6, next_tp=("open",)):
        ns = H.load_bot(qty=1.0, bid=bid)
        ns["_broker"].add_order("s1", "stop_limit", 1.0, stop_price=STOP_PX, limit_price=98.3)
        ns["_broker"].next_tp = next_tp
        phase1(ns, tp_id=None)
        H.cycle(ns)
        return ns

    def tp_sells(self, ns):
        return [r for r in ns["_broker"].submitted if r._kind == "limit" and r.side == "sell"]

    def test_B1_rejected_tp_submission_restores_full_protection(self):
        ns = self.setup(next_tp=("reject",))
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)
        self.assertIsNone(self.state(ns).get("take_profit_order_id"))

    def test_B2_fully_filled_tp(self):
        ns = self.setup(next_tp=("filled", 0.5))
        sells = self.tp_sells(ns)
        self.assertEqual(len(sells), 1)
        self.assertGreaterEqual(float(sells[0].limit_price), TP_PX)   # never sells below target
        self.assertEqual(sells[0].time_in_force, "ioc")
        self.assertTrue(self.state(ns).get("target1_filled"))
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertAlmostEqual(t1[0]["qty"], 0.5, places=9)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.5)

    def test_B3_partially_filled_tp_uses_cumulative_fill(self):
        ns = self.setup(next_tp=("partial_then_cancel", 0.2))
        self.assertTrue(self.state(ns).get("target1_filled"))
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertAlmostEqual(t1[0]["qty"], 0.2, places=9)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.8)

    def test_B4_unconfirmed_stop_cancel_blocks_tp_submission(self):
        ns = H.load_bot(qty=1.0, bid=101.6)
        b = ns["_broker"]
        b.add_order("s1", "stop_limit", 1.0, stop_price=STOP_PX, limit_price=98.3)
        b.cancel_fails = True
        phase1(ns, tp_id=None)
        H.cycle(ns)
        self.assertEqual(self.tp_sells(ns), [], "TP submitted while the full-qty stop may still reserve the BTC")
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)

    def test_B5_unknown_tp_outcome_is_not_a_fill_and_remainder_protected(self):
        ns = self.setup(next_tp=("unknown",))
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertIsNotNone(self.state(ns).get("take_profit_order_id"), "in-flight TP id must be persisted")
        self.assertStopsCover(ns, 0.5, max_price=ENTRY)      # unreserved remainder protected

    def test_B6_target_not_reached_submits_nothing(self):
        ns = self.setup(bid=101.4)
        self.assertEqual(self.tp_sells(ns), [])
        self.assertNoFalseTarget(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)


# ---------------------------------------------------------------------------
# C. End-to-end reproduction of the logged incident (cryptobot.log:932-938).
# ---------------------------------------------------------------------------
class C_IncidentReproduction(Base):
    def test_C_entry_does_not_oversubscribe_and_next_cycle_is_not_false_target(self):
        ns = H.load_bot(qty=0.0, bid=100.0)
        b = ns["_broker"]
        s = ns["strategies"]
        s.ENABLE_BAND_SCALP = 1
        s.bollinger = lambda closes, *a, **k: {"lower": 100.5, "middle": 101.0, "upper": 101.5}
        s.rsi = lambda closes, *a, **k: 30.0

        def place_buy(*a, **k):
            b.qty = ORIG
            b.add_order("buy1", "market", ORIG, status="filled", filled=ORIG, avg=ENTRY)
            return b.orders["buy1"]
        ns["place_buy"] = place_buy
        H.cycle(ns)                                   # cycle 1: scalp entry
        self.assertEqual(self.state(ns).get("entry_strategy"), "SCALP")
        self.assertNoOversubscription(ns)             # baseline: TP rejected here
        self.assertStopsCover(ns, ORIG, max_price=ENTRY)
        b.bid = 100.0
        H.cycle(ns)                                   # cycle 2: price below target
        self.assertNoFalseTarget(ns)                  # baseline: "Target 1 hit — sold ~0.000000"
        self.assertStopsCover(ns, ORIG, max_price=ENTRY)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# D. Review follow-up (2026-09-23): the post-Target-1 "breakeven" stop must never
#    be placed at or above the current bid. A stop above the market triggers at
#    once (the mechanism of the 07:31Z incident, where a $86,719 stop sold the
#    whole position immediately), and its limit (entry*1.005*0.995) can sit above
#    a falling bid, leaving the triggered order unfilled and the position with no
#    effective protection. Reachable when a partially filled TP terminates on a
#    LATER cycle after price has dropped back below entry*SCALP_BREAKEVEN_MULT.
# ---------------------------------------------------------------------------
class D_BreakevenStopNeverAboveMarket(Base):
    def _late_terminal_partial(self, bid, quote_available=True):
        ns = H.load_bot(qty=0.8, bid=bid)
        if not quote_available:
            ns["get_live_quote"] = lambda: None
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="canceled", filled=0.2, avg=101.55)
        b.add_order("s1", "stop_limit", 0.5, stop_price=STOP_PX, limit_price=98.3)
        phase1(ns, tp_id="tp1")
        H.cycle(ns)
        return ns

    def test_D1_bid_below_breakeven_keeps_prior_stop_below_market(self):
        ns = self._late_terminal_partial(bid=100.2)          # breakeven would be 100.5
        self.assertTrue(self.state(ns).get("target1_filled"))  # the 0.2 sale was real
        self.assertStopsCover(ns, 0.8, max_price=100.2 - 1e-9)
        self.assertAlmostEqual(self.state(ns)["stop_price"], STOP_PX)
        self.assertNoOversubscription(ns)

    def test_D2_bid_above_breakeven_still_uses_breakeven(self):
        ns = self._late_terminal_partial(bid=101.6)
        self.assertAlmostEqual(self.state(ns)["stop_price"], round(ENTRY * 1.005, 2))
        self.assertStopsCover(ns, 0.8, max_price=101.6)

    def test_D3_no_quote_does_not_guess_breakeven(self):
        ns = self._late_terminal_partial(bid=100.2, quote_available=False)
        self.assertAlmostEqual(self.state(ns)["stop_price"], STOP_PX)
        self.assertStopsCover(ns, 0.8, max_price=100.2 - 1e-9)
