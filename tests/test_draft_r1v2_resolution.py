"""R1 v2 operator-resolution binding (Codex review of 8ac5cba). Draft-gated: DRAFT_R1V2=1 with
BOT_SOURCE=review/drafts/crypto_trading_bot_R1v2.py. Fake broker only; assertions count SUBMISSIONS.
Resolution schema 2 (bound): {schema, kind, attempt, cid, escalation_id, nonce, evidence, by, [order_id]}.
A nonempty by/evidence is NOT authentication; records are assertions the bot verifies where it can."""
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


class Crash(BaseException):
    pass


def escalated_hidden(t0=0.0):
    """Original sell accepted, response lost, stays HIDDEN (never revealed); escalated at the bound."""
    ns = breaker_ns(0.5)
    T = [t0]
    ns["_r1_now"] = lambda: T[0]
    delayed_visibility(ns["_broker"])
    H.cycle(ns)
    for t in (10.0, 20.0, 900.0):
        T[0] = t
        H.cycle(ns)
    assert pending(ns).get("escalated"), pending(ns)
    return ns, T


def escalated_never_sent():
    ns = breaker_ns(0.5)
    T = [0.0]
    ns["_r1_now"] = lambda: T[0]
    ns["_broker"].drop_order_kinds = {"market"}
    H.cycle(ns)
    ns["_broker"].drop_order_kinds = set()
    for t in (10.0, 20.0, 900.0):
        T[0] = t
        H.cycle(ns)
    assert pending(ns).get("escalated"), pending(ns)
    return ns, T


def bound(liq, kind, nonce, **kw):
    r = {"schema": 2, "kind": kind, "attempt": liq.get("attempt"), "cid": liq.get("client_order_id"),
         "escalation_id": (liq.get("escalated") or {}).get("id"), "nonce": nonce,
         "evidence": "broker order history export ref X", "by": "David"}
    r.update(kw)
    return r


def put(ns, res):
    liq = dict(pending(ns))
    liq["operator_resolution"] = res
    ns["db"].set_liquidation_state(SYM, **liq)


def sells_at_broker(b):
    return [o for o in b.orders.values() if o.kind == "market"]


@unittest.skipUnless(ON, "draft")
class D_EndToEndCounterexample(unittest.TestCase):
    def test_found_pointing_at_unrelated_terminal_order_neither_resells_nor_completes(self):
        ns, T = escalated_hidden()
        b = ns["_broker"]
        b.add_order("u1", "market", 0.4, status="filled", filled=0.4, avg=100.0, client_order_id="unrelated-cid",
                    symbol="BTC/USD")
        put(ns, bound(pending(ns), "found", "n-1", order_id="u1"))
        for t in (1000.0, 1100.0, 1200.0):
            T[0] = t
            H.cycle(ns)
        self.assertEqual(len(market_submissions(b)), 1, "a second sell was submitted")
        self.assertTrue(pending(ns), "liquidation falsely completed")
        self.assertEqual(ns["_marks"], [])
        self.assertAlmostEqual(b.qty, 0.5)


@unittest.skipUnless(ON, "draft")
class C_BindingAndVerification(unittest.TestCase):
    def test_unbound_resolution_is_rejected(self):
        for missing in ("attempt", "cid", "escalation_id", "nonce", "evidence", "by", "schema"):
            with self.subTest(missing=missing):
                ns, T = escalated_never_sent()
                r = bound(pending(ns), "not_placed", "n-1")
                r.pop(missing)
                put(ns, r)
                T[0] = 1000.0
                H.cycle(ns)
                self.assertEqual(sells_at_broker(ns["_broker"]), [])
                self.assertTrue(pending(ns).get("escalated"))

    def test_boolean_or_mismatched_attempt_is_rejected(self):
        for att in (True, 2, "1"):
            with self.subTest(attempt=att):
                ns, T = escalated_never_sent()
                put(ns, bound(pending(ns), "not_placed", "n-1", attempt=att))
                T[0] = 1000.0
                H.cycle(ns)
                self.assertEqual(sells_at_broker(ns["_broker"]), [])

    def test_found_requires_matching_client_id_symbol_and_side(self):
        for label, extra in (("wrong symbol", {"symbol": "ETH/USD"}), ("buy side", {"side": "buy"})):
            with self.subTest(label):
                ns, T = escalated_hidden()
                b = ns["_broker"]
                o = next(o for o in b.orders.values() if o.kind == "market")
                b.add_order("dup", "market", 0.5, status="filled", filled=0.5, avg=100.0,
                            client_order_id=o.client_order_id, symbol="BTC/USD")
                for k, v in extra.items():
                    setattr(b.orders["dup"], k, v)
                put(ns, bound(pending(ns), "found", "n-1", order_id="dup"))
                T[0] = 1000.0
                H.cycle(ns)
                self.assertEqual(len(market_submissions(b)), 1)
                self.assertTrue(pending(ns))

    def test_valid_found_reconciles_the_real_order(self):
        ns, T = escalated_hidden()
        b = ns["_broker"]
        o = next(o for o in b.orders.values() if o.kind == "market")
        o.symbol = "BTC/USD"
        b.reveal(fill=True)
        put(ns, bound(pending(ns), "found", "n-1", order_id=o.id))
        T[0] = 1000.0
        H.cycle(ns)
        self.assertFalse(pending(ns))
        self.assertEqual(len(market_submissions(b)), 1)

    def test_not_placed_is_rejected_if_the_broker_now_shows_the_order(self):
        ns, T = escalated_hidden()
        b = ns["_broker"]
        b.reveal(fill=False)                                   # visible, still working: it WAS placed
        o = next(o for o in b.orders.values() if o.kind == "market")
        o.status = NS(value="accepted")
        put(ns, bound(pending(ns), "not_placed", "n-1"))
        T[0] = 1000.0
        H.cycle(ns)
        self.assertEqual(len(market_submissions(b)), 1)

    def test_not_placed_is_rejected_if_the_position_decreased(self):
        ns, T = escalated_never_sent()
        ns["_broker"].qty = 0.3                                 # something sold 0.2 since the attempt
        put(ns, bound(pending(ns), "not_placed", "n-1"))
        T[0] = 1000.0
        H.cycle(ns)
        self.assertEqual(sells_at_broker(ns["_broker"]), [])
        self.assertTrue(pending(ns).get("escalated"))


@unittest.skipUnless(ON, "draft")
class E_ReplayCrashRejection(unittest.TestCase):
    def authorize_once(self):
        ns, T = escalated_never_sent()
        put(ns, bound(pending(ns), "not_placed", "n-1"))
        return ns, T

    def test_authorization_is_exactly_once_and_cannot_be_reused(self):
        ns, T = self.authorize_once()
        b = ns["_broker"]
        b.drop_order_kinds = {"market"}                         # attempt 2 also never arrives
        T[0] = 1000.0
        H.cycle(ns)
        b.drop_order_kinds = set()
        first = pending(ns)
        for t in (1010.0, 1020.0, 2000.0):
            T[0] = t
            H.cycle(ns)
        self.assertTrue(pending(ns).get("escalated"), "attempt 2 should escalate")
        self.assertEqual(sells_at_broker(b), [])
        # replay: identical old record (stale binding) and a rebound record reusing the consumed nonce
        put(ns, bound(first, "not_placed", "n-1"))
        T[0] = 3000.0
        H.cycle(ns)
        put(ns, bound(pending(ns), "not_placed", "n-1"))
        T[0] = 3100.0
        H.cycle(ns)
        self.assertEqual(sells_at_broker(b), [], "a consumed/stale authorization was reused")
        hist = pending(ns)["resolution_history"]
        self.assertEqual([h["outcome"] for h in hist][:1], ["applied"])
        self.assertEqual(sum(1 for h in hist if h["outcome"].startswith("rejected")), 2)

    def test_crash_after_applying_resolution_yields_exactly_one_new_attempt(self):
        ns, T = self.authorize_once()
        db = ns["db"]
        orig = db.set_liquidation_state

        def crash_after_apply(s, **kw):
            orig(s, **kw)
            if kw.get("authorized_attempt") is not None and not getattr(db, "_crashed", False):
                db._crashed = True
                raise Crash()
        db.set_liquidation_state = crash_after_apply
        T[0] = 1000.0
        try:
            H.cycle(ns)
        except Crash:
            pass
        db.set_liquidation_state = orig
        n2 = restart(ns)
        n2["_r1_now"] = lambda: 1100.0
        H.cycle(n2)
        H.cycle(n2)
        self.assertEqual(len(sells_at_broker(n2["_broker"])), 1)
        self.assertAlmostEqual(n2["_broker"].qty, 0.0)
        self.assertEqual(len(n2["_marks"]), 1)

    def test_rejected_resolution_stays_rejected_and_history_is_preserved(self):
        ns, T = escalated_never_sent()
        put(ns, bound(pending(ns), "not_placed", "n-bad", evidence=""))
        for t in (1000.0, 1100.0, 1200.0):
            T[0] = t
            H.cycle(ns)
        liq = pending(ns)
        self.assertEqual(sells_at_broker(ns["_broker"]), [])
        self.assertNotIn("operator_resolution", liq, "rejected record left in the active slot")
        self.assertEqual(len(liq["resolution_history"]), 1, "rejected record re-evaluated or duplicated")
        put(ns, bound(liq, "not_placed", "n-good"))
        T[0] = 1300.0
        H.cycle(ns)
        self.assertEqual(len(sells_at_broker(ns["_broker"])), 1)
        final = pending(ns) or {}
        self.assertEqual(len(ns["_marks"]), 1)
        self.assertEqual(final, {}, "completed liquidation record should be cleared (history is in escalation/"
                                    "resolution logs of the last persisted state)")


if __name__ == "__main__":
    unittest.main()
