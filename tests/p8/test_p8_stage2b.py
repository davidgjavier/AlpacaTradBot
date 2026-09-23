"""Stage 2b regression tests: durable submission ownership across gateways/processes/crashes,
operational lock release disabled, and one end-to-end submit budget (incl. DB waits).

Run: cd review/p8_sdk_transport/proposed && /usr/bin/python3 -m unittest -v test_p8_stage2b
Written BEFORE the fix; run unchanged against baseline b228258 (fix 900d511) and the fix.
Codex's reviewer_checks.py (sha256 9fa2f8753f8efe38) scenarios are A1 and C1.
"""
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unittest

import test_broker_io as T
import test_p8_round4 as R4
import broker_io as B

HERE = os.path.dirname(os.path.abspath(__file__))


def fresh_gateway(c, db, clk):
    """A second, independent gateway on the SAME client and intent DB, default settings."""
    return B.OrderGateway(c, B.IntentStore(db), "BTC/USD", now_ns=clk.ns, read_kw=T.fast_read_kw(clk))


def state(db, cid):
    return B.IntentStore(db).get(cid)["state"]


class A_OwnershipAcrossGateways(unittest.TestCase):
    def test_second_gateway_cannot_abandon_or_release_while_post_in_flight(self):
        """Codex reviewer_checks.py case 1, as assertions."""
        b, c, g, clk, db = R4.make4()
        b.post_delay_before = 2.0
        g.submit_deadline_s = .05
        r = g.submit(T.req("review-cross-gateway"), "test")
        g2 = fresh_gateway(c, db, clk)
        a = g2.abandon("review-cross-gateway", operator="offline-review", note="same-process second gateway")
        u = g2.release_entry_lock("review-cross-gateway", operator="offline-review", note="offline only")
        self.assertNotEqual(a.state, "ABANDONED", "second gateway bypassed active submission ownership")
        self.assertNotEqual(state(db, "review-cross-gateway"), "ABANDONED")
        self.assertTrue(g2.entries_locked()[0])
        time.sleep(2.1)                                  # the late POST lands
        self.assertEqual((len(b.orders), len(R4.posts(b))), (1, 1))
        self.assertTrue(g2.entries_locked()[0])

    def test_completion_bookkeeping_failure_keeps_ownership_conservative(self):
        """If the worker cannot record that its request finished, the id must stay 'in flight'."""
        b, c, g, clk, db = R4.make4()
        b.post = [("raise", T.requests.exceptions.ConnectTimeout("ct"))]
        b.post_delay_before = 0.4
        g.submit_deadline_s = 0.1

        def fail(op):
            if op in ("post_finished", "mark_post_finished"):
                raise T.sqlite_error()
        g.store.fail_hook = fail
        g.submit(T.req("r5-a2"), "test")
        time.sleep(0.7)                                  # worker finished; its bookkeeping write failed
        g.store.fail_hook = None
        g2 = fresh_gateway(c, db, clk)
        g2.abandon("r5-a2", operator="op", note="n")
        self.assertEqual(state(db, "r5-a2"), "UNRESOLVED")


CHILD = textwrap.dedent(r'''
    import os, sys, time
    sys.path.insert(0, os.getcwd())
    import warnings; warnings.filterwarnings("ignore")
    import test_p8_round4 as R4, test_broker_io as T, broker_io as B
    db, ready, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    b = R4.SlowBroker()
    c = T.TradingClient("PKP8TESTONLY000000000", "x", paper=True)
    B.configure_client(c, adapter=b)
    clk = T.Clock(); clk.t = R4.R3.T0.timestamp()
    g = B.OrderGateway(c, B.IntentStore(db), "BTC/USD", now_ns=clk.ns, read_kw=T.fast_read_kw(clk))
    g.submit_deadline_s = 0.2
    b.post_delay_before = 30.0                         # POST stays in flight
    g.submit(T.req("r5-xproc"), "test")
    open(ready, "w").write("submitted")
    if mode == "crash":
        os.kill(os.getpid(), 9)                        # owner dies with the POST in flight
    time.sleep(30)
''')


class B_OwnershipAcrossProcessesAndCrashes(unittest.TestCase):
    def run_child(self, mode):
        import tempfile
        d = tempfile.mkdtemp()
        db, ready = os.path.join(d, "intents.db"), os.path.join(d, "ready")
        p = subprocess.Popen([sys.executable, "-c", CHILD, db, ready, mode], cwd=HERE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t = time.monotonic()
        while not os.path.exists(ready) and time.monotonic() - t < 15:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(ready), p.stderr.read().decode()[-500:] if p.poll() is not None else "timeout")
        return p, db

    def parent_gateway(self, db):
        b = R4.SlowBroker()                                 # parent's view of the broker: no order (yet)
        c = T.TradingClient("PKP8TESTONLY000000000", "x", paper=True)
        B.configure_client(c, adapter=b)
        clk = T.Clock()
        clk.t = R4.R3.T0.timestamp() + 3600                 # long after the window
        return B.OrderGateway(c, B.IntentStore(db), "BTC/USD", now_ns=clk.ns, read_kw=T.fast_read_kw(clk))

    def test_other_process_cannot_abandon_while_owner_post_in_flight(self):
        p, db = self.run_child("live")
        try:
            g = self.parent_gateway(db)
            g.abandon("r5-xproc", operator="op", note="n")
            self.assertEqual(state(db, "r5-xproc"), "UNRESOLVED", "other process bypassed submission ownership")
            self.assertTrue(g.entries_locked()[0])
        finally:
            p.kill()
            p.wait(5)

    def test_owner_crash_preserves_uncertainty(self):
        p, db = self.run_child("crash")
        p.wait(10)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        g = self.parent_gateway(db)
        g.abandon("r5-xproc", operator="op", note="owner died")
        self.assertEqual(state(db, "r5-xproc"), "UNRESOLVED",
                         "owner disappearance treated as proof the order cannot arrive")
        g.recover_pending()
        self.assertTrue(g.entries_locked()[0])


class C_EndToEndSubmitBudget(unittest.TestCase):
    SLACK = 0.10          # allowed Python/scheduling overhead beyond the advertised budget

    def test_codex_case_budget_covers_reconciliation(self):
        """Codex reviewer_checks.py case 2, as an assertion."""
        b, c, g, clk, db = R4.make4()
        b.post_delay_before = 1.0
        b.lookup_delay = 1.0
        g.submit_deadline_s = .05
        g.read_kw = {"deadline_s": .25, "max_attempts": 1}
        t = time.monotonic()
        r = g.submit(T.req("review-total-deadline"), "test")
        el = time.monotonic() - t
        self.assertLess(el, .05 + self.SLACK, f"submit took {el:.3f}s for a 0.05s end-to-end budget")
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertTrue(r.exposure_may_exist)

    def test_budget_covers_slow_reconcile_after_fast_ambiguous_post(self):
        b, c, g, clk, db = R4.make4()
        b.post = [("status", 504, T.J504)]
        b.lookup_delay = 3.0
        g.submit_deadline_s = 0.6
        g.read_kw = dict(g.read_kw, deadline_s=5.0)        # per-read deadline larger than the budget
        t = time.monotonic()
        r = g.submit(T.req("r5-c2"), "test")
        el = time.monotonic() - t
        self.assertLess(el, 0.6 + self.SLACK, f"submit took {el:.3f}s for a 0.6s budget")
        self.assertEqual(r.state, "UNRESOLVED")

    def test_database_lock_wait_counts_against_budget(self):
        """Another connection holds the write lock after the POST; the post-POST record must not
        wait sqlite's default busy timeout. The result must still carry the known outcome."""
        b, c, g, clk, db = R4.make4()
        g.submit_deadline_s = 0.5
        holder = {}

        def lock_db_on_post(req, **kw):
            conn = sqlite3.connect(db, timeout=1, isolation_level=None, check_same_thread=False)
            conn.execute("BEGIN IMMEDIATE")
            holder["c"] = conn
        orig = b._transport_send

        def send(req, **kw):
            if req.method == "POST" and "c" not in holder:
                lock_db_on_post(req, **kw)
            return orig(req, **kw)
        b._transport_send = send
        t = time.monotonic()
        r = g.submit(T.req("r5-c3"), "test")
        el = time.monotonic() - t
        holder["c"].execute("ROLLBACK")
        holder["c"].close()
        self.assertLess(el, 0.5 + self.SLACK, f"submit waited {el:.3f}s on a DB lock with a 0.5s budget")
        self.assertIn(r.state, ("ACCEPTED", "ACCEPTED_UNVERIFIED", "UNRESOLVED"))
        self.assertIs(r.persisted, False)
        self.assertEqual(len(b.orders), 1)
        g.recover_pending()
        self.assertIn(state(db, "r5-c3"), ("ACCEPTED", "ACCEPTED_UNVERIFIED"))


class D_LockReleaseDisabledByDefault(unittest.TestCase):
    def test_default_gateway_never_releases_the_entry_lock(self):
        b, c, g, clk, db = R4.make4()
        b.post = [("raise", T.requests.exceptions.ConnectTimeout("ct"))]
        g.submit(T.req("r5-d1"), "test")
        time.sleep(0.05)
        g.abandon("r5-d1", operator="op", note="n")
        self.assertEqual(state(db, "r5-d1"), "ABANDONED")   # abandon mechanism still works
        g.release_entry_lock("r5-d1", operator="op", note="n")
        self.assertTrue(g.entries_locked()[0], "operational lock release must be disabled by default")


if __name__ == "__main__":
    unittest.main()
