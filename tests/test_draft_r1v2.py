"""DRAFT R1 v2 + D10 snapshot-check tests. Skipped unless DRAFT_R1V2=1. Run:
  DRAFT_R1V2=1 BOT_SOURCE=review/drafts/crypto_trading_bot_R1v2.py \\
    python3 -m unittest discover -s tests -t tests -p 'test_draft_r1v2.py' -v
Fake broker only. Assertions count SUBMISSIONS; broker rejection is never relied on."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p0_1_harness as H                                                      # noqa: E402
from test_stage5_liquidation import breaker_ns, pending, restart, SYM          # noqa: E402
from test_stage5d_visibility import delayed_visibility, market_submissions     # noqa: E402

ON = os.environ.get("DRAFT_R1V2") == "1"


def clocked(ns, t0=0.0):
    T = [t0]
    ns["_r1_now"] = lambda: T[0]
    return T


def invisible_attempt(t0=0.0):
    ns = breaker_ns(0.5)
    T = clocked(ns, t0)
    delayed_visibility(ns["_broker"])
    H.cycle(ns)                                  # attempt 1 accepted; response lost; invisible
    return ns, T


def step(ns, T, t):
    T[0] = t
    H.cycle(ns)
    return pending(ns)


@unittest.skipUnless(ON, "draft R1 v2")
class G1_MissingMetadata(unittest.TestCase):
    def test_pending_record_without_inflight_is_reconstructed_conservatively(self):
        ns, T = invisible_attempt()
        liq = dict(pending(ns))
        liq.pop("inflight", None)                  # e.g. a record written by the committed (pre-R1) code
        ns["db"].liquidations[SYM] = liq
        liq = step(ns, T, 5000.0)                  # long after submit
        self.assertEqual(liq["inflight"]["origin"], "reconstructed")
        self.assertEqual(liq["inflight"]["since"], 5000.0, "bound must start at first observation")
        self.assertNotIn("escalated", liq)
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)

    def test_attempt_without_any_identity_escalates_immediately_without_selling(self):
        ns = breaker_ns(0.5)
        clocked(ns)
        ns["db"].set_liquidation_state(SYM, reason="r", day_stamp="2026-09-23", attempt=1, order_id=None,
                                       client_order_id=None, started="x")
        H.cycle(ns)
        self.assertEqual(pending(ns)["escalated"]["kind"], "NO_IDENTITY")
        self.assertEqual(market_submissions(ns["_broker"]), [])


@unittest.skipUnless(ON, "draft R1 v2")
class G2_OutcomeMatrix(unittest.TestCase):
    def open_attempt(self):
        ns = breaker_ns(0.5)
        T = clocked(ns)
        b = ns["_broker"]
        orig = b._accept

        def accept(req):                           # market sell accepted, working, unfilled, VISIBLE
            if getattr(req, "_kind", None) == "market" and req.side == "sell":
                b._n += 1
                oid = f"o{b._n}"
                b.add_order(oid, "market", float(req.qty), status="accepted", filled=0.0,
                            client_order_id=getattr(req, "client_order_id", None))
                return NS(id=oid, legs=[])
            return orig(req)
        b._accept = accept
        H.cycle(ns)
        return ns, T, b, pending(ns)["order_id"]

    def test_unknown_lookup_escalates_at_bound_and_never_resubmits(self):
        ns, T, b, oid = self.open_attempt()
        b.unknown_ids.add(oid)
        for t in (100, 200, 900):
            step(ns, T, t)
        esc = pending(ns).get("escalated")
        self.assertTrue(esc and esc["kind"] == "UNKNOWN", pending(ns))
        self.assertEqual(len(market_submissions(b)), 1)

    def test_not_found_by_order_id_escalates_at_bound(self):
        ns, T, b, oid = self.open_attempt()
        orig = b.get_order_by_id
        b.get_order_by_id = lambda x: (_ for _ in ()).throw(H.FakeAPIError(404, 40410000, "nf")) if x == oid else orig(x)
        for t in (100, 200, 900):
            step(ns, T, t)
        self.assertEqual(pending(ns)["escalated"]["kind"], "NOT_FOUND_BY_ID")
        self.assertEqual(len(market_submissions(b)), 1)

    def test_visible_working_order_never_escalates(self):
        ns, T, b, oid = self.open_attempt()
        for t in (1000, 5000, 100000):
            liq = step(ns, T, t)
        self.assertNotIn("escalated", liq)
        self.assertEqual(len(market_submissions(b)), 1)


@unittest.skipUnless(ON, "draft R1 v2")
class G3_BoundariesAndRestart(unittest.TestCase):
    def test_two_checks_at_window_do_not_escalate(self):
        ns, T = invisible_attempt()
        step(ns, T, 900.0)
        liq = step(ns, T, 900.0)
        self.assertEqual(liq["inflight"]["checks"], 2)
        self.assertNotIn("escalated", liq)

    def test_three_checks_just_before_window_do_not_escalate(self):
        ns, T = invisible_attempt()
        for t in (10.0, 20.0, 899.999):
            liq = step(ns, T, t)
        self.assertEqual(liq["inflight"]["checks"], 3)
        self.assertNotIn("escalated", liq)

    def test_three_checks_exactly_at_window_escalate(self):
        ns, T = invisible_attempt()
        for t in (10.0, 20.0, 900.0):
            liq = step(ns, T, t)
        self.assertEqual(liq["escalated"]["checks"], 3)
        self.assertEqual(liq["escalated"]["waited_s"], 900.0)

    def test_restart_after_escalation_keeps_it_without_resubmit_or_entry(self):
        ns, T = invisible_attempt()
        for t in (10.0, 20.0, 900.0):
            step(ns, T, t)
        esc = dict(pending(ns)["escalated"])
        n2 = restart(ns)
        T2 = clocked(n2, 5000.0)
        n2["get_today_pl"] = lambda baseline: 0.0
        n2["strategies"].get_signal = lambda *a, **k: "buy"
        n2["strategies"].is_volume_confirmed = lambda *a, **k: True
        for t in (5000.0, 9000.0):
            step(n2, T2, t)
        self.assertEqual(pending(n2)["escalated"], esc, "escalation changed/duplicated after restart")
        self.assertEqual(len(market_submissions(n2["_broker"])), 1)
        self.assertEqual([r for r in n2["_broker"].submitted if getattr(r, "side", None) == "buy"], [])

    def test_broker_visibility_after_escalation_resolves_with_history(self):
        ns, T = invisible_attempt()
        for t in (10.0, 20.0, 900.0):
            step(ns, T, t)
        ns["_broker"].reveal(fill=True)
        step(ns, T, 1000.0)
        self.assertFalse(pending(ns))
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)


@unittest.skipUnless(ON, "draft R1 v2")
class G4_OperatorRecovery(unittest.TestCase):
    def escalated_never_sent(self):
        ns = breaker_ns(0.5)
        T = clocked(ns)
        ns["_broker"].drop_order_kinds = {"market"}      # never reached the broker (bot cannot know that)
        H.cycle(ns)
        ns["_broker"].drop_order_kinds = set()
        for t in (10.0, 20.0, 900.0):
            step(ns, T, t)
        self.assertTrue(pending(ns).get("escalated"))
        return ns, T

    def test_valid_not_placed_resolution_authorizes_exactly_one_new_attempt(self):
        ns, T = self.escalated_never_sent()
        liq = dict(pending(ns))
        liq["operator_resolution"] = {"kind": "not_placed", "cid": liq["client_order_id"],
                                      "evidence": "broker order history export 2026-09-23T12:00Z, no such cid",
                                      "by": "David"}
        ns["db"].liquidations[SYM] = liq
        step(ns, T, 1000.0)
        b = ns["_broker"]
        self.assertEqual(len([o for o in b.orders.values() if o.kind == "market"]), 1, "not exactly one new attempt")
        self.assertAlmostEqual(b.qty, 0.0)
        self.assertEqual(len(ns["_marks"]), 1)

    def test_invalid_resolution_is_rejected_and_nothing_is_sold(self):
        ns, T = self.escalated_never_sent()
        liq = dict(pending(ns))
        liq["operator_resolution"] = {"kind": "not_placed", "cid": liq["client_order_id"], "by": "David"}  # no evidence
        ns["db"].liquidations[SYM] = liq
        liq = step(ns, T, 1000.0)
        self.assertIn("rejected", liq["operator_resolution"])
        self.assertTrue(liq.get("escalated"))
        self.assertEqual([o for o in ns["_broker"].orders.values() if o.kind == "market"], [])

    def test_found_resolution_reconciles_the_named_order(self):
        ns, T = invisible_attempt()
        for t in (10.0, 20.0, 900.0):
            step(ns, T, t)
        oid = next(o.id for o in ns["_broker"].orders.values() if o.kind == "market")
        liq = dict(pending(ns))
        liq["operator_resolution"] = {"kind": "found", "order_id": oid, "by": "David"}
        ns["db"].liquidations[SYM] = liq
        ns["_broker"].reveal(fill=True)
        step(ns, T, 1000.0)
        self.assertFalse(pending(ns))
        self.assertEqual(len(market_submissions(ns["_broker"])), 1)


def _d10():
    spec = importlib.util.spec_from_file_location("d10", HERE.parent / "review" / "drafts" / "d10_snapshot_check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def good():
    snap = {"taken_at": "2026-09-23T18:00:00Z", "environment": "paper", "account_key": "paper:abc123", "symbol": "BTC/USD",
            "position_qty": 0.3, "open_orders": [{"id": "s9", "client_order_id": None, "side": "sell", "status": "new",
                                                   "qty": 0.3, "filled_qty": 0, "type": "stop_limit"}],
            "fills_since": "2026-09-23T00:00:00Z",
            "fills": [{"order_id": "o1", "client_order_id": "x", "side": "sell", "qty": 0.2, "price": 1, "time": "2026-09-23T15:00:00Z"}]}
    db = {"breaker_tripped_stamp": "2026-09-23", "position_state": {"stop_order_id": "s9"}, "liquidation_state": {}}
    exp = {"environment": "paper", "account_key": "paper:abc123", "symbol": "BTC/USD", "now": "2026-09-23T18:05:00Z",
           "max_age_s": 600}
    return snap, db, exp


@unittest.skipUnless(ON, "draft D10 check")
class G5_D10Snapshot(unittest.TestCase):
    def test_clean_snapshot_is_ok_for_review_and_detects_condition(self):
        r = _d10().check(*good())
        self.assertEqual(r["verdict"], "OK_FOR_REVIEW", r)
        self.assertTrue(r["facts"]["d10_condition"])

    def test_each_blocking_reason(self):
        cases = {
            "environment": lambda s, d, e: s.update(environment="live"),
            "account": lambda s, d, e: s.update(account_key="paper:other"),
            "symbol": lambda s, d, e: s.update(symbol="ETH/USD"),
            "stale": lambda s, d, e: e.update(now="2026-09-23T19:00:00Z"),
            "future": lambda s, d, e: s.update(taken_at="2026-09-23T18:10:00Z"),
            "fills incomplete": lambda s, d, e: s.update(fills_since="2026-09-23T12:00:00Z"),
            "qty unknown": lambda s, d, e: s.update(position_qty=None),
            "unknown status": lambda s, d, e: s["open_orders"][0].update(status="weird"),
            "terminal listed open": lambda s, d, e: s["open_orders"][0].update(status="filled"),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                snap, db, exp = good()
                mutate(snap, db, exp)
                self.assertEqual(_d10().check(snap, db, exp)["verdict"], "BLOCK", label)

    def test_flags_unrecognized_orders_reentry_and_unseen_attempt(self):
        snap, db, exp = good()
        snap["open_orders"].append({"id": "zz", "client_order_id": "manual", "side": "sell", "status": "new",
                                    "qty": 0.1, "filled_qty": 0, "type": "limit"})
        snap["fills"].append({"order_id": "b1", "client_order_id": None, "side": "buy", "qty": 0.1, "price": 1,
                              "time": "2026-09-23T17:00:00Z"})
        db["liquidation_state"] = {"client_order_id": "liq-1"}
        r = _d10().check(snap, db, exp)
        self.assertEqual(r["verdict"], "FLAGS")
        text = " ".join(r["flags"])
        self.assertIn("unrecognized open sell order zz", text)
        self.assertIn("re-entry", text)
        self.assertIn("NOT proof it was never placed", text)

    def test_live_gate(self):
        m = _d10()
        snap = good()[0]
        self.assertTrue(m.live_gate(snap, 0.3, ["s9"])[0])
        self.assertFalse(m.live_gate(snap, 0.29, ["s9"])[0])
        self.assertFalse(m.live_gate(snap, 0.3, ["s9", "new"])[0])
        self.assertFalse(m.live_gate(snap, None, ["s9"])[0])


if __name__ == "__main__":
    unittest.main()
