"""P0-4 regression tests: entry buys carry a client order id, never duplicate an
unresolved entry, and never treat an unconfirmed submit as "nothing happened".

Run: /usr/bin/python3 -m unittest tests.test_p0_4_entry_identity -v
Offline only: the p0_1 harness blocks sockets and never imports the bot module.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H  # noqa: E402


class BuyBroker(H.Broker):
    """Adds what the P0-1 fake broker lacks for entries: notional market BUYs that
    can stay open (unfilled), and get_orders() returning open orders of both sides."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.buy_fills = True          # False: the market buy rests open (not yet filled)
        self.orders_fail = False       # get_orders raises (open orders unknown)

    def _accept(self, req):
        if req._kind == "market" and req.side == "buy":
            self._n += 1
            oid = f"o{self._n}"
            status = "filled" if self.buy_fills else "new"
            self.orders[oid] = NS(id=oid, kind="market", side="buy", qty=None, notional=req.notional,
                                  status=NS(value=status), filled_qty="0",
                                  filled_avg_price=None, client_order_id=getattr(req, "client_order_id", None))
            if self.buy_fills:
                self.qty += req.notional / self.bid
            return NS(id=oid, legs=[])
        return super()._accept(req)

    def get_orders(self, *a, **k):
        if self.orders_fail:
            raise TimeoutError("fake broker: open-orders read timed out")
        return [o for o in self.orders.values() if o.status.value in H.OPEN]

    def buys(self):
        return [r for r in self.submitted if r._kind == "market" and r.side == "buy"]


def load(**broker_kw):
    ns = H.load_bot()
    b = BuyBroker(**broker_kw)
    ns["trading_client"], ns["_broker"] = b, b
    return ns, b


class EntryIdentity(unittest.TestCase):
    def test_normal_entry_carries_client_order_id(self):
        ns, b = load()
        order, outcome = ns["submit_entry_buy"]("T")
        self.assertEqual(outcome, "submitted")
        self.assertIsNotNone(order)
        self.assertEqual(len(b.buys()), 1)
        self.assertTrue(b.buys()[0].client_order_id.startswith("p04e-"))

    def test_unfilled_entry_blocks_a_second_buy(self):
        # Audit case "two BTC buys from one unfilled entry".
        ns, b = load()
        b.buy_fills = False
        self.assertEqual(ns["submit_entry_buy"]("T")[1], "submitted")
        order, outcome = ns["submit_entry_buy"]("T")
        self.assertEqual(outcome, "pending")
        self.assertIsNone(order)
        self.assertEqual(len(b.buys()), 1, "a second entry was sent while the first was still open")

    def test_unreadable_open_orders_skip_entry(self):
        ns, b = load()
        b.orders_fail = True
        self.assertEqual(ns["submit_entry_buy"]("T"), (None, "unknown"))
        self.assertEqual(b.buys(), [])

    def test_lost_response_is_adopted_by_client_id(self):
        ns, b = load()
        b.lose_response_kinds = {"market"}
        order, outcome = ns["submit_entry_buy"]("T")
        self.assertEqual(outcome, "submitted")
        self.assertEqual(order.client_order_id, b.buys()[0].client_order_id)
        self.assertEqual(len(b.buys()), 1)

    def test_dropped_request_is_confirmed_not_placed(self):
        ns, b = load()
        b.drop_order_kinds = {"market"}
        self.assertEqual(ns["submit_entry_buy"]("T"), (None, "not_placed"))
        self.assertEqual(b.orders, {})

    def test_uncertain_submit_is_not_retried(self):
        ns, b = load()
        b.lose_response_kinds = {"market"}
        b.client_lookup_fails = True
        self.assertEqual(ns["submit_entry_buy"]("T"), (None, "uncertain"))
        self.assertEqual(len(b.buys()), 1, "exactly one submit attempt; no blind retry")

    def test_uncertain_then_next_cycle_sees_the_open_order(self):
        ns, b = load()
        b.buy_fills = False
        b.lose_response_kinds = {"market"}
        b.client_lookup_fails = True
        self.assertEqual(ns["submit_entry_buy"]("T")[1], "uncertain")
        b.lose_response_kinds, b.client_lookup_fails = set(), False
        self.assertEqual(ns["submit_entry_buy"]("T")[1], "pending")
        self.assertEqual(len(b.buys()), 1)


class SellIdentity(unittest.TestCase):
    def test_unreadable_position_after_sell_is_not_sold(self):
        # Before the fix, get_position_qty() mapped the read error to 0.0 and
        # verify_sell_filled reported "fully filled" -> state cleared, stop gone.
        ns, b = load(qty=0.5)
        b.position_fail = lambda n: TimeoutError("position read timed out")
        self.assertEqual(ns["verify_sell_filled"](0.5, timeout_s=2), 0.5)

    def test_confirmed_flat_after_sell_is_sold(self):
        ns, b = load(qty=0.0)
        self.assertEqual(ns["verify_sell_filled"](0.5, timeout_s=2), 0.0)

    def test_sell_uses_client_id_and_adopts_lost_response(self):
        ns, b = load(qty=0.5)
        b.lose_response_kinds = {"market"}
        order = ns["place_market_sell"](0.5)
        sells = [r for r in b.submitted if r._kind == "market" and r.side == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertTrue(sells[0].client_order_id.startswith("p04s-"))
        self.assertEqual(order.client_order_id, sells[0].client_order_id)

    def test_sell_never_placed_still_raises(self):
        ns, b = load(qty=0.5)
        b.drop_order_kinds = {"market"}
        with self.assertRaises(TimeoutError):
            ns["place_market_sell"](0.5)


if __name__ == "__main__":
    unittest.main()
