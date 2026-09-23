"""Stage 5b regressions (Codex review of d84cc56): _liquidation_step must RECONCILE before sizing or completing.
1. Position is re-read AFTER own-order reconciliation and open-sell cancellation; the sell is sized from that
   read; an UNKNOWN re-read keeps the liquidation pending and submits nothing.
2. Completion requires a confirmed zero position AND no potentially executable order (own or other) left;
   unknown/unresolved order state stays pending.
Run: /usr/bin/python3 -m unittest discover -s tests -t tests -p 'test_stage5b_*.py' -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H                                   # noqa: E402
from test_stage5_liquidation import (breaker_ns, market_sells, partial_market_sells, pending,  # noqa: E402
                                     SYM)


def stop_fills_during_cancel(broker, oid, fill):
    """Cancelling `oid` races with its execution: `fill` BTC executes, then the order ends canceled."""
    orig = broker.cancel_order_by_id

    def cancel(x):
        o = broker.orders.get(x)
        if x == oid and o is not None and o.status.value in H.OPEN:
            o.filled_qty = str(fill)
            broker.qty -= fill
            o.status = NS(value="canceled")
            return
        return orig(x)
    broker.cancel_order_by_id = cancel


def with_stop(ns, qty=0.5):
    ns["_broker"].add_order("s9", "stop_limit", qty, stop_price=90.0, limit_price=89.9)
    ns["db"].states[SYM]["stop_order_id"] = "s9"


class A_ReReadAfterCancellation(unittest.TestCase):
    def test_fill_during_cancellation_resizes_the_sell(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        with_stop(ns)
        stop_fills_during_cancel(b, "s9", 0.2)
        H.cycle(ns)
        self.assertEqual(b.rejections, [], "sold the pre-cancellation quantity; broker rejected it")
        self.assertEqual([round(float(r.qty), 6) for r in market_sells(b)], [0.3])
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertFalse(pending(ns))

    def test_failed_post_cancel_read_submits_nothing_and_stays_pending(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        with_stop(ns)
        stop_fills_during_cancel(b, "s9", 0.2)
        b.position_fail = lambda k: TimeoutError("read timeout") if k >= 2 else None   # only the cycle-top read works
        H.cycle(ns)
        self.assertEqual(market_sells(b), [], "sold without a confirmed post-cancel position")
        self.assertEqual(b.rejections, [])
        self.assertTrue(pending(ns))
        self.assertEqual(ns["_marks"], [])
        b.position_fail = None
        H.cycle(ns)
        self.assertEqual([round(float(r.qty), 6) for r in market_sells(b)], [0.3])
        self.assertEqual(len(ns["_marks"]), 1)


class B_CompletionRequiresReconciledOrders(unittest.TestCase):
    def pending_with_open_own_sell(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2], remainder="open")     # own sell o1 still working
        H.cycle(ns)
        self.assertTrue(pending(ns))
        b.qty = 0.0                                            # position now reads zero; o1 still OPEN
        return ns, b

    def test_zero_position_with_uncancellable_working_sell_stays_pending(self):
        ns, b = self.pending_with_open_own_sell()
        b.cancel_fails = True
        H.cycle(ns)
        self.assertTrue(pending(ns), "completed while its own sell was still working")
        self.assertEqual(ns["_marks"], [])
        b.cancel_fails = False
        H.cycle(ns)
        self.assertFalse(pending(ns))
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertEqual([o.status.value for o in b.orders.values() if o.kind == "market"], ["canceled"])

    def test_zero_position_with_unknown_own_order_stays_pending(self):
        ns, b = self.pending_with_open_own_sell()
        b.unknown_ids.add(next(o.id for o in b.orders.values() if o.kind == "market"))
        H.cycle(ns)
        self.assertTrue(pending(ns), "completed although its own order state was UNKNOWN")
        self.assertEqual(ns["_marks"], [])

    def test_zero_position_with_other_open_sell_and_unknown_order_list_stays_pending(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2, 0.3])
        H.cycle(ns)                                            # pending, own sell terminal (canceled remainder)
        b.qty = 0.0
        b.add_order("s7", "stop_limit", 0.3, stop_price=90.0, limit_price=89.9)
        b.get_orders = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("orders timeout"))
        H.cycle(ns)
        self.assertTrue(pending(ns), "completed although open orders could not be listed")
        self.assertEqual(ns["_marks"], [])

    def test_zero_position_cancels_and_confirms_leftover_sell_then_completes(self):
        ns, b = self.pending_with_open_own_sell()
        H.cycle(ns)
        self.assertFalse(pending(ns))
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertEqual([o for o in b.open_sells()], [], "completed with an executable sell still open")


if __name__ == "__main__":
    unittest.main()
