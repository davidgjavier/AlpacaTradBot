"""Round-2 regression tests: ambiguous submissions and duplicate-order prevention.

Run: cd review/p8_sdk_transport/proposed && /usr/bin/python3 -m unittest -v test_p8_round2
Reuses the fixtures of test_broker_io.py (network blocked; real alpaca-py 0.43.5 client;
simulated broker under the timeout adapter). Written BEFORE the fix and run unchanged against
the baseline (eddcdda) and the fix. Assertions check POST counts, broker orders and stored
intent state, so a baseline failure reflects the defect rather than an API difference.

Broker behaviors marked HYPOTHETICAL (duplicate-id handling, visibility delay) are not
documented by Alpaca; the tests require safety under EITHER possibility.
"""
import json
import os
import tempfile
import threading
import unittest

import test_broker_io as T  # fixtures + network block
import broker_io as B

J422_DUP = json.dumps({"code": 40010001, "message": "client_order_id must be unique (HYPOTHETICAL)"})


class Broker2(T.SimBroker):
    """Adds: hidden (not-yet-visible) orders, truncated listings, duplicate-id policy,
    and barriers so two callers can be held at the same point."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.hidden = set()                 # client ids whose orders exist but are not yet visible
        self.truncate_list = False          # list returns a full page of OTHER orders only
        self.dup_policy = "reject_422"      # HYPOTHETICAL: 'reject_422' | 'accept'
        self.list_barrier = None            # threading.Barrier to align concurrent reconciles
        self.lookup_hook = None             # callable(thread_name) run before a by-id lookup
        self.lock = threading.Lock()

    def _transport_send(self, req, **kw):
        path = req.url.split("/v2")[-1]
        if req.method == "POST":
            body = json.loads(req.body)
            cid = body.get("client_order_id")
            with self.lock:
                exists = cid and any(o["client_order_id"] == cid for o in self.orders)
            if exists and not self.post:
                self.calls.append((req.method, path.split("?")[0], kw.get("timeout")))
                if self.dup_policy == "reject_422":
                    return self._resp(req, 422, J422_DUP)
        if req.method == "GET" and "by_client_order_id" in path:
            if self.lookup_hook:
                self.lookup_hook(threading.current_thread().name)
            cid = T.requests.utils.unquote(path.split("client_order_id=")[-1])
            if cid in self.hidden and not self.get_cid:
                self.calls.append((req.method, path.split("?")[0], kw.get("timeout")))
                return self._resp(req, 404, T.J404)
        if req.method == "GET" and path.startswith("/orders") and "by_client_order_id" not in path:
            if self.list_barrier:
                self.list_barrier.wait(10)
            if self.truncate_list or self.hidden:
                self.calls.append((req.method, path.split("?")[0], kw.get("timeout")))
                if self.truncate_list:
                    # valid UUID ids (the SDK validates them); none is ours
                    page = [T.order_json(str(T.uuid.uuid4()), f"other-cid-{i}", {"qty": 0.0001, "type": "limit",
                                                                         "side": "buy", "time_in_force": "gtc"})
                            for i in range(500)]
                else:
                    page = [o for o in self.orders if o["client_order_id"] not in self.hidden]
                return self._resp(req, 200, json.dumps(page))
        with self.lock:
            return super()._transport_send(req, **kw)


def make2(db=None):
    broker = Broker2()
    client = T.TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real", paper=True)
    B.configure_client(client, adapter=broker)
    clock = T.Clock()
    db = db or os.path.join(tempfile.mkdtemp(), "intents.db")
    gw = B.OrderGateway(client, B.IntentStore(db), "BTC/USD", now_ns=clock.ns, read_kw=T.fast_read_kw(clock))
    return broker, client, gw, clock, db


def second_gateway(client, db, clock):
    return B.OrderGateway(client, B.IntentStore(db), "BTC/USD", now_ns=clock.ns, read_kw=T.fast_read_kw(clock))


def posts(b):
    return [c for c in b.calls if c[0] == "POST"]


def state(db, cid):
    return B.IntentStore(db).get(cid)


def call_safely(fn):
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001
        return None, e


class F1_ConcurrentCallers(unittest.TestCase):
    def test_concurrent_resubmit_never_double_posts(self):
        """Finding 1. Two callers both reconcile past the window, then both resubmit."""
        b, c, gw, clk, db = make2()
        b.post = [("raise", T.requests.exceptions.ConnectTimeout("ct"))]   # first POST never arrives
        gw.submit(T.req("r-f1"), "test")
        clk.t += 31
        gw2 = second_gateway(c, db, clk)
        b.list_barrier = threading.Barrier(2)
        out = {}

        def run(name, g):
            out[name] = call_safely(lambda: g.resubmit(T.req("r-f1")))
        ts = [threading.Thread(target=run, args=(n, g), name=n) for n, g in (("A", gw), ("B", gw2))]
        [t.start() for t in ts]
        [t.join(15) for t in ts]
        self.assertEqual(len(posts(b)), 1, f"resubmit POSTed {len(posts(b)) - 1} more time(s)")
        self.assertEqual(len(b.orders), 0)
        self.assertNotEqual(state(db, "r-f1")["state"], "REJECTED")

    def test_concurrent_first_submit_same_id_single_post_no_exception(self):
        b, c, gw, clk, db = make2()
        gw2 = second_gateway(c, db, clk)
        out = {}
        barrier = threading.Barrier(2)

        def run(name, g):
            barrier.wait(5)
            out[name] = call_safely(lambda: g.submit(T.req("r-f1b"), "test"))
        ts = [threading.Thread(target=run, args=(n, g), name=n) for n, g in (("A", gw), ("B", gw2))]
        [t.start() for t in ts]
        [t.join(15) for t in ts]
        self.assertEqual(len(posts(b)), 1)
        self.assertEqual(len(b.orders), 1)
        errors = [e for _, e in out.values() if e is not None]
        self.assertEqual(errors, [], f"a concurrent submit raised instead of returning a result: {errors}")

    def test_stale_reconcile_cannot_downgrade_accepted(self):
        """Caller A's lookup is slow and returns 404; caller B finds the order and records
        ACCEPTED first; A's stale result must not overwrite it."""
        b, c, gw, clk, db = make2()
        b.post = [("accept_then", 504, T.J504)]
        b.hidden.add("r-f1c")
        gw.submit(T.req("r-f1c"), "test")                 # UNRESOLVED (hidden)
        clk.t += 31                                       # past the window: A's stale view takes the "absent" path
        gw2 = second_gateway(c, db, clk)
        a_in_lookup, b_done = threading.Event(), threading.Event()

        def hook(name):
            if name == "A":
                a_in_lookup.set()
                b_done.wait(10)                          # A's (stale) 404 returns after B finishes
        b.lookup_hook = hook
        out = {}

        def run_a():
            out["A"] = call_safely(lambda: gw.reconcile("r-f1c"))

        def run_b():
            a_in_lookup.wait(10)
            b.hidden.discard("r-f1c")                    # order becomes visible for B
            out["B"] = call_safely(lambda: gw2.reconcile("r-f1c"))
            b.hidden.add("r-f1c")                        # ...but A already fetched its 404 view
            b_done.set()
        ta, tb = threading.Thread(target=run_a, name="A"), threading.Thread(target=run_b, name="B")
        ta.start(); tb.start(); ta.join(15); tb.join(15)   # noqa: E702
        self.assertEqual(state(db, "r-f1c")["state"], "ACCEPTED", "stale reconcile downgraded ACCEPTED")
        self.assertEqual(state(db, "r-f1c")["order_id"], b.orders[0]["id"])


class F2_DuplicateIdRejectionAfterAmbiguous(unittest.TestCase):
    def test_later_422_does_not_mark_rejected_or_stop_reconciliation(self):
        """Finding 2. First POST accepted but hidden (504). After the window, a resubmit with
        the same id is rejected as a duplicate (HYPOTHETICAL 422). The intent must not become
        REJECTED, must stay in the recovery queue, and must resolve to the ORIGINAL order."""
        b, c, gw, clk, db = make2()
        b.post = [("accept_then", 504, T.J504)]
        b.hidden.add("r-f2")
        gw.submit(T.req("r-f2"), "test")
        clk.t += 31
        call_safely(lambda: gw.resubmit(T.req("r-f2")))
        self.assertNotEqual(state(db, "r-f2")["state"], "REJECTED", "earlier accepted order hidden behind REJECTED")
        self.assertIn("r-f2", [r["client_order_id"] for r in B.IntentStore(db).pending()],
                      "intent dropped from the recovery queue")
        b.hidden.discard("r-f2")
        gw.recover_pending()
        self.assertEqual((state(db, "r-f2")["state"], state(db, "r-f2")["order_id"]),
                         ("ACCEPTED", b.orders[0]["id"]))


class F3_AbsenceIsNotProven(unittest.TestCase):
    def test_delayed_visibility_beyond_window_no_resubmit(self):
        b, c, gw, clk, db = make2()
        b.post = [("accept_then", 504, T.J504)]
        b.hidden.add("r-f3a")
        gw.submit(T.req("r-f3a"), "test")
        clk.t += 120                                     # far beyond the 30 s window
        call_safely(lambda: gw.reconcile("r-f3a"))
        call_safely(lambda: gw.resubmit(T.req("r-f3a")))
        self.assertEqual(len(posts(b)), 1, "elapsed time + negative lookup authorized another POST")
        self.assertEqual(len(b.orders), 1)
        self.assertIn("r-f3a", [r["client_order_id"] for r in B.IntentStore(db).pending()])
        b.hidden.discard("r-f3a")
        gw.recover_pending()
        self.assertEqual(state(db, "r-f3a")["order_id"], b.orders[0]["id"])

    def test_truncated_listing_is_not_proof_of_absence(self):
        b, c, gw, clk, db = make2()
        b.post = [("accept_then", 504, T.J504)]
        b.get_cid = [("status", 404, T.J404)] * 20       # by-id lookup keeps missing it
        b.truncate_list = True                           # one full page of OTHER orders
        gw.submit(T.req("r-f3b"), "test")
        clk.t += 31
        call_safely(lambda: gw.reconcile("r-f3b"))
        call_safely(lambda: gw.resubmit(T.req("r-f3b")))
        self.assertEqual(len(posts(b)), 1, "a truncated listing authorized another POST")
        self.assertNotIn(state(db, "r-f3b")["state"], ("REJECTED",))
        self.assertIn("r-f3b", [r["client_order_id"] for r in B.IntentStore(db).pending()])

    def test_even_full_negative_evidence_does_not_authorize_resubmit(self):
        """Policy for this round: no automatic resubmission of ambiguous intents at all."""
        b, c, gw, clk, db = make2()
        b.post = [("raise", T.requests.exceptions.ConnectTimeout("ct"))]
        gw.submit(T.req("r-f3c"), "test")
        clk.t += 3600
        call_safely(lambda: gw.resubmit(T.req("r-f3c")))
        self.assertEqual(len(posts(b)), 1)
        self.assertIn("r-f3c", [r["client_order_id"] for r in B.IntentStore(db).pending()])


class F4_RecoveredIdentity(unittest.TestCase):
    def test_every_accepted_result_carries_the_stored_order_id(self):
        b, c, gw, clk, db = make2()
        b.post = [("accept_then", 504, T.J504)]
        r1 = gw.submit(T.req("r-f4"), "test")
        oid = b.orders[0]["id"]
        self.assertEqual(getattr(r1, "order_id", None), oid, "submit->reconcile ACCEPTED lacks order_id")
        r2, _ = call_safely(lambda: gw.reconcile("r-f4"))           # "already resolved" path
        self.assertEqual(getattr(r2, "order_id", None), oid, "already-resolved ACCEPTED lacks order_id")
        r3, _ = call_safely(lambda: gw.submit(T.req("r-f4"), "test"))  # idempotent resubmit-call path
        self.assertEqual(getattr(r3, "order_id", None), oid, "existing-intent result lacks order_id")
        gw_restart = second_gateway(c, db, clk)
        r4, _ = call_safely(lambda: gw_restart.reconcile("r-f4"))
        self.assertEqual(getattr(r4, "order_id", None), oid, "restart result lacks order_id")
        self.assertEqual(len(posts(b)), 1)


class F5_PersistenceFailure(unittest.TestCase):
    def fail_on(self, gw, op, times=1):
        n = {"k": 0}

        def hook(o):
            if o == op and n["k"] < times:
                n["k"] += 1
                raise T.sqlite_error()
        gw.store.fail_hook = hook

    def test_accepted_then_record_fails_returns_known_identity(self):
        b, c, gw, clk, db = make2()
        self.fail_on(gw, "mark_ACCEPTED")
        r, err = call_safely(lambda: gw.submit(T.req("r-f5a"), "test"))
        self.assertIsNone(err, f"accepted order hidden behind exception: {err!r}")
        self.assertEqual((r.state, getattr(r, "order_id", None)), ("ACCEPTED", b.orders[0]["id"]))
        self.assertIs(getattr(r, "persisted", None), False)
        self.assertIn(state(db, "r-f5a")["state"], ("SUBMITTING", "UNRESOLVED"))   # still queued
        gw.store.fail_hook = None
        gw.recover_pending()
        self.assertEqual(state(db, "r-f5a")["order_id"], b.orders[0]["id"])
        self.assertEqual(len(posts(b)), 1)

    def test_ambiguous_then_record_fails_returns_unresolved_not_exception(self):
        b, c, gw, clk, db = make2()
        b.post = [("status", 504, T.J504)]
        self.fail_on(gw, "mark_UNRESOLVED", times=10)
        r, err = call_safely(lambda: gw.submit(T.req("r-f5b"), "test"))
        self.assertIsNone(err, f"ambiguous outcome hidden behind exception: {err!r}")
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertIs(getattr(r, "persisted", None), False)
        self.assertIn("r-f5b", [x["client_order_id"] for x in B.IntentStore(db).pending()])
        self.assertEqual(len(posts(b)), 1)

    def test_reconcile_found_but_record_fails_still_returns_identity(self):
        b, c, gw, clk, db = make2()
        b.post = [("accept_then", 504, T.J504)]
        b.hidden.add("r-f5c")
        gw.submit(T.req("r-f5c"), "test")
        b.hidden.discard("r-f5c")
        self.fail_on(gw, "mark_ACCEPTED")
        r, err = call_safely(lambda: gw.reconcile("r-f5c"))
        self.assertIsNone(err, f"found order hidden behind exception: {err!r}")
        self.assertEqual((r.state, getattr(r, "order_id", None)), ("ACCEPTED", b.orders[0]["id"]))
        self.assertIs(getattr(r, "persisted", None), False)

    def test_intent_write_failure_returns_not_submitted_zero_posts(self):
        b, c, gw, clk, db = make2()
        self.fail_on(gw, "create_submitting")
        r, err = call_safely(lambda: gw.submit(T.req("r-f5d"), "test"))
        self.assertEqual(len(posts(b)), 0)
        self.assertIsNone(err, f"pre-submit failure raised instead of an explicit result: {err!r}")
        self.assertEqual(r.state, "NOT_SUBMITTED")
        self.assertIs(getattr(r, "persisted", None), False)


if __name__ == "__main__":
    unittest.main()
