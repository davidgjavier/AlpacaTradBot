"""OPTION 1 CANDIDATE (experiment, NOT adopted): positive reconciliation required; no `not_placed` path.
Gated: CANDIDATE_OPT1=1 with BOT_SOURCE=review/candidates/option1/crypto_trading_bot_opt1.py. Fake broker only;
assertions count SUBMISSIONS reaching the broker (broker rejection is never relied on)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p0_1_harness as H                                                          # noqa: E402
from test_stage5_liquidation import breaker_ns, pending, restart, SYM              # noqa: E402
from test_stage5d_visibility import delayed_visibility, market_submissions         # noqa: E402
from test_draft_r1v2_resolution import bound, put, escalated_hidden, escalated_never_sent  # noqa: E402

ON = os.environ.get("CANDIDATE_OPT1") == "1"


def text(ns):
    return H.text(ns)


def buys(b):
    return [r for r in b.submitted if getattr(r, "side", None) == "buy"]


def entries_possible(ns):
    ns["get_today_pl"] = lambda baseline: 0.0
    ns["strategies"].get_signal = lambda *a, **k: "buy"
    ns["strategies"].is_volume_confirmed = lambda *a, **k: True


def new_day(ns):
    ns["db"].baseline.update(day_stamp="2026-09-24", breaker_tripped_stamp=None)   # manual reset state (no clock)


def only_hidden_market(b):
    return next(o for o in b.orders.values() if o.kind == "market")


def cycles(ns, T, times):
    for t in times:
        T[0] = t
        H.cycle(ns)


@unittest.skipUnless(ON, "candidate option 1")
class A_HiddenAcceptedOriginal(unittest.TestCase):
    def test_no_second_sell_no_false_completion_ever(self):
        ns, T = escalated_hidden()
        cycles(ns, T, (1000.0, 5000.0, 90000.0))
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)
        self.assertTrue(pending(ns))
        self.assertEqual(ns["_marks"], [])

    def test_bound_not_placed_assertion_is_rejected_no_second_sell(self):
        """Inverse of the frozen draft's documented risk (R_DocumentedRisk)."""
        ns, T = escalated_hidden()
        put(ns, bound(pending(ns), "not_placed", "opt1-1"))
        cycles(ns, T, (1000.0, 2000.0))
        self.assertEqual(len(market_submissions(ns["_broker"])), 1, "second sell after a not_placed assertion")
        self.assertTrue(pending(ns))
        self.assertEqual(ns["_marks"], [])
        hist = pending(ns)["resolution_history"]
        self.assertTrue(hist[-1]["outcome"].startswith("rejected"), hist)
        self.assertNotIn("authorized_attempt", pending(ns))


@unittest.skipUnless(ON, "candidate option 1")
class B_IntentSavedNeverSent(unittest.TestCase):
    def test_remains_pending_blocks_entries_and_exposes_status(self):
        ns, T = escalated_never_sent()
        entries_possible(ns)
        cycles(ns, T, (1000.0, 2000.0))
        liq = pending(ns)
        self.assertTrue(liq and liq.get("escalated"), "unresolved status not exposed in state")
        self.assertIn("ESCALATED", text(ns))
        self.assertEqual([o for o in ns["_broker"].orders.values() if o.kind == "market"], [])
        self.assertEqual(buys(ns["_broker"]), [], "entry while an attempt is unresolved")
        self.assertEqual(ns["_marks"], [])


@unittest.skipUnless(ON, "candidate option 1")
class C_ZeroPositionUnknownOrder(unittest.TestCase):
    def test_flat_with_never_sent_attempt_is_not_reconciled(self):
        ns, T = escalated_never_sent()
        ns["_broker"].qty = 0.0
        cycles(ns, T, (1000.0, 90000.0))
        self.assertTrue(pending(ns))
        self.assertEqual(ns["_marks"], [])

    def test_flat_with_hidden_attempt_is_not_reconciled(self):
        ns, T = escalated_hidden()
        ns["_broker"].qty = 0.0
        cycles(ns, T, (1000.0, 90000.0))
        self.assertTrue(pending(ns))
        self.assertEqual(ns["_marks"], [])
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)


@unittest.skipUnless(ON, "candidate option 1")
class D_OriginalLaterAppears(unittest.TestCase):
    """The hidden original becomes visible in each terminal/non-terminal state. Positive evidence only."""

    def appear(self, status, filled):
        ns, T = escalated_hidden()
        b = ns["_broker"]
        o = only_hidden_market(b)
        b.reveal(fill=False)
        o.status, o.filled_qty = NS(value=status), str(filled)
        b.qty -= filled
        if filled:
            o.filled_avg_price = str(b.bid)
        cycles(ns, T, (1000.0, 1100.0))
        return ns, b

    def test_working(self):
        ns, b = self.appear("accepted", 0.0)
        self.assertEqual(len(market_submissions(b)), 1)
        self.assertTrue(pending(ns))

    def test_partially_filled_still_working(self):
        ns, b = self.appear("partially_filled", 0.2)
        self.assertEqual(len(market_submissions(b)), 1, "resold while the original can still execute")
        self.assertTrue(pending(ns))

    def test_filled(self):
        ns, b = self.appear("filled", 0.5)
        self.assertEqual(len(market_submissions(b)), 1)
        self.assertFalse(pending(ns))
        self.assertEqual(len(ns["_marks"]), 1)

    def test_partially_filled_then_canceled_sells_only_the_remainder_once(self):
        ns, b = self.appear("canceled", 0.2)
        subs = market_submissions(b)
        self.assertEqual([round(float(r.qty), 6) for r in subs], [0.5, 0.3])
        self.assertFalse(pending(ns))

    def test_canceled_unfilled_resells_once_on_positive_terminal_evidence(self):
        ns, b = self.appear("canceled", 0.0)
        self.assertEqual([round(float(r.qty), 6) for r in market_submissions(b)], [0.5, 0.5])
        self.assertFalse(pending(ns))

    def test_rejected_resells_once_on_positive_terminal_evidence(self):
        ns, b = self.appear("rejected", 0.0)
        self.assertEqual(len(market_submissions(b)), 2)
        self.assertFalse(pending(ns))


@unittest.skipUnless(ON, "candidate option 1")
class E_RestartAndDayRollover(unittest.TestCase):
    """Every unresolved state survives a restart and a (manually installed) new-day state: no sell, no entry."""

    def states(self):
        out = {}
        ns, T = escalated_hidden()
        out["escalated NOT_FOUND_BY_CID (hidden)"] = (ns, T)
        ns, T = escalated_never_sent()
        out["escalated NOT_FOUND_BY_CID (never sent)"] = (ns, T)
        ns = breaker_ns(0.5)
        T = [0.0]
        ns["_r1_now"] = lambda: T[0]
        delayed_visibility(ns["_broker"])
        H.cycle(ns)                                                   # in flight, not yet escalated
        out["in flight (pre-escalation)"] = (ns, T)
        ns, T = escalated_hidden()
        ns["_broker"].unknown_ids.add(only_hidden_market(ns["_broker"]).id)
        ns["_broker"].client_lookup_fails = True
        out["UNKNOWN lookups"] = (ns, T)
        ns = breaker_ns(0.5)
        T = [0.0]
        ns["_r1_now"] = lambda: T[0]
        ns["db"].set_liquidation_state(SYM, reason="r", day_stamp="2026-09-23", attempt=1, order_id=None,
                                       client_order_id=None, started="x")
        H.cycle(ns)
        out["NO_IDENTITY"] = (ns, T)
        return out

    def test_every_unresolved_state(self):
        for label, (ns, T) in self.states().items():
            with self.subTest(label):
                before = len(market_submissions(ns["_broker"]))
                n2 = restart(ns)
                n2["_r1_now"] = lambda: 200000.0
                entries_possible(n2)
                new_day(n2)
                H.cycle(n2)
                H.cycle(n2)
                self.assertEqual(len(market_submissions(n2["_broker"])), before, "sell after restart/rollover")
                self.assertEqual(buys(n2["_broker"]), [], "entry after restart/rollover")
                self.assertTrue(pending(n2), "pending cleared after restart/rollover")


@unittest.skipUnless(ON, "candidate option 1")
class F_ReadFailuresDuringRecovery(unittest.TestCase):
    def test_position_open_orders_and_lookup_failures_take_no_action_then_recover(self):
        ns, T = escalated_hidden()
        b = ns["_broker"]
        b.position_fail = lambda k: TimeoutError("position read timeout")
        cycles(ns, T, (1000.0,))
        orig_get_orders = b.get_orders
        b.position_fail = None
        b.get_orders = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("orders timeout"))
        cycles(ns, T, (1100.0,))
        b.get_orders = orig_get_orders
        b.client_lookup_fails = True
        cycles(ns, T, (1200.0,))
        self.assertEqual(len(market_submissions(b)), 1)
        self.assertTrue(pending(ns))
        b.client_lookup_fails = False
        b.reveal(fill=True)                                           # positive evidence arrives
        cycles(ns, T, (1300.0,))
        self.assertFalse(pending(ns))
        self.assertEqual(len(market_submissions(b)), 1)


@unittest.skipUnless(ON, "candidate option 1")
class G_LateOriginalFillAfterInvestigation(unittest.TestCase):
    def test_operator_assertion_rejected_then_late_fill_completes_with_one_sell(self):
        ns, T = escalated_hidden()
        put(ns, bound(pending(ns), "not_placed", "opt1-late"))       # investigation concluded "never placed" (wrong)
        cycles(ns, T, (1000.0, 2000.0))
        ns["_broker"].reveal(fill=True)                               # the original fills late
        cycles(ns, T, (3000.0,))
        self.assertEqual(len(market_submissions(ns["_broker"])), 1, "a replacement sell was placed")
        self.assertFalse(pending(ns))
        self.assertAlmostEqual(ns["_broker"].qty, 0.0)

    def test_manual_flatten_then_original_reported_rejected_resolves_without_a_new_sell(self):
        ns, T = escalated_hidden()
        b = ns["_broker"]
        b.qty = 0.0                                                   # operator flattened manually at the broker
        cycles(ns, T, (1000.0,))
        self.assertTrue(pending(ns))
        o = only_hidden_market(b)
        b.reveal(fill=False)
        o.status = NS(value="rejected")                               # original could not execute
        cycles(ns, T, (2000.0,))
        self.assertFalse(pending(ns))
        self.assertEqual(len(market_submissions(b)), 1)


if __name__ == "__main__":
    unittest.main()
