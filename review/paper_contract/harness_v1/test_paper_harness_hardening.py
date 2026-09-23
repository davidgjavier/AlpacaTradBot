"""Hardening tests for paper_harness (Codex review of 4ee799d). Offline; fake transport only.
Labels:
  R = CODEX-REPRODUCED DEFECT (written to run on the pre-fix harness: only constructor args it accepts are used).
  H = HARDENING of static concerns (uses the intended API; on the pre-fix harness these fail/error BY DESIGN).
Run: python3 -m unittest -v test_paper_harness_hardening"""
import inspect
import math
import os
import tempfile
import unittest

import paper_harness as PH
from paper_harness import Harness, LocalRefusal
from test_paper_harness import Fake, PX

NAN, INF = float("nan"), float("inf")


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def make(fake, journal=None, **kw):
    """Pass only kwargs the harness under test accepts (so R tests also run on the pre-fix harness)."""
    journal = journal or os.path.join(tempfile.mkdtemp(), "journal.jsonl")
    params = inspect.signature(Harness.__init__).parameters
    kw = {k: v for k, v in kw.items() if k in params}
    if "account_key" in params and "account_key" not in kw:
        kw["account_key"] = "paper:test-account"
    return Harness(fake, journal, run_id="t", **kw), journal


def refused_without_submission(tc, fake, fn):
    n = len(fake.submits)
    with tc.assertRaises(LocalRefusal):
        fn()
    tc.assertEqual(len(fake.submits), n, "the transport was called")


class R1_UnknownPositionBlocksSubmission(unittest.TestCase):
    def test_buy_with_unknown_position_is_refused(self):
        f = Fake()
        h, _ = make(f)
        h.position = None
        refused_without_submission(self, f, lambda: h.buy(0.0001, PX, otype="market"))

    def test_buy_after_failed_position_read_is_refused(self):
        f = Fake()
        h, _ = make(f)
        f.position_fails = True
        h.read_position()
        refused_without_submission(self, f, lambda: h.buy(0.0001, PX, otype="market"))


class R2_NonFiniteNumbers(unittest.TestCase):
    def test_price_limit_and_notional_must_be_finite_positive_non_boolean(self):
        for label, call in {"nan price": lambda h: h.buy(0.0001, NAN, otype="market"),
                            "inf price": lambda h: h.buy(0.0001, INF, otype="market"),
                            "zero price": lambda h: h.buy(0.0001, 0.0, otype="market"),
                            "negative price": lambda h: h.buy(0.0001, -1.0, otype="market"),
                            "bool price": lambda h: h.buy(0.0001, True, otype="market"),
                            "nan limit": lambda h: h.buy(0.0001, PX, otype="limit", limit=NAN),
                            "bool limit": lambda h: h.buy(0.0001, PX, otype="limit", limit=True),
                            "negative limit": lambda h: h.buy(0.0001, PX, otype="limit", limit=-5.0)}.items():
            with self.subTest(label):
                f = Fake()
                h, _ = make(f)
                if hasattr(h, "refresh"):
                    h.refresh()
                else:
                    h.read_position()
                refused_without_submission(self, f, lambda: call(h))


class R3_DeadlineAtSubmissionBoundary(unittest.TestCase):
    def test_direct_call_after_test_deadline_is_refused(self):
        f = Fake()
        c = Clock(0.0)
        h, _ = make(f, max_wall_s=1800.0, clock=c, wall=c)
        h.read_position()
        c.t = 1900.0
        refused_without_submission(self, f, lambda: h.buy(0.0001, PX, otype="market"))


class H1_ReturnedOrderIdentity(unittest.TestCase):
    def test_mismatched_returned_identity_is_an_anomaly_and_state_stays_unknown(self):
        for field, bad in (("client_order_id", "someone-else"), ("symbol", "ETH/USD"), ("side", "sell")):
            with self.subTest(field):
                f = Fake()
                h, _ = make(f)
                h.refresh()
                f.lose_next = True
                cid = h.buy(0.0001, PX, otype="market")
                f.orders[cid][field] = bad
                if field == "symbol":
                    f.orders[cid]["symbol"] = bad
                h.reconcile()
                self.assertIn(cid, h.unresolved(), "a mismatched broker order was accepted as ours")
                self.assertTrue(h.anomalies)


class H2_CumulativeFillValidation(unittest.TestCase):
    def test_invalid_filled_qty_is_an_anomaly_and_never_zeroes_state(self):
        for bad in (NAN, INF, -0.0001, True, "abc", 0.0005):             # 0.0005 > order qty 0.0001
            with self.subTest(bad=bad):
                f = Fake()
                h, _ = make(f)
                h.refresh()
                f.lose_next = True
                cid = h.buy(0.0001, PX, otype="market")
                f.orders[cid]["filled_qty"] = bad
                h.reconcile()
                self.assertIn(cid, h.unresolved())
                self.assertTrue(h.anomalies)
                self.assertTrue(math.isfinite(h.orders[cid]["filled"]) and h.orders[cid]["filled"] >= 0)


class H3_BooleanPosition(unittest.TestCase):
    def test_boolean_position_is_invalid(self):
        f = Fake()
        f.pos = True
        h, _ = make(f)
        h.read_position()
        self.assertIsNone(h.position)
        refused_without_submission(self, f, lambda: h.buy(0.0001, PX, otype="market"))


class H4_JournalBinding(unittest.TestCase):
    def test_replay_under_different_run_account_or_symbol_is_refused(self):
        f = Fake()
        h, j = make(f)
        h.refresh()
        h.buy(0.0001, PX, otype="market")
        h.close()
        for label, kw in {"run": dict(run_id="other"), "account": dict(account_key="paper:OTHER"),
                          "symbol": dict(symbol="ETH/USD")}.items():
            with self.subTest(label):
                args = dict(run_id="t", account_key="paper:test-account", symbol="BTC/USD")
                args.update(kw)
                with self.assertRaises(ValueError):
                    Harness(f, j, **args)

    def test_restart_keeps_the_original_deadline(self):
        f = Fake()
        c = Clock(1000.0)
        h, j = make(f, max_wall_s=1800.0, clock=c, wall=c)
        h.refresh()
        h.close()
        c.t = 1000.0 + 1900.0
        h2 = Harness(f, j, run_id="t", account_key="paper:test-account", max_wall_s=1800.0, clock=c, wall=c)
        h2.refresh()
        refused_without_submission(self, f, lambda: h2.buy(0.0001, PX, otype="market"))

    def test_single_writer(self):
        f = Fake()
        h, j = make(f)
        with self.assertRaises(ValueError):
            Harness(f, j, run_id="t", account_key="paper:test-account")
        h.close()
        Harness(f, j, run_id="t", account_key="paper:test-account").close()


class H5_StalePositionAfterFills(unittest.TestCase):
    def test_sell_sized_from_a_pre_fill_read_is_refused_until_re_read(self):
        f = Fake()
        h, _ = make(f)
        h.refresh()
        h.buy(0.0002, PX, otype="market")                         # fills; the earlier read is now stale
        refused_without_submission(self, f, lambda: h.sell(0.0001, PX, otype="limit", limit=150_000.0))
        h.refresh()
        h.sell(0.0001, PX, otype="limit", limit=150_000.0)
        self.assertEqual(len(f.submits), 2)


class H6_CleanupAllowanceAndPersistence(unittest.TestCase):
    def test_cleanup_has_a_bounded_allowance_after_the_test_deadline(self):
        f = Fake()
        c = Clock(0.0)
        h, _ = make(f, max_wall_s=1800.0, cleanup_allowance_s=300.0, clock=c, wall=c)
        h.refresh()
        h.buy(0.0002, PX, otype="market")
        c.t = 1850.0                                              # past the test deadline, within the allowance
        rep = h.cleanup(PX)
        self.assertEqual(rep["status"], "CLEAN")
        self.assertAlmostEqual(f.pos, 0.0)

    def test_cleanup_after_the_allowance_is_refused_and_unresolved(self):
        f = Fake()
        c = Clock(0.0)
        h, _ = make(f, max_wall_s=1800.0, cleanup_allowance_s=300.0, clock=c, wall=c)
        h.refresh()
        h.buy(0.0002, PX, otype="market")
        c.t = 2200.0
        rep = h.cleanup(PX)
        self.assertEqual(rep["status"], "UNRESOLVED")
        self.assertEqual(f.market_sells(), [])

    def test_journal_write_failure_refuses_without_submitting_or_phantom_state(self):
        f = Fake()
        h, j = make(f)
        h.refresh()
        orig = h._j

        def fail_on_attempt(kind, **body):
            if kind == "attempt":
                raise OSError("disk full")
            return orig(kind, **body)
        h._j = fail_on_attempt
        refused_without_submission(self, f, lambda: h.buy(0.0001, PX, otype="market"))
        self.assertEqual(h.attempts, 0)
        self.assertEqual(h.unresolved(), [])


if __name__ == "__main__":
    unittest.main()
