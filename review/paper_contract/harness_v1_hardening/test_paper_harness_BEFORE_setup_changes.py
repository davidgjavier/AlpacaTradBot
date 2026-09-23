"""Offline tests for paper_harness (fake transport only; no broker, credentials or bot code).
Run: cd review/paper_contract/harness_v1 && python3 -m unittest -v test_paper_harness
Labels: NEW FEATURE = harness behavior (no prior implementation exists). DEFECT CHARACTERIZATION = demonstrates the
cleanup rule written in paper plan v1 (candidate_option1_v1/3_PAPER_TEST_PLAN.md), which this revision replaces."""
import os
import tempfile
import unittest

from paper_harness import Harness, LocalRefusal, Rejected, StopCondition

PX = 100_000.0


class Fake:
    """Scriptable fake broker. Counts every submit call. Knobs model the unknowns named in the review."""

    def __init__(self, position=0.0):
        self.pos, self.orders, self.submits, self.cancels = position, {}, [], []
        self.lose_next = False          # accepted, then response lost
        self.reject_next = False
        self.cancel_raises = False
        self.position_fails = False
        self.lookup_fails = False
        self.fill_limits = False        # adverse: a "far away" limit buy fills anyway
        self.market_fill_fraction = 1.0

    def submit_order(self, req):
        self.submits.append(dict(req))
        cid = req["client_order_id"]
        if cid in self.orders:
            raise Rejected("duplicate client_order_id")
        if self.reject_next:
            self.reject_next = False
            raise Rejected("rejected by fake broker")
        o = {"id": f"id-{len(self.submits)}", "client_order_id": cid, "side": req["side"], "qty": req["qty"],
             "status": "new", "filled_qty": 0.0, "symbol": req["symbol"]}   # v1.1 fixture: realistic symbol field
        if req["type"] == "market" or self.fill_limits:
            f = req["qty"] * (self.market_fill_fraction if req["type"] == "market" else 1.0)
            o.update(filled_qty=f, status="filled" if f >= req["qty"] else "canceled")
            self.pos += f if req["side"] == "buy" else -f
        self.orders[cid] = o
        if self.lose_next:
            self.lose_next = False
            raise TimeoutError("response lost after acceptance")
        return dict(o)

    def get_order_by_client_id(self, cid):
        if self.lookup_fails:
            raise TimeoutError("lookup timeout")
        if cid not in self.orders:
            raise LookupError("404 not found")
        return dict(self.orders[cid])

    def cancel_order(self, oid):
        self.cancels.append(oid)
        if self.cancel_raises:
            raise TimeoutError("cancel outcome unknown")
        for o in self.orders.values():
            if o["id"] == oid or o["client_order_id"] == oid:
                if o["status"] in ("new", "accepted", "partially_filled"):
                    o["status"] = "canceled"

    def get_position(self, symbol):
        if self.position_fails:
            raise TimeoutError("position read failed")
        return self.pos

    def market_sells(self):
        return [s for s in self.submits if s["side"] == "sell" and s["type"] == "market"]


def harness(fake, **kw):
    d = tempfile.mkdtemp()
    kw.setdefault("account_key", "paper:test-account")                       # v1.1: required
    return Harness(fake, os.path.join(d, "journal.jsonl"), run_id="t", **kw), os.path.join(d, "journal.jsonl")


class N0_Hygiene(unittest.TestCase):
    """NEW FEATURE."""

    def test_no_default_transport(self):
        with self.assertRaises(ValueError):
            Harness(None, os.path.join(tempfile.mkdtemp(), "j"), run_id="x")

    def test_source_has_no_bot_credential_or_network_imports(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_harness.py")) as fh:
            src = fh.read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"')) and "import" in l)
        for bad in ("dotenv", "alpaca", "crypto_trading_bot", "requests", "urllib", "socket", "http"):
            self.assertNotIn(bad, code)
        self.assertNotIn("os.environ", src)
        self.assertNotIn("getenv", src)


class N1_UnknownOrders(unittest.TestCase):
    """NEW FEATURE."""

    def test_accepted_order_with_lost_response_blocks_everything_including_cleanup(self):
        f = Fake()
        h, _ = harness(f)
        f.lose_next = True
        cid = h.buy(0.0002, PX, otype="market")                   # accepted + filled at the broker, response lost
        f.lookup_fails = True                                     # ...and not findable: genuinely UNKNOWN
        rep = h.run([lambda hh: hh.buy(0.0001, PX, otype="market")], PX)
        self.assertEqual(len(f.submits), 1, "another order was submitted while one was unknown")
        self.assertEqual(f.market_sells(), [], "cleanup sold while an order was unknown")
        self.assertEqual(rep["status"], "UNRESOLVED")
        self.assertIn(cid, rep["unresolved_orders"])
        self.assertTrue(rep["human_reconciliation_required"])

    def test_lost_response_then_positive_reconciliation_unblocks_correctly(self):
        """Counterpart: a lost response later FOUND by client id is positive evidence; cleanup may then proceed."""
        f = Fake()
        h, _ = harness(f)
        f.lose_next = True
        cid = h.buy(0.0002, PX, otype="market")
        self.assertIn(cid, h.unresolved())
        rep = h.cleanup(PX)
        self.assertEqual(h.orders[cid]["state"], "FILLED")
        self.assertEqual(rep["status"], "CLEAN")
        self.assertEqual(len(f.market_sells()), 1)

    def test_unknown_cancellation_blocks_cleanup_sell(self):
        f = Fake(position=0.0)
        h, _ = harness(f)
        h.buy(0.0002, PX, otype="market")                         # owned inventory 0.0002
        h.read_position()
        rest = h.buy(0.0001, PX, otype="limit", limit=50_000.0)    # resting far-below limit buy
        f.cancel_raises = True
        rep = h.cleanup(PX)
        self.assertEqual(f.market_sells(), [], "market-sold after an unconfirmed cancellation")
        self.assertEqual(rep["status"], "UNRESOLVED")
        self.assertIn(rest, rep["unresolved_orders"])

    def test_404_after_lost_response_is_not_resolution(self):
        f = Fake()
        h, _ = harness(f)
        f.lose_next = True
        cid = h.buy(0.0001, PX, otype="limit", limit=50_000.0)
        del f.orders[cid]                                         # broker not (yet) showing it
        h.reconcile()
        self.assertIn(cid, h.unresolved())

    def test_cleanup_never_submits_a_second_sell_while_the_first_is_unknown(self):
        f = Fake()
        h, _ = harness(f)
        h.buy(0.0002, PX, otype="market")
        h.reconcile()
        h.read_position()
        f.lose_next = True
        f.market_fill_fraction = 0.0                              # accepted, not yet executed
        orig_get = f.get_order_by_client_id
        f.get_order_by_client_id = lambda c: (_ for _ in ()).throw(TimeoutError("hidden")) if c.endswith("-2") \
            else orig_get(c)                                      # the cleanup sell stays unfindable
        rep1 = h.cleanup(PX)                                      # cleanup sell accepted, response lost
        f.lookup_fails = True
        rep2 = h.cleanup(PX)                                      # e.g. operator re-runs cleanup
        self.assertEqual(len(f.market_sells()), 1)
        self.assertEqual(rep1["status"], "UNRESOLVED")
        self.assertEqual(rep2["status"], "UNRESOLVED")


class N2_LateFillsAndReads(unittest.TestCase):
    """NEW FEATURE."""

    def test_late_fill_after_terminal_is_an_anomaly_and_stops(self):
        f = Fake()
        h, _ = harness(f)
        cid = h.buy(0.0001, PX, otype="limit", limit=50_000.0)
        h.cancel(cid)
        self.assertEqual(h.orders[cid]["state"], "TERMINAL")
        f.orders[cid]["filled_qty"] = 0.0001                        # late fill reported after "canceled"
        h.orders[cid]["recheck"] = True
        h.reconcile()
        self.assertTrue(h.anomalies)
        rep = h.run([lambda hh: hh.buy(0.0001, PX, otype="market")], PX)
        self.assertEqual(len(f.submits), 1)
        self.assertEqual(rep["status"], "UNRESOLVED")

    def test_failed_position_read_blocks_cleanup_sell(self):
        f = Fake()
        h, _ = harness(f)
        h.buy(0.0002, PX, otype="market")
        f.position_fails = True
        rep = h.cleanup(PX)
        self.assertEqual(f.market_sells(), [])
        self.assertEqual(rep["status"], "UNRESOLVED")
        self.assertIsNone(rep["position"])

    def test_adverse_fill_of_far_limit_is_a_stop_and_cleanup_stays_within_reserve(self):
        f = Fake()
        f.fill_limits = True                                     # far-below limit buy fills anyway
        h, _ = harness(f)

        def step(hh):
            cid = hh.buy(0.0001, PX, otype="limit", limit=50_000.0)
            if hh.orders[cid]["filled"] > 0:
                raise StopCondition("unexpected fill")
        rep = h.run([step], PX)
        self.assertEqual(rep["status"], "CLEAN")
        self.assertEqual(len(f.market_sells()), 1)
        self.assertAlmostEqual(f.pos, 0.0)


class N3_DuplicatesAndRestart(unittest.TestCase):
    """NEW FEATURE."""

    def test_duplicate_callbacks_do_not_double_count(self):
        f = Fake()
        h, _ = harness(f)
        cid = h.buy(0.0002, PX, otype="market")
        for _ in range(3):
            h._apply(cid, f.get_order_by_client_id(cid))
        self.assertAlmostEqual(h.orders[cid]["filled"], 0.0002)

    def test_restart_rebuilds_state_keeps_budget_and_refuses_new_orders_while_unresolved(self):
        f = Fake()
        h, j = harness(f)
        h.buy(0.0001, PX, otype="market")
        f.lose_next = True
        lost = h.buy(0.0001, PX, otype="market")
        h2 = Harness(f, j, run_id="t")                          # new process, same journal
        self.assertEqual(h2.attempts, 2)
        self.assertIn(lost, h2.unresolved())
        with self.assertRaises(LocalRefusal):
            h2.buy(0.0001, PX, otype="market")
        self.assertEqual(len(f.submits), 2)


class N4_BudgetLimitsInterrupt(unittest.TestCase):
    """NEW FEATURE."""

    def test_oversized_quantity_and_notional_are_refused_locally_with_zero_submissions(self):
        f = Fake()
        h, _ = harness(f)
        for q in (1.0, float("nan"), -0.1, True, 0.0):
            with self.subTest(qty=q):
                with self.assertRaises(LocalRefusal):
                    h.buy(q, PX, otype="market")
        self.assertEqual(f.submits, [])

    def test_aggregate_buy_cap(self):
        f = Fake()
        h, _ = harness(f)
        for _ in range(4):
            h.buy(0.00024, PX, otype="market")                   # 4 x $24 = $96
        with self.assertRaises(LocalRefusal):
            h.buy(0.00024, PX, otype="market")                   # would exceed $100
        self.assertEqual(len(f.submits), 4)

    def test_sell_capped_by_confirmed_unreserved_inventory(self):
        f = Fake()
        h, _ = harness(f)
        h.buy(0.0002, PX, otype="market")
        h.reconcile()
        h.read_position()
        h.sell(0.00015, PX, otype="limit", limit=150_000.0)      # $22.50; rests; reserves 0.00015
        with self.assertRaises(LocalRefusal):
            h.sell(0.0001, PX, otype="limit", limit=150_000.0)   # $15, but only 0.00005 unreserved
        q, why = h.p6_partial_probe_qty(1.0, PX, min_qty=0.0001)
        self.assertIsNone(q)
        self.assertIn("INCONCLUSIVE", why)
        self.assertEqual(len([s for s in f.submits if s["side"] == "buy"]), 1, "P6 bought to create size")

    def test_rejected_and_duplicate_submissions_count_against_budget_and_reserve_is_kept(self):
        f = Fake()
        h, _ = harness(f, max_orders=6, cleanup_reserve=2)
        f.reject_next = True
        h.buy(0.0001, PX, otype="limit", limit=50_000.0)          # counted (rejected)
        f.orders["pt-t-2"] = {"id": "x", "client_order_id": "pt-t-2", "status": "new", "filled_qty": 0.0}
        h.buy(0.0001, PX, otype="limit", limit=50_000.0)          # counted (duplicate client id -> rejected)
        h.buy(0.0001, PX, otype="limit", limit=50_000.0)
        h.buy(0.0001, PX, otype="limit", limit=50_000.0)
        with self.assertRaises(LocalRefusal):
            h.buy(0.0001, PX, otype="limit", limit=50_000.0)      # 4 used; 2 reserved for cleanup
        self.assertEqual(h.attempts, 4)
        self.assertEqual(len(f.submits), 4)

    def test_exhausted_cleanup_budget_reports_residual_exposure(self):
        f = Fake()
        h, _ = harness(f, max_orders=3, cleanup_reserve=1)
        h.buy(0.0001, PX, otype="market")
        h.buy(0.0001, PX, otype="market")
        f.market_fill_fraction = 0.5
        h.reconcile()
        h.read_position()
        rep1 = h.cleanup(PX)                                    # uses the single reserved order; half fills
        rep2 = h.cleanup(PX)                                    # nothing left in budget
        self.assertEqual(h.attempts, 3)
        self.assertEqual(rep2["status"], "UNRESOLVED")
        self.assertGreater(rep2["position"], 0.0)
        self.assertTrue(any("budget exhausted" in r for r in rep2["local_refusals"]))

    def test_graceful_ctrl_c_runs_cleanup_under_the_invariant(self):
        f = Fake()
        h, _ = harness(f)

        def step(hh):
            hh.buy(0.0001, PX, otype="market")
            raise KeyboardInterrupt
        rep = h.run([step], PX)
        self.assertEqual(rep["status"], "CLEAN")
        self.assertFalse(rep["forced"])
        self.assertAlmostEqual(f.pos, 0.0)

    def test_second_ctrl_c_during_cleanup_is_forced_and_journal_survives(self):
        f = Fake()
        h, j = harness(f)
        orig = h.cleanup

        def interrupted_cleanup(px):
            raise KeyboardInterrupt
        h.cleanup = interrupted_cleanup

        def step(hh):
            hh.buy(0.0001, PX, otype="market")
            raise KeyboardInterrupt
        rep = h.run([step], PX)
        self.assertTrue(rep["forced"])
        self.assertEqual(rep["status"], "UNRESOLVED")
        self.assertTrue(os.path.getsize(j) > 0)
        h2 = Harness(f, j, run_id="t")
        self.assertEqual(h2.attempts, 1)


class D1_PlanV1CleanupRuleDefect(unittest.TestCase):
    """DEFECT CHARACTERIZATION of paper plan v1's cleanup ('cancel every open order ... market-sell any BTC bought').
    Implemented literally here (not in the harness) to show the defect the v2 plan fixes."""

    @staticmethod
    def v1_cleanup(fake):
        for o in list(fake.orders.values()):
            try:
                fake.cancel_order(o["id"])
            except Exception:
                pass                                            # v1 has no rule for unknown cancellation
        try:
            q = fake.get_position("BTC/USD")
        except Exception:
            return
        if q > 0:
            fake.submit_order({"symbol": "BTC/USD", "side": "sell", "qty": q, "type": "market",
                               "limit_price": None, "client_order_id": f"v1-cleanup-{len(fake.submits)}",
                               "time_in_force": "gtc"})

    def test_v1_rule_sells_while_a_cancellation_is_unknown(self):
        f = Fake()
        h, _ = harness(f)
        h.buy(0.0002, PX, otype="market")
        h.buy(0.0001, PX, otype="limit", limit=50_000.0)          # resting
        f.cancel_raises = True
        self.v1_cleanup(f)
        self.assertEqual(len(f.market_sells()), 1, "v1 rule submitted a market sell despite an unknown cancel")

    def test_v1_rule_resells_after_a_lost_cleanup_response(self):
        f = Fake()
        f.market_fill_fraction = 0.0                             # accepted, not yet executed
        h, _ = harness(f)
        f.pos = 0.0002
        self.v1_cleanup(f)
        self.v1_cleanup(f)                                        # re-run: position still shows inventory
        self.assertEqual(len(f.market_sells()), 2, "v1 rule submitted a second sell while the first was unresolved")


if __name__ == "__main__":
    unittest.main()
