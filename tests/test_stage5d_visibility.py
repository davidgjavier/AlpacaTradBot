"""Stage 5d: DELAYED BROKER VISIBILITY (offline fake broker).
A liquidation market sell is ACCEPTED, the response is LOST, and until `reveal()` the order is invisible to
client-id lookup (404 NOT_FOUND), id lookup and open-order listing. Assertions count SUBMISSIONS reaching the
broker; broker rejection is never relied on (the fake reservation check is disabled for hidden orders).

These tests encode the SAFE expectation. On the committed bot they are expected to FAIL (known open gap,
marked expectedFailure so the suite states it explicitly). Against the DRAFT proposal R1
(review/drafts/crypto_trading_bot_R1.py) run with:
  DRAFT_R1=1 BOT_SOURCE=review/drafts/crypto_trading_bot_R1.py python3 -m unittest discover -s tests -t tests -p 'test_stage5d_*.py' -v
they must PASS (expectedFailure is not applied when DRAFT_R1=1).
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H                                            # noqa: E402
from test_stage5_liquidation import breaker_ns, pending, restart, SYM  # noqa: E402

DRAFT = os.environ.get("DRAFT_R1") == "1"
known_gap = (lambda f: f) if DRAFT else unittest.expectedFailure


def delayed_visibility(broker):
    """Next market SELL: accepted (working, unfilled), response lost, invisible until broker.reveal()."""
    hidden = set()
    orig_accept = broker._accept

    def accept(req):
        if getattr(req, "_kind", None) == "market" and req.side == "sell" and not hidden and not broker.__dict__.get("_done"):
            broker._n += 1
            oid = f"o{broker._n}"
            broker.add_order(oid, "market", float(req.qty), status="accepted", filled=0.0,
                             client_order_id=getattr(req, "client_order_id", None))
            hidden.add(oid)
            broker._done = True
            raise TimeoutError("fake broker: order ACCEPTED, response lost")
        return orig_accept(req)
    broker._accept = accept
    # a hidden order must not reserve quantity, so a second sell is NOT stopped by a rejection
    broker.open_sells = lambda: [o for o in broker.orders.values()
                                 if o.status.value in H.OPEN and o.side == "sell" and o.id not in hidden]
    orig_cid, orig_id = broker.get_order_by_client_id, broker.get_order_by_id

    def by_cid(cid):
        for o in broker.orders.values():
            if o.id in hidden and getattr(o, "client_order_id", None) == cid:
                raise H.FakeAPIError(404, 40410000, "order not found for client_order_id")
        return orig_cid(cid)

    def by_id(oid):
        if oid in hidden:
            raise H.FakeAPIError(404, 40410000, "order not found")
        return orig_id(oid)
    broker.get_order_by_client_id, broker.get_order_by_id = by_cid, by_id

    def reveal(fill=True):
        for oid in list(hidden):
            o = broker.orders[oid]
            if fill:
                o.status, o.filled_qty, o.filled_avg_price = NS(value="filled"), str(o.qty), str(broker.bid)
                broker.qty -= float(o.qty)
            hidden.discard(oid)
    broker.reveal = reveal
    return hidden


def market_submissions(b):
    return [r for r in b.submitted if getattr(r, "_kind", None) == "market" and r.side == "sell"]


class V_DelayedVisibility(unittest.TestCase):
    def start(self):
        ns = breaker_ns(0.5)
        delayed_visibility(ns["_broker"])
        H.cycle(ns)                                   # submit accepted; response lost
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)
        self.assertTrue(pending(ns))
        return ns

    @known_gap
    def test_invisible_accepted_sell_is_not_followed_by_a_second_sell(self):
        ns = self.start()
        for _ in range(3):
            H.cycle(ns)
        self.assertEqual(len(market_submissions(ns["_broker"])), 1,
                         "a second sell was submitted under a new client id while the first was invisible")

    @known_gap
    def test_eventual_visibility_completes_with_a_single_sell(self):
        ns = self.start()
        H.cycle(ns)
        ns["_broker"].reveal(fill=True)
        H.cycle(ns)
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)
        self.assertFalse(pending(ns))
        self.assertEqual(len(ns["_marks"]), 1)

    @known_gap
    def test_restart_while_invisible_does_not_resubmit(self):
        ns = self.start()
        n2 = restart(ns)
        H.cycle(n2)
        H.cycle(n2)
        self.assertEqual(len(market_submissions(n2["_broker"])), 1)
        self.assertTrue(pending(n2))

    @known_gap
    def test_invisible_order_filled_position_flat_is_not_completed_until_reconciled(self):
        ns = self.start()
        b = ns["_broker"]
        b.qty = 0.0                                   # position already flat (the hidden sell executed) ...
        H.cycle(ns)                                   # ... but the order itself is still invisible
        self.assertTrue(pending(ns), "completed with its own sell unresolved")
        b.reveal(fill=False)                          # now visible (already accounted as executed)
        o = next(o for o in b.orders.values() if o.kind == "market")
        o.status, o.filled_qty = NS(value="filled"), str(o.qty)
        H.cycle(ns)
        self.assertFalse(pending(ns))
        self.assertEqual(len(market_submissions(b)), 1)


class W_DraftEscalation(unittest.TestCase):
    """Draft R1 only: bounded wait, then ESCALATION (persisted), still no automatic resubmit."""

    @unittest.skipUnless(DRAFT, "draft R1 behavior")
    def test_escalates_after_bound_without_resubmitting(self):
        ns = breaker_ns(0.5)
        delayed_visibility(ns["_broker"])
        H.cycle(ns)
        for _ in range(6):
            ns["_clock"].t += 400                     # > draft window across cycles
            H.cycle(ns)
        liq = pending(ns)
        self.assertTrue(liq and liq.get("escalated"), "no escalation after the visibility bound")
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)


if __name__ == "__main__":
    unittest.main()
