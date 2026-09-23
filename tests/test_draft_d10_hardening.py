"""D10 snapshot-check hardening (Codex review of 8ac5cba). Draft-gated: DRAFT_R1V2=1.
Pure/offline: no broker, no DB writes. Snapshot schema v2 requires explicit lists, strict timestamps, valid finite
quantities, well-formed orders/fills, and an explicit COMPLETENESS attestation (pagination + coverage window);
fills_since alone is insufficient. live_gate(snapshot, live, expected) revalidates identity, freshness and
authoritative order details at apply time."""
import copy
import importlib.util
import os
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ON = os.environ.get("DRAFT_R1V2") == "1"
NAN, INF = float("nan"), float("inf")


def m():
    spec = importlib.util.spec_from_file_location("d10", HERE.parent / "review" / "drafts" / "d10_snapshot_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def order(**kw):
    o = {"id": "s9", "client_order_id": None, "symbol": "BTC/USD", "side": "sell", "status": "new", "qty": 0.3,
         "filled_qty": 0.0, "type": "stop_limit", "stop_price": 90.0, "limit_price": 89.9}
    o.update(kw)
    return o


def fill(**kw):
    f = {"order_id": "o1", "client_order_id": "x", "symbol": "BTC/USD", "side": "sell", "qty": 0.2, "price": 100.0,
         "time": "2026-09-23T15:00:00+00:00"}
    f.update(kw)
    return f


def base():
    snap = {"schema": 2, "taken_at": "2026-09-23T18:00:00+00:00", "environment": "paper", "account_key": "paper:abc123",
            "symbol": "BTC/USD", "position_qty": 0.3, "open_orders": [order()], "fills": [fill()],
            "fills_since": "2026-09-23T00:00:00+00:00",   # kept so schema-1 code evaluates it (not relied on by v2)
            "completeness": {"orders": {"has_more": False, "pages": 1},
                             "fills": {"has_more": False, "pages": 1, "covers_from": "2026-09-23T00:00:00+00:00",
                                       "covers_to": "2026-09-23T18:00:00+00:00"}}}
    db = {"breaker_tripped_stamp": "2026-09-23", "position_state": {"stop_order_id": "s9"}, "liquidation_state": {}}
    exp = {"environment": "paper", "account_key": "paper:abc123", "symbol": "BTC/USD", "now": "2026-09-23T18:05:00+00:00",
           "max_age_s": 600}
    return snap, db, exp


def live_from(snap, **kw):
    lv = {"environment": snap["environment"], "account_key": snap["account_key"], "symbol": snap["symbol"],
          "read_at": "2026-09-23T18:04:00+00:00", "position_qty": snap["position_qty"],
          "open_orders": copy.deepcopy(snap["open_orders"]), "complete": True}
    lv.update(kw)
    return lv


@unittest.skipUnless(ON, "draft")
class A_SnapshotValidation(unittest.TestCase):
    def verdict(self, mutate):
        s, d, e = base()
        mutate(s, d, e)
        return m().check(s, d, e)

    def test_valid_snapshot_ok(self):
        r = m().check(*base())
        self.assertEqual(r["verdict"], "OK_FOR_REVIEW", r)

    def test_invalid_position_quantities_block(self):
        for v in (NAN, INF, -INF, -0.1, True, False, "0.3", None):
            with self.subTest(qty=v):
                self.assertEqual(self.verdict(lambda s, d, e: s.update(position_qty=v))["verdict"], "BLOCK")

    def test_missing_or_malformed_lists_block(self):
        for label, f in {"no open_orders": lambda s, d, e: s.pop("open_orders"),
                         "no fills": lambda s, d, e: s.pop("fills"),
                         "orders not list": lambda s, d, e: s.update(open_orders={}),
                         "fills not list": lambda s, d, e: s.update(fills="none")}.items():
            with self.subTest(label):
                self.assertEqual(self.verdict(f)["verdict"], "BLOCK")

    def test_invalid_timestamps_block(self):
        for label, f in {"naive": lambda s, d, e: s.update(taken_at="2026-09-23T18:00:00"),
                         "garbage": lambda s, d, e: s.update(taken_at="yesterday"),
                         "missing": lambda s, d, e: s.pop("taken_at"),
                         "bad now": lambda s, d, e: e.update(now="soon")}.items():
            with self.subTest(label):
                self.assertEqual(self.verdict(f)["verdict"], "BLOCK")

    def test_malformed_orders_block(self):
        bad = {"no id": order(id=None), "empty id": order(id=""), "bad side": order(side="x"),
               "nan qty": order(qty=NAN), "bool qty": order(qty=True), "zero qty": order(qty=0.0),
               "neg filled": order(filled_qty=-1.0), "filled>qty": order(filled_qty=0.5),
               "symbol": order(symbol="ETH/USD"), "no status": order(status=None), "not dict": "s9",
               "nan price": order(limit_price=NAN)}
        for label, o in bad.items():
            with self.subTest(label):
                self.assertEqual(self.verdict(lambda s, d, e: s.update(open_orders=[o]))["verdict"], "BLOCK")

    def test_malformed_fills_block(self):
        bad = {"neg qty": fill(qty=-0.2), "nan price": fill(price=NAN), "bad time": fill(time="noon"),
               "naive time": fill(time="2026-09-23T15:00:00"), "bad side": fill(side="?"),
               "symbol": fill(symbol="ETH/USD"), "no order id": fill(order_id=None), "not dict": 7,
               "outside coverage": fill(time="2026-09-22T23:00:00+00:00")}
        for label, f_ in bad.items():
            with self.subTest(label):
                self.assertEqual(self.verdict(lambda s, d, e: s.update(fills=[f_]))["verdict"], "BLOCK")

    def test_completeness_evidence_required(self):
        cases = {"missing": lambda s, d, e: s.pop("completeness"),
                 "orders has_more": lambda s, d, e: s["completeness"]["orders"].update(has_more=True),
                 "fills has_more": lambda s, d, e: s["completeness"]["fills"].update(has_more=True),
                 "pages missing": lambda s, d, e: s["completeness"]["fills"].pop("pages"),
                 "pages bool": lambda s, d, e: s["completeness"]["orders"].update(pages=True),
                 "covers_from after day start": lambda s, d, e: s["completeness"]["fills"].update(
                     covers_from="2026-09-23T06:00:00+00:00"),
                 "covers_to before taken_at": lambda s, d, e: s["completeness"]["fills"].update(
                     covers_to="2026-09-23T17:00:00+00:00"),
                 "fills_since only (schema 1)": lambda s, d, e: s.pop("completeness")}
        for label, f in cases.items():
            with self.subTest(label):
                self.assertEqual(self.verdict(f)["verdict"], "BLOCK")


@unittest.skipUnless(ON, "draft")
class B_ApplyTimeGate(unittest.TestCase):
    def gate(self, mutate_live=None, mutate_exp=None):
        s, d, e = base()
        lv = live_from(s)
        if mutate_live:
            mutate_live(lv)
        if mutate_exp:
            mutate_exp(e)
        return m().live_gate(s, lv, e)

    def test_matching_fresh_live_state_passes(self):
        ok, why = self.gate()
        self.assertTrue(ok, why)

    def test_invalid_live_quantities_refused_even_if_equal(self):
        for v in (NAN, INF, -0.1, True):
            with self.subTest(qty=v):
                s, d, e = base()
                s["position_qty"] = v
                self.assertFalse(m().live_gate(s, live_from(s), e)[0])

    def test_wrong_account_environment_symbol_with_identical_qty_and_ids_refused(self):
        for k, v in (("account_key", "paper:OTHER"), ("environment", "live"), ("symbol", "ETH/USD")):
            with self.subTest(k):
                self.assertFalse(self.gate(lambda lv: lv.update({k: v}))[0])

    def test_snapshot_identity_must_match_expected_too(self):
        s, d, e = base()
        s["account_key"] = "paper:OTHER"
        lv = live_from(s)                                   # live agrees with the (wrong) snapshot
        self.assertFalse(m().live_gate(s, lv, e)[0])

    def test_freshness(self):
        self.assertFalse(self.gate(lambda lv: lv.update(read_at="2026-09-23T17:00:00+00:00"))[0])   # stale / before snapshot
        self.assertFalse(self.gate(lambda lv: lv.update(read_at="2026-09-23T18:30:00+00:00"))[0])   # after 'now'
        self.assertFalse(self.gate(lambda lv: lv.update(read_at="bad"))[0])
        self.assertFalse(self.gate(mutate_exp=lambda e: e.update(now="2026-09-23T19:00:00+00:00"))[0])  # snapshot too old

    def test_same_order_id_with_changed_details_refused(self):
        for field, val in (("status", "partially_filled"), ("filled_qty", 0.1), ("qty", 0.25), ("side", "buy"),
                           ("client_order_id", "other"), ("limit_price", 50.0), ("type", "limit")):
            with self.subTest(field):
                self.assertFalse(self.gate(lambda lv: lv["open_orders"][0].update({field: val}))[0])

    def test_incomplete_live_read_refused(self):
        self.assertFalse(self.gate(lambda lv: lv.update(complete=False))[0])
        self.assertFalse(self.gate(lambda lv: lv.update(open_orders=None))[0])


if __name__ == "__main__":
    unittest.main()
