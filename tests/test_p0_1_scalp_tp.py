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
        # CHANGED 2026-09-23 (review of 9fab57e, case 1): a submission exception is
        # UNKNOWN, not a confirmed rejection, so the client-id reference is kept
        # for one more reconciliation (previously asserted None immediately).
        self.assertTrue(str(self.state(ns).get("take_profit_order_id")).startswith("cid:"))
        H.cycle(ns)   # a full cycle later the broker still has no such order -> resolved
        self.assertIsNone(self.state(ns).get("take_profit_order_id"))
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=ENTRY)

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


# ===========================================================================
# Review of 9fab57e (2026-09-23, p01_patch_review.md). Three counterexamples.
# ===========================================================================
def _target_reached(bid=101.6, qty=1.0):
    ns = H.load_bot(qty=qty, bid=bid)
    ns["_broker"].add_order("s1", "stop_limit", qty, stop_price=STOP_PX, limit_price=98.3)
    phase1(ns, tp_id=None)
    return ns


def _restart(ns):
    """Fresh bot namespace (new process) sharing only the broker and persisted DB state."""
    ns2 = H.load_bot()
    b = ns["_broker"]
    ns2["trading_client"], ns2["_broker"], ns2["db"] = b, b, ns["db"]
    ns2["get_live_quote"] = lambda: (b.bid, b.bid + 0.01)
    return ns2


class E_LostSubmissionResponse(Base):
    """Case 1: the broker accepts (and may execute) the Target-1 IOC, but the
    submission response is lost. Must be UNKNOWN -> reconciled by client id,
    never 'rejected', and protection must be sized from the ACTUAL position."""

    def run_lost(self, next_tp):
        ns = _target_reached()
        b = ns["_broker"]
        b.next_tp = next_tp
        b.lose_response_kinds = {"limit"}
        H.cycle(ns)
        return ns, b

    def test_E1_reviewer_case_accepted_and_filled_response_lost(self):
        ns, b = self.run_lost(("filled", 0.5))
        self.assertAlmostEqual(b.qty, 0.5)
        self.assertNoOversubscription(ns)                       # was: stale 1.0 BTC stop rejected
        self.assertStopsCover(ns, 0.5, max_price=b.bid - 1e-9)  # was: 0 protected
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertEqual(len(t1), 1)                            # was: no trade row
        self.assertAlmostEqual(t1[0]["qty"], 0.5, places=9)
        self.assertTrue(self.state(ns).get("target1_filled"))
        self.assertNotIn("REJECTED", H.text(ns))

    def test_E2_accepted_unfilled_response_lost(self):
        ns, b = self.run_lost(("partial_then_cancel", 0.0))
        self.assertAlmostEqual(b.qty, 1.0)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=b.bid - 1e-9)

    def test_E3_accepted_partially_filled_response_lost(self):
        ns, b = self.run_lost(("partial_then_cancel", 0.2))
        self.assertAlmostEqual(b.qty, 0.8)
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertEqual(len(t1), 1)
        self.assertAlmostEqual(t1[0]["qty"], 0.2, places=9)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.8, max_price=b.bid - 1e-9)

    def test_E4_accepted_still_working_response_lost(self):
        ns, b = self.run_lost(("partial_open", 0.1))            # 0.1 filled, 0.4 still reserved
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.5, max_price=b.bid - 1e-9)  # 0.9 held - 0.4 reserved
        self.assertTrue(str(self.state(ns).get("take_profit_order_id")).startswith("cid:"))

    def test_E5_filled_response_lost_and_lookup_down_then_restart_reconciles(self):
        ns = _target_reached()
        b = ns["_broker"]
        b.next_tp, b.lose_response_kinds, b.client_lookup_fails = ("filled", 0.5), {"limit"}, True
        H.cycle(ns)
        # Unknown outcome: no trade, no stale-qty order; ACTUAL 0.5 held & unreserved is protected.
        self.assertEqual(ns["db"].trades, [])
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 0.5, max_price=b.bid - 1e-9)
        ref = self.state(ns).get("take_profit_order_id")
        self.assertTrue(str(ref).startswith("cid:"))            # intent persisted for recovery
        # Process restarts; broker lookups work again.
        b.client_lookup_fails, b.lose_response_kinds = False, set()
        ns2 = _restart(ns)
        H.cycle(ns2)
        t1 = [t for t in ns2["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertEqual(len(t1), 1)
        self.assertAlmostEqual(t1[0]["qty"], 0.5, places=9)
        self.assertTrue(self.state(ns2).get("target1_filled"))
        self.assertNoOversubscription(ns2)
        self.assertStopsCover(ns2, 0.5, max_price=b.bid - 1e-9)

    def test_E6_order_never_reached_broker_keeps_full_protection_then_resolves(self):
        ns = _target_reached()
        b = ns["_broker"]
        b.drop_order_kinds = {"limit"}
        H.cycle(ns)
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)
        self.assertStopsCover(ns, 1.0, max_price=b.bid - 1e-9)
        b.drop_order_kinds = set()
        H.cycle(ns)
        self.assertIsNone(self.state(ns).get("take_profit_order_id"))
        self.assertStopsCover(ns, 1.0, max_price=b.bid - 1e-9)

    def test_E7_unknown_outcome_and_position_unreadable_places_nothing_from_stale_qty(self):
        ns = _target_reached()
        b = ns["_broker"]
        b.next_tp, b.lose_response_kinds, b.client_lookup_fails = ("filled", 0.5), {"limit"}, True
        real_pos = b.get_open_position

        def flaky_position(*a):
            # Reads before the TP submission succeed; reconciliation reads after it time out.
            # (A cycle-START failure is covered separately by H5/H6: as of the P0-2 commit it skips the cycle.)
            if any(r._kind == "limit" for r in b.submitted):
                raise TimeoutError("fake broker: position lookup timed out")
            return real_pos(*a)
        b.get_open_position = flaky_position
        H.cycle(ns)
        self.assertNoOversubscription(ns)                      # no stale 1.0 BTC stop
        self.assertEqual([r for r in b.submitted if r._kind == "stop_limit"], [])
        self.assertIn("PROTECTION STATUS UNKNOWN", H.text(ns))
        self.assertTrue(str(self.state(ns).get("take_profit_order_id")).startswith("cid:"))


class F_FinalStopCandidateValidated(Base):
    """Case 2: the FINAL stop candidate must be below the bid. If even the prior
    floor is breached, exit (risk exit) instead of placing an immediately-
    triggering stop; never widen the floor to make an order admissible."""

    def late_partial(self, bid, prior_stop=STOP_PX, quote=True):
        ns = H.load_bot(qty=0.8, bid=bid)
        if not quote:
            ns["get_live_quote"] = lambda: None
        ns["_broker"].add_order("tp1", "limit", 0.5, status="canceled", filled=0.2, avg=101.55)
        phase1(ns, tp_id="tp1", stop_id=None)
        if prior_stop is None:
            ns["db"].states["BTC/USD"]["stop_price"] = None
        H.cycle(ns)
        return ns, ns["_broker"]

    def assertNoStopAtOrAboveBid(self, ns, b):
        for o in b.open_stops():
            self.assertLess(float(o.stop_price), b.bid)
            self.assertLess(float(o.limit_price), b.bid)

    def test_F1_reviewer_case_bid_below_breakeven_and_prior_stop(self):
        ns, b = self.late_partial(bid=97.0)
        self.assertNoStopAtOrAboveBid(ns, b)                 # was: stop 98.8 / limit 98.31 above bid 97
        mkts = [r for r in b.submitted if r._kind == "market" and r.side == "sell"]
        self.assertEqual(len(mkts), 1)
        self.assertAlmostEqual(float(mkts[0].qty), 0.8)
        self.assertAlmostEqual(b.qty, 0.0)
        self.assertEqual([r for r in b.submitted if r._kind == "stop_limit" and float(r.stop_price) < STOP_PX], [],
                         "floor was widened below the prior stop")
        self.assertIn("RISK EXIT", H.text(ns))
        self.assertNoOversubscription(ns)

    def test_F2_missing_prior_stop_below_breakeven_does_not_invent_floor(self):
        ns, b = self.late_partial(bid=99.0, prior_stop=None)
        self.assertNoStopAtOrAboveBid(ns, b)
        self.assertEqual([r for r in b.submitted if r._kind == "stop_limit"], [])
        self.assertAlmostEqual(b.qty, 0.0)                   # risk exit, no invented lower floor

    def test_F3_risk_exit_rejected_then_recovery_never_places_above_market_stop(self):
        ns = H.load_bot(qty=0.8, bid=97.0)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="canceled", filled=0.2, avg=101.55)
        phase1(ns, tp_id="tp1", stop_id=None)
        b.reject_kinds_once = {"market"}
        H.cycle(ns)
        self.assertNoStopAtOrAboveBid(ns, b)
        self.assertAlmostEqual(b.qty, 0.8)                   # exit rejected: still held, flagged
        self.assertTrue(self.state(ns).get("target1_filled"))
        H.cycle(ns)                                          # recovery cycle, bid still below floor
        self.assertNoStopAtOrAboveBid(ns, b)
        self.assertAlmostEqual(b.qty, 0.0)                   # second risk exit succeeds
        self.assertNoOversubscription(ns)

    def test_F4_price_recovers_above_floor_restores_stop(self):
        ns = H.load_bot(qty=0.8, bid=97.0)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="canceled", filled=0.2, avg=101.55)
        phase1(ns, tp_id="tp1", stop_id=None)
        b.reject_kinds_once = {"market"}
        H.cycle(ns)
        b.bid = 100.0                                        # recovers above the 98.8 floor
        H.cycle(ns)
        self.assertStopsCover(ns, 0.8, max_price=b.bid - 1e-9)
        self.assertAlmostEqual(float(b.open_stops()[0].stop_price), STOP_PX)

    def test_F5_no_quote_places_existing_floor_flagged_unvalidated(self):
        ns, b = self.late_partial(bid=100.2, quote=False)
        self.assertStopsCover(ns, 0.8)
        self.assertAlmostEqual(float(b.open_stops()[0].stop_price), STOP_PX)   # not widened
        self.assertIn("UNVALIDATED", H.text(ns))


class G_FillPriceEvidence(Base):
    """Case 3: confirmed quantity with missing/invalid execution price must not
    become a verified price with zero slippage; protection continues."""

    def filled_without_price(self, avg_raw, status="filled", filled=0.5, qty=0.5):
        ns = H.load_bot(qty=qty, bid=101.6)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status=status, filled=filled)
        b.orders["tp1"].filled_avg_price = avg_raw
        phase1(ns, tp_id="tp1", stop_id=None)
        H.cycle(ns)
        return ns, b

    def check_unconfirmed(self, ns, b, qty_left, filled):
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")]
        self.assertEqual(len(t1), 1)
        self.assertIn("price_unconfirmed", t1[0]["exit_reason"])
        self.assertIsNone(t1[0]["slippage"])                  # was 0.0
        self.assertAlmostEqual(t1[0]["qty"], filled, places=9)
        self.assertIn("UNCONFIRMED", H.text(ns))
        self.assertStopsCover(ns, qty_left, max_price=b.bid - 1e-9)   # protection continues

    def test_G1_reviewer_case_missing_price(self):
        ns, b = self.filled_without_price(None)
        self.check_unconfirmed(ns, b, 0.5, 0.5)

    def test_G2_nan_price(self):
        ns, b = self.filled_without_price("nan")
        self.check_unconfirmed(ns, b, 0.5, 0.5)

    def test_G3_zero_and_negative_price(self):
        for bad in ("0", "-5"):
            ns, b = self.filled_without_price(bad)
            self.check_unconfirmed(ns, b, 0.5, 0.5)

    def test_G4_partial_terminal_missing_price(self):
        ns, b = self.filled_without_price(None, status="canceled", filled=0.2, qty=0.8)
        self.check_unconfirmed(ns, b, 0.8, 0.2)
        self.assertIn("target1_partial_price_unconfirmed",
                      [t["exit_reason"] for t in ns["db"].trades])

    def test_G5_valid_price_still_recorded_exactly(self):
        ns, b = self.filled_without_price("101.57")
        t1 = [t for t in ns["db"].trades if str(t["exit_reason"]).startswith("target1")][0]
        self.assertEqual(t1["exit_reason"], "target1")
        self.assertAlmostEqual(t1["exit_price"], 101.57)
        self.assertAlmostEqual(t1["slippage"], abs(101.57 - TP_PX))


# ===========================================================================
# P0-2 focus (2026-09-23): position-read failures. UNKNOWN must never be
# treated as a confirmed zero: no fictitious close, no state clearing, no new
# entry, no order sized from an arithmetic (unsupported) quantity.
# ===========================================================================
TIMEOUT = lambda: TimeoutError("fake broker: position lookup timed out")  # noqa: E731


def fail_from(n):
    """position_fail predicate: reads 1..n-1 succeed, read n onward time out."""
    return lambda k: TIMEOUT() if k >= n else None


def fail_all():
    return lambda k: TIMEOUT()


def trades(ns, prefix=""):
    return [t for t in ns["db"].trades if str(t.get("exit_reason", "")).startswith(prefix)]


class H_PositionReadFailure(Base):

    def reviewer_sequence(self):
        """1.0 BTC; Target 1 already sold 0.5 (IOC filled; stop was cancelled for it).
        Next cycle sees 0.5 held, order reports 0.5 cumulative, then the position
        lookup inside Target-1 advancement fails (3rd read of the cycle)."""
        ns = H.load_bot(qty=0.5, bid=101.6)
        b = ns["_broker"]
        b.add_order("tp1", "limit", 0.5, status="filled", filled=0.5, avg=101.5)
        phase1(ns, tp_id="tp1", stop_id=None)
        b.position_fail = fail_from(3)
        return ns, b

    # --- the reviewer's sequence ------------------------------------------
    def test_H1_reviewer_sequence_no_double_subtraction_no_clear(self):
        ns, b = self.reviewer_sequence()
        before = dict(self.state(ns))
        H.cycle(ns)
        self.assertGreaterEqual(b.position_calls, 3, "scenario did not reach the failing read")
        st = self.state(ns)
        self.assertTrue(st, "position state was CLEARED while 0.5 BTC is still held")
        self.assertEqual(st.get("take_profit_order_id"), "tp1", "pending order reference lost")
        self.assertFalse(st.get("target1_filled"), "phase advanced without a known position")
        self.assertEqual(trades(ns), [], "trade recorded while position size was unknown")
        self.assertEqual(b.submitted, [], "order submitted/sized from an unsupported quantity")
        self.assertEqual({k: st.get(k) for k in before}, before)
        self.assertIn("POSITION UNKNOWN", H.text(ns))

    def test_H2_recovery_after_reads_return_logs_once_and_protects(self):
        ns, b = self.reviewer_sequence()
        H.cycle(ns)
        b.position_fail = None
        H.cycle(ns)
        t1 = trades(ns, "target1")
        self.assertEqual(len(t1), 1)
        self.assertAlmostEqual(t1[0]["qty"], 0.5, places=9)
        self.assertTrue(self.state(ns).get("target1_filled"))
        self.assertStopsCover(ns, 0.5, max_price=b.bid - 1e-9)
        H.cycle(ns)                                              # idempotent: no second Target-1 row
        self.assertEqual(len(trades(ns, "target1")), 1)
        self.assertNoOversubscription(ns)

    def test_H3_restart_after_failed_read_recovers_from_persisted_state(self):
        ns, b = self.reviewer_sequence()
        H.cycle(ns)
        b.position_fail = None
        ns2 = _restart(ns)
        H.cycle(ns2)
        self.assertEqual(len(trades(ns2, "target1")), 1)
        self.assertTrue(self.state(ns2).get("target1_filled"))
        self.assertStopsCover(ns2, 0.5, max_price=b.bid - 1e-9)

    # --- same-cycle Target-1 execution, then the read fails ---------------
    def test_H4_fill_this_cycle_then_read_fails_no_arithmetic_sizing(self):
        ns = _target_reached()                                   # 1.0 held, bid >= target
        b = ns["_broker"]
        b.next_tp = ("filled", 0.5)
        # reads: #1 cycle start, #2 phase-1 current qty; fail from #3 (after the IOC fill)
        b.position_fail = fail_from(3)
        H.cycle(ns)
        self.assertGreaterEqual(b.position_calls, 3)
        self.assertEqual([r for r in b.submitted if r._kind == "stop_limit"], [],
                         "replacement stop sized from pre-submission arithmetic")
        self.assertEqual(trades(ns), [])
        st = self.state(ns)
        self.assertTrue(st and st.get("take_profit_order_id"), "TP reference must persist")
        self.assertFalse(st.get("target1_filled"))
        b.position_fail = None
        H.cycle(ns)                                              # recovery
        self.assertEqual(len(trades(ns, "target1")), 1)
        self.assertStopsCover(ns, 0.5, max_price=b.bid - 1e-9)
        self.assertNoOversubscription(ns)

    # --- initial-cycle (outer loop) lookup failure ------------------------
    def test_H5_cycle_start_read_fails_state_and_pending_ref_preserved(self):
        ns = H.load_bot(qty=1.0, bid=100.0)
        b = ns["_broker"]
        b.add_order("s1", "stop_limit", 0.5, stop_price=STOP_PX, limit_price=98.3)
        b.add_order("tp1", "limit", 0.5, status="partially_filled", filled=0.0)
        phase1(ns, tp_id="tp1", stop_id="s1")
        before = dict(self.state(ns))
        b.position_fail = fail_all()
        H.cycle(ns)
        self.assertEqual(self.state(ns), before, "state changed/cleared on an unknown position")
        self.assertEqual(ns["db"].trades, [])
        self.assertEqual(b.submitted, [])
        self.assertIn("POSITION UNKNOWN", H.text(ns))
        b.position_fail = None                                   # recovery: normal reconciliation resumes
        H.cycle(ns)
        self.assertEqual(self.state(ns).get("take_profit_order_id"), "tp1")
        self.assertNoFalseTarget(ns)
        self.assertNoOversubscription(ns)

    def _buy_ready(self, ns):
        calls = []
        ns["place_buy"] = lambda: calls.append(1) or H.NS(id="buy1")
        ns["strategies"].get_signal = lambda *a, **k: "buy"
        ns["strategies"].is_volume_confirmed = lambda *a, **k: True
        return calls

    def test_H6_cycle_start_read_fails_blocks_new_entry(self):
        ctrl = H.load_bot(qty=0.0, bid=100.0)                   # positive control: flat & readable -> buys
        ctrl_calls = self._buy_ready(ctrl)
        H.cycle(ctrl)
        self.assertEqual(len(ctrl_calls), 1, "control: entry path not reached")
        ns = H.load_bot(qty=0.5, bid=100.0)                     # really holding 0.5, read times out
        calls = self._buy_ready(ns)
        ns["_broker"].position_fail = fail_all()
        H.cycle(ns)
        self.assertEqual(calls, [], "new entry submitted while position was unknown")

    def test_H7_unstructured_not_found_text_is_not_confirmed_flat(self):
        ns = H.load_bot(qty=0.5, bid=100.0)
        calls = self._buy_ready(ns)
        ns["_broker"].position_fail = lambda k: Exception("upstream proxy: resource not found")
        H.cycle(ns)
        self.assertEqual(calls, [], "free-text 'not found' was treated as a confirmed zero position")

    def test_H8_structured_404_is_confirmed_flat_control(self):
        ns = H.load_bot(qty=0.0, bid=100.0)                     # genuine 404 position does not exist
        ns["db"].states["BTC/USD"] = dict(entry_price=100.0, stop_order_id=None, entry_strategy="TREND")
        H.cycle(ns)
        self.assertEqual(ns["db"].states["BTC/USD"], {}, "confirmed-flat stale state should still clear")

    # --- mid-cycle re-reads -------------------------------------------------
    def test_H9_runner_phase_read_fails_no_fictitious_close(self):
        ns = H.load_bot(qty=0.5, bid=101.6)
        b = ns["_broker"]
        b.add_order("s2", "stop_limit", 0.5, stop_price=100.5, limit_price=100.0)
        ns["db"].states["BTC/USD"] = dict(entry_price=ENTRY, stop_order_id="s2", stop_price=100.5,
                                          entry_time=datetime.now(timezone.utc).isoformat(), peak_price=101.6,
                                          entry_strategy="SCALP", target1_filled=True, original_qty=ORIG)
        before = dict(self.state(ns))
        b.position_fail = fail_from(2)                           # cycle-start OK, runner re-read fails
        H.cycle(ns)
        self.assertGreaterEqual(b.position_calls, 2)
        self.assertEqual(trades(ns), [], "fictitious runner close recorded")
        self.assertEqual(self.state(ns), before)

    def test_H10_trend_stop_check_read_fails_no_fictitious_close(self):
        ns = H.load_bot(qty=0.5, bid=101.6)
        b = ns["_broker"]
        b.add_order("s3", "stop_limit", 0.5, stop_price=99.0, limit_price=98.5, status="canceled")
        ns["db"].states["BTC/USD"] = dict(entry_price=ENTRY, stop_order_id="s3", stop_price=99.0,
                                          entry_time=datetime.now(timezone.utc).isoformat(), peak_price=101.6,
                                          entry_strategy="TREND")
        before = dict(self.state(ns))
        b.position_fail = fail_from(2)
        H.cycle(ns)
        self.assertGreaterEqual(b.position_calls, 2)
        self.assertEqual(trades(ns), [], "fictitious stop-loss close recorded")
        self.assertEqual(self.state(ns), before)
