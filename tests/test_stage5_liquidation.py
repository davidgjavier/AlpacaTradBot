"""Stage 5 regressions: breaker liquidation must persist until flat is CONFIRMED, reconcile outstanding exit
orders before acting (no blind repeat sells), survive restarts, and never size protection/exit orders from a
position quantity whose most recent read FAILED (stale). Deterministic fake broker; no network.

Run: /usr/bin/python3 -m unittest discover -s tests -t tests -p 'test_stage5_*.py' -v
Fixture notes: uses the P0-1 harness; the only harness change is three ADDITIVE MemoryDB methods
(get/set/clear_liquidation_state). Partial market fills are a test-local fake-broker behavior below.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H  # noqa: E402

SYM = "BTC/USD"


def partial_market_sells(broker, fills, remainder="canceled"):
    """Each market SELL fills the next amount in `fills` (clamped to the position). remainder='canceled'
    ends the order terminal with filled_qty < qty; remainder='open' leaves it partially_filled (still working)."""
    orig = broker._accept
    queue = list(fills)

    def accept(req):
        if getattr(req, "_kind", None) == "market" and req.side == "sell":
            broker._n += 1
            oid = f"o{broker._n}"
            fill = min(queue.pop(0) if queue else float(req.qty), float(req.qty), broker.qty)
            status = "filled" if fill >= float(req.qty) - 1e-12 else ("canceled" if remainder == "canceled"
                                                                       else "partially_filled")
            broker.add_order(oid, "market", float(req.qty), status=status, filled=fill, avg=broker.bid,
                             client_order_id=getattr(req, "client_order_id", None))
            broker.qty -= fill
            return NS(id=oid, legs=[])
        return orig(req)
    broker._accept = accept


def market_sells(broker):
    return [r for r in broker.submitted if getattr(r, "_kind", None) == "market" and r.side == "sell"]


def stops_submitted(broker):
    return [r for r in broker.submitted if getattr(r, "_kind", None) == "stop_limit"]


def breaker_ns(qty=0.5):
    ns = H.load_bot(qty=qty, bid=100.0)
    ns["db"].states[SYM] = dict(entry_price=100.0, stop_order_id=None, stop_price=None, entry_time=None,
                                peak_price=100.0, entry_strategy="TREND")
    ns["get_today_pl"] = lambda baseline: -1e9          # daily loss limit breached
    ns["_marks"] = []

    def mark(key, stamp):                                 # realistic: records AND persists today's stamp
        ns["_marks"].append((key, stamp))
        ns["db"].baseline["breaker_tripped_stamp"] = stamp
    ns["db"].mark_breaker_tripped = mark
    return ns


def pending(ns):
    return getattr(ns["db"], "liquidations", {}).get(SYM)


def restart(ns):
    """New process: fresh bot module, SAME persisted DB and SAME broker."""
    n2 = H.load_bot(qty=0.0, bid=ns["_broker"].bid)
    n2["db"], n2["trading_client"], n2["_broker"] = ns["db"], ns["_broker"], ns["_broker"]
    n2["get_today_pl"] = ns["get_today_pl"]
    n2["_marks"] = ns["_marks"]
    n2["log"] = lambda *a: n2["db"].events.append(a)
    return n2


class A_PartialAcrossCycles(unittest.TestCase):
    def test_partial_flatten_keeps_liquidation_pending_until_flat(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2, 0.2, 0.1])
        H.cycle(ns)
        self.assertEqual(ns["_marks"], [], "breaker marked done after a PARTIAL flatten")
        self.assertTrue(pending(ns), "no pending liquidation persisted after a PARTIAL flatten")
        self.assertEqual(stops_submitted(b), [], "a protective stop was placed on the residual, reserving it")
        H.cycle(ns)
        self.assertEqual(ns["_marks"], [])
        H.cycle(ns)
        self.assertEqual(len(ns["_marks"]), 1, "breaker not marked once flat was confirmed")
        self.assertFalse(pending(ns), "liquidation not cleared after confirmed flat")
        self.assertAlmostEqual(b.qty, 0.0)
        self.assertEqual(b.rejections, [], "an oversubscribed sell was attempted")
        self.assertAlmostEqual(sum(float(r.qty) for r in market_sells(b)), 0.5 + 0.3 + 0.1, places=9)

    def test_each_resell_is_sized_from_a_confirmed_read_of_that_cycle(self):
        ns = breaker_ns(0.5)
        partial_market_sells(ns["_broker"], [0.2, 0.2, 0.1])
        for _ in range(3):
            H.cycle(ns)
        self.assertEqual([round(float(r.qty), 6) for r in market_sells(ns["_broker"])], [0.5, 0.3, 0.1])


class B_OutstandingExitOrder(unittest.TestCase):
    def test_working_sell_is_reconciled_not_repeated(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2], remainder="open")          # o1 partially filled, still working
        H.cycle(ns)
        H.cycle(ns)
        self.assertEqual(len(market_sells(b)), 1, "a second sell was sent while the first was still working")
        self.assertEqual(b.rejections, [])
        self.assertEqual(stops_submitted(b), [])
        self.assertEqual(ns["_marks"], [])
        o1 = next(o for o in b.orders.values() if o.kind == "market")
        o1.status, o1.filled_qty = NS(value="filled"), str(o1.qty)
        b.qty = 0.0
        H.cycle(ns)
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertFalse(pending(ns))
        self.assertEqual(len(market_sells(b)), 1)

    def test_other_open_sells_are_cancelled_and_confirmed_before_reselling(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        b.add_order("s9", "stop_limit", 0.5, stop_price=90.0, limit_price=89.9)
        ns["db"].states[SYM]["stop_order_id"] = "s9"
        partial_market_sells(b, [0.2, 0.3])
        H.cycle(ns)
        H.cycle(ns)
        self.assertEqual(b.rejections, [], "sold while a stop still reserved the quantity")
        self.assertEqual(b.orders["s9"].status.value, "canceled")
        self.assertEqual(len(ns["_marks"]), 1)


class C_RestartRecovery(unittest.TestCase):
    def test_pending_liquidation_survives_restart(self):
        ns = breaker_ns(0.5)
        partial_market_sells(ns["_broker"], [0.2, 0.3])
        H.cycle(ns)
        self.assertTrue(pending(ns))
        n2 = restart(ns)
        H.cycle(n2)
        self.assertEqual(len(n2["_marks"]), 1)
        self.assertFalse(pending(n2))
        self.assertAlmostEqual(n2["_broker"].qty, 0.0)

    def test_restart_after_submit_before_id_persisted_does_not_double_sell(self):
        """Crash between submitting the liquidation sell and recording its id: the order is found by its
        deterministic client id and reconciled; no second sell while it is working."""
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2], remainder="open")
        H.cycle(ns)
        liq = pending(ns)
        self.assertTrue(liq and liq.get("client_order_id"), "liquidation intent has no client order id")
        liq = dict(liq)
        liq["order_id"] = None                                    # simulate the lost id write
        ns["db"].liquidations[SYM] = liq
        n2 = restart(ns)
        H.cycle(n2)
        self.assertEqual(len(market_sells(b)), 1, "restart re-sold while the unrecorded sell was working")


class D_IntermittentPositionReads(unittest.TestCase):
    def test_success_then_failed_read_is_not_a_quantity(self):
        """verify_sell_filled: a good read (0.3) followed only by failures must return UNKNOWN (None)."""
        ns = H.load_bot(qty=0.3, bid=100.0)
        ns["_broker"].position_fail = lambda k: H.FakeAPIError(500, 50000000, "timeout") if k >= 2 else None
        self.assertIsNone(ns["verify_sell_filled"](0.5), "stale 0.3 returned after the latest read failed")

    def test_stale_read_does_not_size_a_protective_stop_after_partial(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2])
        b.position_fail = lambda k: TimeoutError("read timeout") if k >= 3 else None   # top read + 1 verify read ok
        H.cycle(ns)
        self.assertEqual(stops_submitted(b), [], "a stop was sized from a stale position read")
        self.assertEqual(ns["_marks"], [])
        self.assertTrue(pending(ns))

    def test_unreadable_position_while_pending_takes_no_action_and_no_entry(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2, 0.3])
        H.cycle(ns)
        n_before = len(b.submitted)
        b.position_fail = lambda k: TimeoutError("read timeout")
        H.cycle(ns)
        self.assertEqual(len(b.submitted), n_before, "acted without a readable position")
        self.assertTrue(pending(ns))
        b.position_fail = None
        H.cycle(ns)
        self.assertEqual(len(ns["_marks"]), 1, "not completed once reads recovered")

    def test_eventual_confirmed_flat_after_failures_completes(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2], remainder="open")
        H.cycle(ns)
        o1 = next(o for o in b.orders.values() if o.kind == "market")
        o1.status, o1.filled_qty = NS(value="filled"), str(o1.qty)
        b.qty = 0.0
        b.position_fail = lambda k: TimeoutError("read timeout")
        H.cycle(ns)
        self.assertEqual(ns["_marks"], [], "flat assumed without a confirmed read")
        b.position_fail = None
        H.cycle(ns)
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertFalse(pending(ns))


class E_DayRollover(unittest.TestCase):
    def test_liquidation_continues_after_day_rollover_and_blocks_entries(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        partial_market_sells(b, [0.2, 0.3])
        H.cycle(ns)
        ns["get_today_pl"] = lambda baseline: 0.0                # new day: breaker no longer tripped
        ns["strategies"].get_signal = lambda *a, **k: "buy"
        H.cycle(ns)
        self.assertAlmostEqual(b.qty, 0.0, msg="residual not liquidated after the day rolled over")
        self.assertFalse([r for r in b.submitted if getattr(r, "side", None) == "buy"], "entry while liquidating")


if __name__ == "__main__":
    unittest.main()
