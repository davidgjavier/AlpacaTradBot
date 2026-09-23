"""Offline tests for the PROPOSED broker_io wrapper, driving the REAL alpaca-py 0.43.5 client.

Run:  cd review/p8_sdk_transport/proposed && /usr/bin/python3 -m unittest -v test_broker_io
Non-loopback network is blocked. The broker is simulated underneath the timeout adapter.
Simulated duplicate-client-id handling is HYPOTHETICAL and never needed by these tests
(the wrapper never re-POSTs an id that might exist).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import warnings

warnings.filterwarnings("ignore")
_real_connect, _real_create = socket.socket.connect, socket.create_connection


def _loop(addr):
    return (addr[0] if isinstance(addr, tuple) else str(addr)) in ("127.0.0.1", "::1", "localhost")


def _gc(self, addr):
    if not _loop(addr):
        raise RuntimeError(f"network blocked {addr}")
    return _real_connect(self, addr)


def _gcc(addr, *a, **k):
    if not _loop(addr):
        raise RuntimeError(f"network blocked {addr}")
    return _real_create(addr, *a, **k)


socket.socket.connect, socket.create_connection = _gc, _gcc

import requests  # noqa: E402
import broker_io as B  # noqa: E402
from alpaca.trading.client import TradingClient  # noqa: E402
from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: E402
from alpaca.trading.requests import LimitOrderRequest  # noqa: E402

J504 = json.dumps({"code": 50410000, "message": "request timed out"})
J429 = json.dumps({"code": 42910000, "message": "rate limit exceeded"})
J403 = json.dumps({"code": 40310000, "message": "insufficient balance for BTC"})
J404 = json.dumps({"code": 40410000, "message": "order not found"})
HTML504 = "<html>504 Gateway Time-out</html>"


def order_json(oid, cid, body, status="new"):
    return {"id": oid, "client_order_id": cid, "created_at": "2026-09-23T14:00:00Z",
            "updated_at": "2026-09-23T14:00:00Z", "submitted_at": "2026-09-23T14:00:00Z",
            "filled_at": None, "expired_at": None, "canceled_at": None, "failed_at": None,
            "replaced_at": None, "replaced_by": None, "replaces": None,
            "asset_id": "276e2673-764b-4ab6-a611-caf665ca6340", "symbol": "BTC/USD", "asset_class": "crypto",
            "notional": None, "qty": str(body.get("qty")), "filled_qty": "0", "filled_avg_price": None,
            "order_class": "simple", "order_type": body.get("type"), "type": body.get("type"),
            "side": body.get("side"), "time_in_force": body.get("time_in_force"),
            "limit_price": body.get("limit_price"), "stop_price": None, "status": status,
            "extended_hours": False, "legs": None, "trail_percent": None, "trail_price": None,
            "hwm": None, "position_intent": None}


class SimBroker(B.TimeoutHTTPAdapter):
    """Sits UNDER the timeout adapter, so every request records the timeout actually applied.
    Scripts are per-route lists of behaviors; empty script -> default (state-based) behavior.
    state_file: optional JSON file so a second PROCESS sees the same broker state."""

    def __init__(self, state_file=None, **kw):
        super().__init__(timeout=kw.pop("timeout", (0.5, 1.0)))
        self.state_file = state_file
        self.post, self.get_cid, self.list, self.delete = [], [], [], []
        self.calls = []
        self.orders = self._load()

    def _load(self):
        if self.state_file and os.path.exists(self.state_file):
            return json.load(open(self.state_file))
        return []

    def _save(self):
        if self.state_file:
            json.dump(self.orders, open(self.state_file, "w"))

    def _resp(self, req, code, text):
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = code, text.encode(), req.url, req
        r.headers["Content-Type"] = "application/json"
        return r

    def _transport_send(self, req, **kw):
        path = req.url.split("/v2")[-1]
        self.calls.append((req.method, path.split("?")[0], kw.get("timeout")))
        if req.method == "POST":
            beh = self.post.pop(0) if self.post else ("accept",)
            if beh[0] == "status":
                return self._resp(req, beh[1], beh[2])
            if beh[0] == "raise":
                raise beh[1]
            body = json.loads(req.body)
            o = order_json(str(uuid.uuid4()), body.get("client_order_id") or str(uuid.uuid4()), body)
            self.orders.append(o)
            self._save()
            if beh[0] == "accept":
                return self._resp(req, 200, json.dumps(o))
            if beh[0] == "accept_then":
                return self._resp(req, beh[1], beh[2])
            if beh[0] == "accept_then_raise":
                raise beh[1]
        if req.method == "GET" and "by_client_order_id" in path:
            beh = self.get_cid.pop(0) if self.get_cid else ("state",)
            if beh[0] == "status":
                return self._resp(req, beh[1], beh[2])
            if beh[0] == "raise":
                raise beh[1]
            cid = requests.utils.unquote(path.split("client_order_id=")[-1])
            hit = [o for o in self.orders if o["client_order_id"] == cid]
            return self._resp(req, 200, json.dumps(hit[0])) if hit else self._resp(req, 404, J404)
        if req.method == "GET" and path.startswith("/orders"):
            beh = self.list.pop(0) if self.list else ("state",)
            if beh[0] == "status":
                return self._resp(req, beh[1], beh[2])
            return self._resp(req, 200, json.dumps(self.orders))
        if req.method == "DELETE":
            beh = self.delete.pop(0) if self.delete else ("state",)
            if beh[0] == "status":
                return self._resp(req, beh[1], beh[2])
            oid = path.rsplit("/", 1)[-1]
            o = next((x for x in self.orders if x["id"] == oid), None)
            if o is None:
                return self._resp(req, 404, J404)
            if o["status"] in ("filled", "canceled", "expired"):
                return self._resp(req, 422, json.dumps({"code": 42210000, "message": "order is not cancelable"}))
            o["status"] = "pending_cancel"
            r = self._resp(req, 204, "")
            return r
        return self._resp(req, 500, "{}")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def ns(self):
        return int(self.t * 1e9)


def fast_read_kw(clock):
    clock.sleeps = []
    return dict(clock=clock.mono, sleep=clock.sleep, deadline_s=8.0, max_attempts=5)


def make(state_file=None, db=None):
    broker = SimBroker(state_file=state_file)
    client = TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real", paper=True)
    B.configure_client(client, adapter=broker)
    clock = Clock()
    store = B.IntentStore(db or os.path.join(tempfile.mkdtemp(), "intents.db"))
    gw = B.OrderGateway(client, store, "BTC/USD", now_ns=clock.ns, read_kw=fast_read_kw(clock))
    return broker, client, store, gw, clock


def req(cid):
    return LimitOrderRequest(symbol="BTC/USD", qty=0.0002, side=OrderSide.SELL, time_in_force=TimeInForce.IOC,
                             limit_price=100000, client_order_id=cid)


class A_Configuration(unittest.TestCase):
    def test_retries_disabled_and_timeout_reaches_transport(self):
        b, c, s, gw, clk = make()
        self.assertEqual(c._retry, 0)
        gw.submit(req("t-a1"), "test")
        self.assertEqual(b.calls[0][2], (0.5, 1.0))            # timeout tuple applied by adapter

    def test_fails_closed_on_version_or_missing_attrs(self):
        c = TradingClient("PKP8TESTONLY000000000", "x", paper=True)
        with self.assertRaises(B.ConfigurationError):
            B.configure_client(c, expected_version="9.9.9")
        del c._retry
        with self.assertRaises(B.ConfigurationError):
            B.configure_client(c)

    def test_gateway_refuses_unconfigured_client(self):
        c = TradingClient("PKP8TESTONLY000000000", "x", paper=True)
        with self.assertRaises(B.ConfigurationError):
            B.OrderGateway(c, B.IntentStore(os.path.join(tempfile.mkdtemp(), "i.db")), "BTC/USD")


class B_SubmissionOutcomes(unittest.TestCase):
    def test_504_no_sdk_retry_single_post_unresolved(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 504, J504)]
        r = gw.submit(req("t-b1"), "test")
        self.assertEqual(len(b.posts()), 1)                    # SDK default would have sent 4
        self.assertEqual(r.state, "UNRESOLVED")                # lookup: not found, inside window
        self.assertEqual(s.get("t-b1")["state"], "UNRESOLVED")

    def test_429_is_not_blindly_retried(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 429, J429)]
        r = gw.submit(req("t-b2"), "test")
        self.assertEqual(len(b.posts()), 1)
        self.assertEqual(r.state, "UNRESOLVED")

    def test_accepted_response_lost_504_reconciled_one_order(self):
        b, c, s, gw, clk = make()
        b.post = [("accept_then", 504, J504)]
        r = gw.submit(req("t-b3"), "test")
        self.assertEqual((r.state, len(b.posts()), len(b.orders)), ("ACCEPTED", 1, 1))
        self.assertEqual(s.get("t-b3")["order_id"], b.orders[0]["id"])

    def test_accepted_read_timeout_and_reset_reconciled(self):
        for i, exc in enumerate([requests.exceptions.ReadTimeout("rt"), requests.exceptions.ConnectionError("reset")]):
            b, c, s, gw, clk = make()
            b.post = [("accept_then_raise", exc)]
            r = gw.submit(req(f"t-b4-{i}"), "test")
            self.assertEqual((r.state, len(b.posts()), len(b.orders)), ("ACCEPTED", 1, 1))

    def test_connect_timeout_never_accepted_stays_unresolved_no_repost(self):
        b, c, s, gw, clk = make()
        b.post = [("raise", requests.exceptions.ConnectTimeout("ct"))]
        r = gw.submit(req("t-b5"), "test")
        self.assertEqual((r.state, len(b.posts()), len(b.orders)), ("UNRESOLVED", 1, 0))

    def test_definitive_403_rejected(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 403, J403)]
        r = gw.submit(req("t-b6"), "test")
        self.assertEqual((r.state, len(b.posts())), ("REJECTED", 1))

    def test_non_json_504_does_not_crash_and_is_ambiguous(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 504, HTML504)]
        r = gw.submit(req("t-b7"), "test")
        self.assertEqual((r.state, len(b.posts())), ("UNRESOLVED", 1))

    def test_missing_client_id_refused_before_any_post(self):
        b, c, s, gw, clk = make()
        with self.assertRaises(ValueError):
            gw.submit(LimitOrderRequest(symbol="BTC/USD", qty=0.0002, side=OrderSide.SELL,
                                        time_in_force=TimeInForce.IOC, limit_price=100000), "test")
        self.assertEqual(b.posts(), [])

    def test_repeated_submit_same_id_never_reposts(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 504, J504)]
        gw.submit(req("t-b9"), "test")
        r2 = gw.submit(req("t-b9"), "test")
        self.assertEqual(len(b.posts()), 1)
        self.assertIn("no POST", r2.detail)

    def test_same_id_different_payload_refused(self):
        b, c, s, gw, clk = make()
        gw.submit(req("t-b10"), "test")
        other = LimitOrderRequest(symbol="BTC/USD", qty=0.0003, side=OrderSide.SELL,
                                  time_in_force=TimeInForce.IOC, limit_price=100000, client_order_id="t-b10")
        with self.assertRaises(ValueError):
            gw.submit(other, "test")
        self.assertEqual(len(b.posts()), 1)


class C_Persistence(unittest.TestCase):
    def test_intent_write_failure_means_no_post(self):
        b, c, s, gw, clk = make()

        def fail(op):
            if op == "create_submitting":
                raise sqlite_error()
        s.fail_hook = fail
        # CHANGED (round 2, finding 5): an explicit NOT_SUBMITTED result replaces the exception.
        r = gw.submit(req("t-c1"), "test")
        self.assertEqual((r.state, r.persisted), ("NOT_SUBMITTED", False))
        self.assertEqual(b.posts(), [])                           # safety assertion unchanged

    def test_record_failure_after_accepted_post_is_recovered(self):
        b, c, s, gw, clk = make()
        state = {"n": 0}

        def fail(op):
            if op == "mark_ACCEPTED" and state["n"] == 0:
                state["n"] += 1
                raise sqlite_error()
        s.fail_hook = fail
        # CHANGED (round 2, finding 5): the accepted order is RETURNED with its identity, not raised.
        r = gw.submit(req("t-c2"), "test")
        self.assertEqual((r.state, r.order_id, r.persisted), ("ACCEPTED", b.orders[0]["id"], False))
        self.assertEqual(s.get("t-c2")["state"], "SUBMITTING")     # intent survived (unchanged)
        s.fail_hook = None
        rec = gw.recover_pending()
        self.assertEqual([r.state for r in rec], ["ACCEPTED"])
        self.assertEqual((len(b.posts()), len(b.orders)), (1, 1))


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError("disk I/O error (injected)")


class D_ReconcileAndResubmit(unittest.TestCase):
    def test_crash_before_post_restart_then_window_list_then_single_resubmit(self):
        db = os.path.join(tempfile.mkdtemp(), "i.db")
        b, c, s, gw, clk = make(db=db)
        # Process "crashed" right after the durable intent, before the POST.
        s.create_submitting("t-d1", "test", "BTC/USD", req("t-d1").to_request_fields(), clk.ns())
        s2 = B.IntentStore(db)                                   # restart: fresh store on same file
        gw2 = B.OrderGateway(c, s2, "BTC/USD", now_ns=clk.ns, read_kw=fast_read_kw(clk))
        r = gw2.recover_pending()[0]
        self.assertEqual((r.state, b.posts()), ("UNRESOLVED", []))   # not found, inside window
        self.assertEqual(gw2.resubmit(req("t-d1")).state, "UNRESOLVED")   # still inside window: no POST
        self.assertEqual(b.posts(), [])
        clk.t += 31
        # CHANGED (round 2, finding 3 + policy): elapsed time and negative lookup/list no longer
        # authorize a POST. The intent stays UNRESOLVED and queued; resolving an intent that truly
        # never reached the broker now requires an explicit human decision (not automated).
        r = gw2.resubmit(req("t-d1"))
        self.assertEqual((r.state, len(b.posts())), ("UNRESOLVED", 0))
        self.assertEqual(b.orders, [])
        self.assertIn("t-d1", [x["client_order_id"] for x in s2.pending()])
        self.assertEqual(s2.get("t-d1")["submit_attempts"], 1)

    def test_crash_after_accepted_post_restart_reconciles_without_post(self):
        db = os.path.join(tempfile.mkdtemp(), "i.db")
        b, c, s, gw, clk = make(db=db)
        s.create_submitting("t-d2", "test", "BTC/USD", req("t-d2").to_request_fields(), clk.ns())
        c.submit_order(req("t-d2"))                              # POST reached broker; crash before record
        n = len(b.posts())
        gw2 = B.OrderGateway(c, B.IntentStore(db), "BTC/USD", now_ns=clk.ns, read_kw=fast_read_kw(clk))
        self.assertEqual([x.state for x in gw2.recover_pending()], ["ACCEPTED"])
        self.assertEqual(len(b.posts()), n)

    def test_delayed_visibility_found_by_list_after_window(self):
        b, c, s, gw, clk = make()
        b.post = [("accept_then", 504, J504)]
        b.get_cid = [("status", 404, J404)] * 3                 # by-id lookup lags
        r = gw.submit(req("t-d3"), "test")
        self.assertEqual(r.state, "UNRESOLVED")
        clk.t += 31
        r = gw.reconcile("t-d3")                                 # lookup still 404, list shows it
        self.assertEqual((r.state, r.detail), ("ACCEPTED", "found by list"))
        self.assertEqual(len(b.posts()), 1)

    def test_list_unavailable_keeps_unresolved_never_absent(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 504, J504)]
        gw.submit(req("t-d4"), "test")
        clk.t += 31
        b.list = [("status", 504, J504)] * 10
        r = gw.reconcile("t-d4")
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertEqual(gw.resubmit(req("t-d4")).state, "UNRESOLVED")
        self.assertEqual(len(b.posts()), 1)

    def test_lookup_unavailable_is_not_not_found(self):
        b, c, s, gw, clk = make()
        b.post = [("status", 504, J504)]
        b.get_cid = [("status", 503, "{}")] * 20
        r = gw.submit(req("t-d5"), "test")
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertIn("UNAVAILABLE", s.get("t-d5")["last_error"])


class E_Reads(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self.kw = fast_read_kw(self.clk)

    def test_bounded_by_deadline_and_attempts(self):
        calls = []

        def f():
            calls.append(1)
            raise requests.exceptions.ConnectionError("down")
        r = B.read(f, **self.kw)
        self.assertEqual(r.state, "UNAVAILABLE")
        self.assertLessEqual(len(calls), 5)
        self.assertLess(sum(self.clk.sleeps), 8.0)

    def test_structured_404_is_not_found_without_retry(self):
        b, c, s, gw, clk = make()
        r = B.read(lambda: c.get_order_by_client_id("nope"), **self.kw)
        self.assertEqual((r.state, r.attempts), ("NOT_FOUND", 1))

    def test_free_text_not_found_is_unavailable(self):
        def f():
            raise requests.exceptions.ConnectionError("proxy: resource not found")
        self.assertEqual(B.read(f, **self.kw).state, "UNAVAILABLE")

    def test_transient_then_ok(self):
        seq = [requests.exceptions.ReadTimeout("t"), requests.exceptions.ReadTimeout("t")]

        def f():
            if seq:
                raise seq.pop(0)
            return 42
        r = B.read(f, **self.kw)
        self.assertEqual((r.state, r.value, r.attempts), ("OK", 42, 3))

    def test_non_retryable_4xx_is_error(self):
        b, c, s, gw, clk = make()
        b.get_cid = [("status", 401, json.dumps({"code": 40110000, "message": "unauthorized"}))]
        r = B.read(lambda: c.get_order_by_client_id("x"), **self.kw)
        self.assertEqual((r.state, r.attempts), ("ERROR", 1))

    def test_cancel_204_is_request_accepted_not_terminal_and_422(self):
        b, c, s, gw, clk = make()
        gw.submit(req("t-e6"), "test")
        oid = b.orders[0]["id"]
        r = B.cancel(c, oid, **self.kw)
        self.assertEqual(r.state, "CANCEL_REQUEST_ACCEPTED")
        self.assertEqual(b.orders[0]["status"], "pending_cancel")     # not yet terminal
        b.orders[0]["status"] = "filled"
        self.assertEqual(B.cancel(c, oid, **self.kw).state, "NOT_CANCELABLE")


class F_RealSocketTimeout(unittest.TestCase):
    def test_silent_server_raises_read_timeout_instead_of_hanging(self):
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
            stop.wait(20)
            conn.close()
            s.close()
        threading.Thread(target=srv, daemon=True).start()
        ready.wait(5)
        c = TradingClient("PKP8TESTONLY000000000", "x", url_override=f"http://127.0.0.1:{port[0]}")
        B.configure_client(c, timeout=(1.0, 1.5))               # REAL HTTPAdapter underneath
        t = time.time()
        with self.assertRaises(requests.exceptions.ReadTimeout):
            c.submit_order(req("t-f1"))
        self.elapsed = time.time() - t
        stop.set()
        self.assertLess(self.elapsed, 3.0)
        print(f"\n    real read timeout fired after {self.elapsed:.2f}s (limit 1.5s read)")


class G_FreshProcessRestart(unittest.TestCase):
    CHILD = r'''
import os, sys, json
sys.path.insert(0, os.getcwd())
import test_broker_io as T, broker_io as B
db, state, phase = sys.argv[1], sys.argv[2], sys.argv[3]
b, c, s, gw, clk = T.make(state_file=state, db=db)
if phase == "A":
    b.post = [("accept_then_raise", T.requests.exceptions.ConnectionError("reset"))]
    s.create_submitting("t-g1", "test", "BTC/USD", T.req("t-g1").to_request_fields(), clk.ns())
    try:
        c.submit_order(T.req("t-g1"))
    except Exception:
        pass
    os._exit(17)          # die before recording anything
else:
    res = [r.state for r in gw.recover_pending()]
    print(json.dumps({"recovered": res, "posts_in_B": len(b.posts()), "broker_orders": len(b.orders),
                      "state": s.get("t-g1")["state"]}))
'''

    def test_two_processes_one_logical_order(self):
        d = tempfile.mkdtemp()
        db, state = os.path.join(d, "i.db"), os.path.join(d, "broker.json")
        here = os.path.dirname(os.path.abspath(__file__))
        a = subprocess.run([sys.executable, "-c", self.CHILD, db, state, "A"], cwd=here, capture_output=True, text=True)
        self.assertEqual(a.returncode, 17, a.stderr[-400:])
        bproc = subprocess.run([sys.executable, "-c", self.CHILD, db, state, "B"], cwd=here, capture_output=True, text=True)
        out = json.loads(bproc.stdout.strip().splitlines()[-1])
        self.assertEqual(out, {"recovered": ["ACCEPTED"], "posts_in_B": 0, "broker_orders": 1, "state": "ACCEPTED"})


if __name__ == "__main__":
    unittest.main()


class H_StageA_TimeoutsOnly(unittest.TestCase):
    """Stage A keeps SDK retries (reads unchanged) but bounds each attempt; worst case measured."""

    def test_timeouts_applied_sdk_retries_kept_and_worst_case_bounded(self):
        import alpaca.common.rest as rest
        slept = []
        orig_sleep = rest.time.sleep
        rest.time.sleep = lambda s: slept.append(s)
        try:
            b = SimBroker()
            c = TradingClient("PKP8TESTONLY000000000", "x", paper=True)
            B.apply_timeouts_only(c, adapter=b)
            self.assertEqual(c._retry, 3)                          # unchanged SDK retry
            b.get_cid = [("raise", requests.exceptions.ReadTimeout("rt"))]
            with self.assertRaises(requests.exceptions.ReadTimeout):
                c.get_order_by_client_id("x")                      # transport errors are not SDK-retried
            b.get_cid = [("status", 504, J504)] * 10
            with self.assertRaises(Exception):
                c.get_order_by_client_id("x")
            gets = [x for x in b.calls if x[0] == "GET"]
            self.assertEqual(len(gets), 1 + 4)                     # 1 timeout + 4 SDK attempts on 504
            self.assertEqual(slept, [3, 3, 3])
            self.assertTrue(all(x[2] == (0.5, 1.0) for x in b.calls))
        finally:
            rest.time.sleep = orig_sleep

    def test_stage_a_still_duplicates_posts_documented_risk(self):
        import alpaca.common.rest as rest
        rest_sleep = rest.time.sleep
        rest.time.sleep = lambda s: None
        try:
            b = SimBroker()
            c = TradingClient("PKP8TESTONLY000000000", "x", paper=True)
            B.apply_timeouts_only(c, adapter=b)
            b.post = [("accept_then", 504, J504)] * 4
            with self.assertRaises(Exception):
                c.submit_order(req("t-h2"))
            self.assertEqual((len(b.posts()), len(b.orders)), (4, 4))   # risk remains in Stage A
        finally:
            rest.time.sleep = rest_sleep
