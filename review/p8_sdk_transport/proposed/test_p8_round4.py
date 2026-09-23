"""Stage 2 (round 4) regression tests: wall-clock deadlines, late completion, bounded workers.

Run: cd review/p8_sdk_transport/proposed && /usr/bin/python3 -m unittest -v test_p8_round4
Uses REAL wall-clock time and, for slow-drip, a REAL loopback socket. Written BEFORE the fix and
run unchanged against baseline 127b6d0. Deadlines are set by attribute where possible so baseline
failures are behavioral (it ignores the attribute) rather than signature errors.

Constraint under test: timing out a thread join does NOT cancel the in-flight HTTP request. The
request may complete late; that late result must never be applied to state, and a submit abandoned
at the deadline must be UNRESOLVED, never "not sent".
"""
import json
import socket
import threading
import time
import unittest

import test_broker_io as T
import test_p8_round3 as R3
import broker_io as B


class SlowBroker(R3.Broker3):
    """POST can accept the order and then delay its response (late completion), or delay before
    reaching the broker. GET by id can be delayed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.post_delay_after_accept = 0.0
        self.post_delay_before = 0.0
        self.lookup_delay = 0.0
        self.late_done = threading.Event()

    def _transport_send(self, req, **kw):
        path = req.url.split("/v2")[-1]
        if req.method == "POST" and self.post_delay_before:
            time.sleep(self.post_delay_before)
        if req.method == "GET" and "by_client_order_id" in path and self.lookup_delay:
            time.sleep(self.lookup_delay)
        r = super()._transport_send(req, **kw)
        if req.method == "POST" and self.post_delay_after_accept:
            time.sleep(self.post_delay_after_accept)     # order exists; response is late
            self.late_done.set()
        return r


def make4():
    b = SlowBroker()
    c = T.TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real", paper=True)
    B.configure_client(c, adapter=b)
    clk = T.Clock()
    clk.t = R3.T0.timestamp()
    import os, tempfile
    db = os.path.join(tempfile.mkdtemp(), "intents.db")
    gw = B.OrderGateway(c, B.IntentStore(db), "BTC/USD", now_ns=clk.ns, read_kw=T.fast_read_kw(clk))
    gw.submit_deadline_s = 0.3                      # attribute: baseline ignores it
    return b, c, gw, clk, db


def posts(b):
    return [x for x in b.calls if x[0] == "POST"]


class A_ReadBudget(unittest.TestCase):
    def test_read_never_exceeds_wall_clock_budget_on_a_hung_call(self):
        t = time.monotonic()
        r = B.read(lambda: time.sleep(2.0), deadline_s=0.4)
        el = time.monotonic() - t
        self.assertEqual(r.state, "UNAVAILABLE")
        self.assertLess(el, 0.8, f"read() took {el:.2f}s for a 0.4s budget")

    def test_retry_backoff_is_truncated_to_the_remaining_budget(self):
        def boom():
            raise T.requests.exceptions.ConnectionError("down")
        t = time.monotonic()
        r = B.read(boom, deadline_s=0.6, base_backoff_s=0.5, max_backoff_s=5.0, max_attempts=10)
        el = time.monotonic() - t
        self.assertEqual(r.state, "UNAVAILABLE")
        self.assertLess(el, 0.9, f"read() took {el:.2f}s for a 0.6s budget")

    def test_slow_drip_socket_is_bounded_by_the_caller_deadline(self):
        """Real loopback server: headers, then 1 body byte every 0.2 s forever. The socket read
        timeout (1.0 s, inactivity) never fires; only a wall-clock deadline bounds the caller."""
        stop, ready, port = threading.Event(), threading.Event(), []

        def srv():
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port.append(s.getsockname()[1])
            ready.set()
            conn, _ = s.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n")
            while not stop.is_set():
                try:
                    conn.sendall(b" ")
                except OSError:
                    break
                time.sleep(0.2)
            conn.close()
            s.close()
        threading.Thread(target=srv, daemon=True).start()
        ready.wait(5)
        c = T.TradingClient("PKP8TESTONLY000000000", "x", url_override=f"http://127.0.0.1:{port[0]}")
        B.configure_client(c, timeout=(1.0, 1.0))
        out = {}

        def call():
            t = time.monotonic()
            out["r"] = B.read(lambda: c.get_order_by_client_id("x"), deadline_s=1.5)
            out["el"] = time.monotonic() - t
        th = threading.Thread(target=call, daemon=True)
        th.start()
        th.join(4.0)                                 # watchdog so a failing baseline can't hang the suite
        stop.set()
        self.assertFalse(th.is_alive(), "read() still blocked after 4 s on a slow-drip response")
        self.assertEqual(out["r"].state, "UNAVAILABLE")
        self.assertLess(out["el"], 2.2)


class B_SubmitDeadlineAndLateCompletion(unittest.TestCase):
    def test_deadline_exceeded_after_acceptance_is_unresolved_not_not_submitted(self):
        b, c, gw, clk, db = make4()
        b.post_delay_after_accept = 1.2
        t = time.monotonic()
        r = gw.submit(T.req("r4-b1"), "test")
        el = time.monotonic() - t
        self.assertLess(el, 1.0, f"submit blocked {el:.2f}s past a 0.3s deadline")
        self.assertIn(r.state, ("UNRESOLVED", "ACCEPTED", "ACCEPTED_UNVERIFIED"))
        self.assertNotEqual(r.state, "NOT_SUBMITTED")
        self.assertTrue(r.exposure_may_exist)
        self.assertEqual(len(posts(b)), 1)

    def test_late_response_never_mutates_state_out_of_order(self):
        b, c, gw, clk, db = make4()
        b.post_delay_after_accept = 1.0
        b.hidden.add("r4-b2")
        r = gw.submit(T.req("r4-b2"), "test")              # returns at the deadline (hidden -> UNRESOLVED)
        b.hidden.discard("r4-b2")
        rec = gw.reconcile("r4-b2")                         # resolution happens BEFORE the late response
        events_before = gw.store.events("r4-b2")
        state_before = gw.store.get("r4-b2")
        self.assertTrue(b.late_done.wait(3), "late response never arrived (fixture)")
        time.sleep(0.2)
        self.assertEqual(gw.store.get("r4-b2")["state"], state_before["state"])
        self.assertEqual(gw.store.get("r4-b2")["updated_ns"], state_before["updated_ns"])
        self.assertEqual(gw.store.events("r4-b2"), events_before, "late response wrote history/state")
        self.assertEqual(len(posts(b)), 1)

    def test_post_never_reaches_broker_before_deadline_is_unresolved_one_post(self):
        b, c, gw, clk, db = make4()
        b.post_delay_before = 1.0                           # stalled before reaching the broker
        t = time.monotonic()
        r = gw.submit(T.req("r4-b3"), "test")
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertEqual(len([x for x in gw.store.pending() if x["client_order_id"] == "r4-b3"]), 1)
        time.sleep(1.2)                                     # the stalled POST completes late
        self.assertEqual(len(posts(b)), 1, "late completion must not cause a second POST")

    def test_lookup_hang_inside_reconcile_is_bounded(self):
        b, c, gw, clk, db = make4()
        b.post = [("status", 504, T.J504)]
        b.lookup_delay = 2.0
        gw.read_kw = dict(gw.read_kw, deadline_s=0.4)
        t = time.monotonic()
        r = gw.submit(T.req("r4-b4"), "test")
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertEqual(r.state, "UNRESOLVED")


class C_BoundedWorkers(unittest.TestCase):
    def test_saturated_workers_fail_closed_without_posting(self):
        b, c, gw, clk, db = make4()
        gw.runner = B.CallRunner(max_inflight=1)
        hang = threading.Event()
        gw.runner.try_run(lambda: hang.wait(3), 0.05)       # occupies the only worker slot
        r = gw.submit(T.req("r4-c1"), "test")
        self.assertEqual(r.state, "NOT_SUBMITTED")
        self.assertEqual(len(posts(b)), 0)
        self.assertIsNone(gw.store.get("r4-c1"), "no intent should be created when nothing can be sent")
        rr = B.read(lambda: 1, runner=gw.runner, deadline_s=0.2)
        self.assertEqual(rr.state, "UNAVAILABLE")
        hang.set()
        time.sleep(0.2)
        self.assertEqual(gw.runner.inflight, 0, "worker slot not returned after late completion")


if __name__ == "__main__":
    unittest.main()
