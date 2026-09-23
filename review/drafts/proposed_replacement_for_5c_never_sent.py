"""PROPOSED replacement (NOT active) for tests/test_stage5c_latch_and_crash.py::
C_CrashWindows::test_intent_persisted_but_never_sent_is_resubmitted_once — only if R1 v2 is adopted.
The existing test stays unchanged until then. Run:
  DRAFT_R1V2=1 BOT_SOURCE=review/drafts/crypto_trading_bot_R1v2.py python3 review/drafts/proposed_replacement_for_5c_never_sent.py
"""
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2] / "tests"
sys.path.insert(0, str(TESTS))
import p0_1_harness as H                                    # noqa: E402
from test_stage5_liquidation import breaker_ns, pending, SYM  # noqa: E402


class Proposed(unittest.TestCase):
    def test_never_sent_intent_escalates_and_is_not_resubmitted_automatically(self):
        ns = breaker_ns(0.5)
        T = [0.0]
        ns["_r1_now"] = lambda: T[0]
        b = ns["_broker"]
        b.drop_order_kinds = {"market"}
        H.cycle(ns)
        b.drop_order_kinds = set()
        for t in (10.0, 20.0, 900.0, 5000.0):
            T[0] = t
            H.cycle(ns)
        self.assertEqual([o for o in b.orders.values() if o.kind == "market"], [], "automatic resubmit")
        self.assertTrue(pending(ns).get("escalated"))
        liq = dict(pending(ns))
        liq["operator_resolution"] = {"schema": 2, "kind": "not_placed", "attempt": liq["attempt"],
                                      "cid": liq["client_order_id"], "escalation_id": liq["escalated"]["id"],
                                      "nonce": "repl-1", "evidence": "broker history export", "by": "David"}
        ns["db"].liquidations[SYM] = liq
        H.cycle(ns)
        self.assertEqual(len([o for o in b.orders.values() if o.kind == "market"]), 1)
        self.assertAlmostEqual(b.qty, 0.0)


if __name__ == "__main__":
    unittest.main()
