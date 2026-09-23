"""Round-3 regression tests: unreadable history, identity validation, duplicate client ids,
bounded pagination, and the human-resolution mechanism.

Run: cd review/p8_sdk_transport/proposed && /usr/bin/python3 -m unittest -v test_p8_round3
Same conventions as rounds 1-2: network blocked, real alpaca-py 0.43.5 client, simulated broker
under the timeout adapter, written BEFORE the fix and run unchanged against baseline bb2a2f8.
Broker behaviors marked HYPOTHETICAL are not documented by Alpaca (symbol/decimal normalization,
duplicate client ids, notional/qty reporting); tests require safety under them.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import test_broker_io as T
import test_p8_round2 as R2
import broker_io as B
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

T0 = datetime(2026, 9, 23, 14, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Broker3(R2.Broker2):
    """Adds query-aware, paginated order listing with per-order timestamps, a broker 'view'
    transform (normalization), page failures, and injected foreign/duplicate orders."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seq = 0
        self.view = lambda o: o                 # HYPOTHETICAL broker normalization of OUR orders
        self.fail_pages = set()                 # 1-based list-page numbers that return 503
        self.list_calls = 0

    def stamp(self, o):
        self.seq += 1
        o["created_at"] = o["submitted_at"] = o["updated_at"] = iso(T0 + timedelta(milliseconds=self.seq))
        return o

    def add_foreign(self, n, cid_prefix="foreign"):
        for i in range(n):
            self.orders.append(self.stamp(T.order_json(str(T.uuid.uuid4()), f"{cid_prefix}-{self.seq}-{i}",
                                                       {"qty": 0.0001, "type": "limit", "side": "buy",
                                                        "time_in_force": "gtc", "limit_price": 1})))

    def add_duplicate_of(self, cid, body):
        self.orders.append(self.stamp(T.order_json(str(T.uuid.uuid4()), cid, body)))

    def _transport_send(self, req, **kw):
        path = req.url.split("/v2")[-1]
        before = len(self.orders)
        if not (req.method == "GET" and path.startswith("/orders") and "by_client_order_id" not in path):
            r = super()._transport_send(req, **kw)
            for o in self.orders[before:]:
                self.stamp(o)
            if req.method == "GET" and "by_client_order_id" in path and r.status_code == 200:
                r._content = json.dumps(self.view(json.loads(r._content))).encode()
            return r
        # --- paginated list: GET /orders?status=all&after=...&limit=...&direction=asc&symbols=...
        self.calls.append((req.method, "/orders", kw.get("timeout")))
        self.list_calls += 1
        if self.list_calls in self.fail_pages:
            return self._resp(req, 503, "{}")
        q = {k: v[0] for k, v in parse_qs(urlparse(req.url).query).items()}
        after = datetime.fromisoformat(q["after"].replace("Z", "+00:00")) if "after" in q else None
        limit = int(q.get("limit", 50))
        rows = [o for o in self.orders if o["client_order_id"] not in self.hidden]
        rows.sort(key=lambda o: o["created_at"], reverse=(q.get("direction") == "desc"))
        if after:
            rows = [o for o in rows if datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")) > after]
        return self._resp(req, 200, json.dumps([self.view(dict(o)) for o in rows[:limit]]))


def make3(db=None):
    broker = Broker3()
    client = T.TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real", paper=True)
    B.configure_client(client, adapter=broker)
    clock = T.Clock()
    clock.t = T0.timestamp()                    # align gateway clock with broker timestamps
    db = db or os.path.join(tempfile.mkdtemp(), "intents.db")
    gw = B.OrderGateway(client, B.IntentStore(db), "BTC/USD", now_ns=clock.ns, read_kw=T.fast_read_kw(clock))
    return broker, client, gw, clock, db


def posts(b):
    return [c for c in b.calls if c[0] == "POST"]


def st(db, cid):
    return B.IntentStore(db).get(cid)


def queued(db):
    return [r["client_order_id"] for r in B.IntentStore(db).pending()]


def notional_req(cid, notional=500):
    return MarketOrderRequest(symbol="BTC/USD", notional=notional, side=OrderSide.BUY,
                              time_in_force=TimeInForce.GTC, client_order_id=cid)


class G1_UnreadableHistory(unittest.TestCase):
    """Codex-reproduced defect: 'no POST in this call' was reported as 'never submitted'."""

    def break_get(self, gw):
        def boom(cid):
            raise T.sqlite_error()
        gw.store.get = boom

    def test_resubmit_call_with_unreadable_store_is_not_not_submitted(self):
        b, c, gw, clk, db = make3()
        r1 = gw.submit(T.req("r3-g1"), "test")
        self.assertEqual(r1.state, "ACCEPTED")
        real_get = gw.store.get
        self.break_get(gw)
        r2, err = R2.call_safely(lambda: gw.submit(T.req("r3-g1"), "test"))
        self.assertIsNone(err)
        self.assertNotEqual(r2.state, "NOT_SUBMITTED", "unreadable history reported as never submitted")
        self.assertTrue(r2.exposure_may_exist)
        self.assertEqual(r2.client_order_id, "r3-g1")
        self.assertIs(r2.persisted, False)
        self.assertEqual(len(posts(b)), 1)
        gw.store.get = real_get                          # recovery once the store is readable
        r3 = gw.submit(T.req("r3-g1"), "test")
        self.assertEqual((r3.state, r3.order_id, len(posts(b))), ("ACCEPTED", b.orders[0]["id"], 1))

    def test_unreadable_store_and_broker_unavailable_stays_unresolved(self):
        b, c, gw, clk, db = make3()
        gw.submit(T.req("r3-g1b"), "test")
        self.break_get(gw)
        b.get_cid = [("status", 503, "{}")] * 20
        r, err = R2.call_safely(lambda: gw.submit(T.req("r3-g1b"), "test"))
        self.assertIsNone(err)
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertTrue(r.exposure_may_exist)
        self.assertEqual(len(posts(b)), 1)

    def test_readable_empty_history_and_insert_failure_is_not_submitted_control(self):
        b, c, gw, clk, db = make3()

        def fail(op):
            if op == "create_submitting":
                raise T.sqlite_error()
        gw.store.fail_hook = fail
        r = gw.submit(T.req("r3-g1c"), "test")
        self.assertEqual((r.state, len(posts(b))), ("NOT_SUBMITTED", 0))


class G2_IdentityValidation(unittest.TestCase):
    def test_broker_normalization_still_matches(self):
        b, c, gw, clk, db = make3()
        b.view = lambda o: dict(o, symbol="BTCUSD", qty="0.00020000", limit_price="100000.00")  # HYPOTHETICAL
        b.post = [("accept_then", 504, T.J504)]
        r = gw.submit(T.req("r3-g2a"), "test")
        self.assertEqual((r.state, r.order_id), ("ACCEPTED", b.orders[0]["id"]))

    def test_side_mismatch_is_conflict_not_accepted(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, side="buy")           # broker shows a different order under our id
        r = gw.submit(T.req("r3-g2b"), "test")
        self.assertEqual(r.state, "CONFLICT")
        self.assertEqual(st(db, "r3-g2b")["state"], "CONFLICT")
        self.assertIn("r3-g2b", queued(db))

    def test_qty_mismatch_is_conflict(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, qty="0.0003")
        self.assertEqual(gw.submit(T.req("r3-g2c"), "test").state, "CONFLICT")

    def test_limit_price_mismatch_is_conflict(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, limit_price="99999.99")
        self.assertEqual(gw.submit(T.req("r3-g2d"), "test").state, "CONFLICT")

    def test_notional_intent_matches_on_notional_not_qty(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, notional="500.00", qty=None, type="market", side="buy",
                                time_in_force="gtc", limit_price=None)          # HYPOTHETICAL reporting
        r = gw.submit(notional_req("r3-g2e"), "test")
        self.assertEqual(r.state, "ACCEPTED")

    def test_notional_amount_mismatch_is_conflict(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, notional="400", qty=None, type="market", side="buy",
                                time_in_force="gtc", limit_price=None)
        self.assertEqual(gw.submit(notional_req("r3-g2f"), "test").state, "CONFLICT")

    def test_notional_intent_but_broker_reports_only_qty_is_conflict(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, notional=None, qty="0.0058", type="market", side="buy",
                                time_in_force="gtc", limit_price=None)
        self.assertEqual(gw.submit(notional_req("r3-g2g"), "test").state, "CONFLICT")


class G3_DuplicateClientIds(unittest.TestCase):
    def test_two_broker_orders_share_our_client_id_is_conflict(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.hidden.add("r3-g3")                            # stay unresolved first
        gw.submit(T.req("r3-g3"), "test")
        body = {"qty": 0.0002, "type": "limit", "side": "sell", "time_in_force": "ioc", "limit_price": 100000}
        b.add_duplicate_of("r3-g3", body)                # HYPOTHETICAL: broker accepted a duplicate id
        b.hidden.discard("r3-g3")
        clk.t += 31
        r = gw.reconcile("r3-g3")
        self.assertEqual(r.state, "CONFLICT", f"got {r.state}: {r.detail}")
        ids = {o["id"] for o in b.orders if o["client_order_id"] == "r3-g3"}
        self.assertEqual(len(ids), 2)
        for i in ids:
            self.assertIn(i, (st(db, "r3-g3")["last_error"] or "") + r.detail)
        self.assertIn("r3-g3", queued(db))


class G4_BoundedPagination(unittest.TestCase):
    def hidden_then_listed(self, foreign_before, **gwkw):
        b, c, gw, clk, db = make3()
        b.add_foreign(foreign_before)
        b.post = [("accept_then", 504, T.J504)]
        b.get_cid = [("status", 404, T.J404)] * 50       # by-id lookup keeps lagging
        gw.submit(T.req("r3-g4"), "test")
        clk.t += 31
        return b, c, gw, clk, db

    def test_order_on_page_three_is_found_by_bounded_scan(self):
        b, c, gw, clk, db = self.hidden_then_listed(1200)   # ours sorts after 1200 foreign orders
        r = gw.reconcile("r3-g4")
        self.assertEqual((r.state, r.order_id), ("ACCEPTED", b.orders[-1]["id"]))

    def test_exhausted_page_bound_is_not_absence(self):
        b, c, gw, clk, db = self.hidden_then_listed(5000)   # beyond any small page bound
        r = gw.reconcile("r3-g4")
        self.assertEqual(r.state, "UNRESOLVED")
        self.assertIn("r3-g4", queued(db))
        self.assertEqual(len(posts(b)), 1)

    def test_page_failure_is_not_absence(self):
        b, c, gw, clk, db = self.hidden_then_listed(1200)
        b.fail_pages = {b.list_calls + 2}                # the 2nd page of the next scan fails
        r = gw.reconcile("r3-g4")
        self.assertIn(r.state, ("UNRESOLVED",))
        self.assertIn("r3-g4", queued(db))
        self.assertEqual(len(posts(b)), 1)


class G5_HumanResolution(unittest.TestCase):
    def unresolved(self):
        b, c, gw, clk, db = make3()
        b.post = [("raise", T.requests.exceptions.ConnectTimeout("ct"))]   # never reached the broker
        gw.submit(T.req("r3-g5"), "test")
        return b, c, gw, clk, db

    def test_acknowledge_changes_nothing_but_the_record(self):
        b, c, gw, clk, db = self.unresolved()
        r = gw.acknowledge("r3-g5", operator="david", note="saw the alert")
        self.assertEqual(st(db, "r3-g5")["state"], "UNRESOLVED")
        self.assertIn("r3-g5", queued(db))
        self.assertTrue(gw.entries_locked("BTC/USD")[0])
        self.assertTrue(any(e["kind"] == "ACKNOWLEDGED" for e in gw.store.events("r3-g5")))
        self.assertEqual(len(posts(b)), 1)

    def test_abandon_keeps_history_monitoring_and_entry_lock(self):
        b, c, gw, clk, db = self.unresolved()
        n_events = len(gw.store.events("r3-g5"))
        r = gw.abandon("r3-g5", operator="david", note="checked dashboard: no order")
        self.assertEqual(st(db, "r3-g5")["state"], "ABANDONED")
        self.assertGreater(len(gw.store.events("r3-g5")), n_events)       # history appended, not erased
        self.assertIn("r3-g5", queued(db))                                # still monitored
        self.assertTrue(gw.entries_locked("BTC/USD")[0])                  # NOT unlocked by abandon
        self.assertEqual(len(posts(b)), 1)

    def test_abandon_refused_when_fresh_check_finds_the_order(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.hidden.add("r3-g5b")
        gw.submit(T.req("r3-g5b"), "test")
        b.hidden.discard("r3-g5b")
        r = gw.abandon("r3-g5b", operator="david", note="x")
        self.assertEqual(st(db, "r3-g5b")["state"], "ACCEPTED")
        self.assertNotEqual(r.state, "ABANDONED")

    def test_late_order_after_abandon_becomes_conflict_and_relocks(self):
        b, c, gw, clk, db = self.unresolved()
        gw.abandon("r3-g5", operator="david", note="x")
        gw.release_entry_lock("r3-g5", operator="david", note="verified in dashboard")
        self.assertFalse(gw.entries_locked("BTC/USD")[0])
        body = {"qty": 0.0002, "type": "limit", "side": "sell", "time_in_force": "ioc", "limit_price": 100000}
        b.add_duplicate_of("r3-g5", body)               # the "never sent" order shows up late
        gw.recover_pending()
        self.assertEqual(st(db, "r3-g5")["state"], "CONFLICT")
        self.assertTrue(gw.entries_locked("BTC/USD")[0])

    def test_release_requires_abandoned_and_is_explicit(self):
        b, c, gw, clk, db = self.unresolved()
        r = gw.release_entry_lock("r3-g5", operator="david", note="x")   # not abandoned yet
        self.assertTrue(gw.entries_locked("BTC/USD")[0])
        self.assertNotEqual(getattr(r, "state", None), "LOCK_RELEASED")

    def test_conflict_cannot_be_abandoned(self):
        b, c, gw, clk, db = make3()
        b.post = [("accept_then", 504, T.J504)]
        b.view = lambda o: dict(o, side="buy")
        gw.submit(T.req("r3-g5c"), "test")
        gw.abandon("r3-g5c", operator="david", note="x")
        self.assertEqual(st(db, "r3-g5c")["state"], "CONFLICT")

    def test_resubmission_still_disabled(self):
        b, c, gw, clk, db = self.unresolved()
        clk.t += 3600
        gw.resubmit(T.req("r3-g5"))
        self.assertEqual(len(posts(b)), 1)


if __name__ == "__main__":
    unittest.main()
