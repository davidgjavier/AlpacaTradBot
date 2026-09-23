"""Stage 5c regressions:
 L. Same-day breaker latch: once today's breaker is marked, recovering P/L must not reopen entries that UTC day.
    Thresholds unchanged. A new UTC day unlatches (guard). A pending liquidation still runs on a latched day.
 C. Crash windows: a crash between the completion writes must not lose the latch or the pending liquidation;
    an intent persisted but never sent (client id not found) is resubmitted exactly once.
Run: /usr/bin/python3 -m unittest discover -s tests -t tests -p 'test_stage5c_*.py' -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H                                          # noqa: E402
from test_stage5_liquidation import breaker_ns, market_sells, partial_market_sells, pending, restart, SYM  # noqa: E402


def buys(b):
    return [r for r in b.submitted if getattr(r, "side", None) == "buy"]


def recovered_latched_day(qty=0.0):
    ns = breaker_ns(qty)
    ns["db"].states[SYM] = {} if qty == 0 else ns["db"].states[SYM]
    ns["db"].baseline["breaker_tripped_stamp"] = ns["db"].baseline["day_stamp"]   # tripped earlier today
    ns["get_today_pl"] = lambda baseline: 0.0                                        # P/L recovered
    ns["strategies"].get_signal = lambda *a, **k: "buy"
    entries_possible(ns)
    return ns


def entries_possible(ns):
    """Test-local: the harness's flat synthetic volumes never pass RVOL confirmation, which would make every
    'no entry' assertion vacuous. Only this gate is opened; the new-day guard proves an entry CAN happen."""
    ns["strategies"].is_volume_confirmed = lambda *a, **k: True


class Crash(BaseException):
    pass


class L_SameDayLatch(unittest.TestCase):
    def test_recovered_pl_does_not_reopen_entries_same_day(self):
        ns = recovered_latched_day()
        H.cycle(ns)
        H.cycle(ns)
        self.assertEqual(buys(ns["_broker"]), [], "entry placed after the breaker tripped today")

    def test_manually_installed_new_day_state_allows_entry(self):
        """LABEL (accurate): installs the state reset_day_if_needed() WOULD return on a new UTC day (new
        day_stamp, breaker_tripped_stamp None) by hand. It does NOT exercise a real clock/date transition or
        reset_day_if_needed() itself (the harness stubs it). It proves the latch keys on the stamp and that an
        entry is possible in this harness (so the 'no entry' assertions are not vacuous)."""
        ns = recovered_latched_day()
        ns["db"].baseline.update(day_stamp="2026-09-24", breaker_tripped_stamp=None)   # reset_day_if_needed result
        H.cycle(ns)
        self.assertTrue(buys(ns["_broker"]), "harness guard: an entry should be possible on a new day")

    def test_threshold_unchanged(self):
        ns = H.load_bot()
        self.assertEqual(ns["DAILY_LOSS_LIMIT_USD"], 150)

    def test_pending_liquidation_still_runs_on_latched_day(self):
        ns = breaker_ns(0.5)
        partial_market_sells(ns["_broker"], [0.2, 0.3])
        H.cycle(ns)
        self.assertTrue(pending(ns))
        ns["get_today_pl"] = lambda baseline: 0.0
        ns["strategies"].get_signal = lambda *a, **k: "buy"
        entries_possible(ns)
        H.cycle(ns)
        self.assertAlmostEqual(ns["_broker"].qty, 0.0)
        self.assertEqual(buys(ns["_broker"]), [])


class C_CrashWindows(unittest.TestCase):
    def crash_after_write(self, k):
        """Liquidation completes in cycle 2; the process dies right after the k-th completion write."""
        ns = breaker_ns(0.5)
        partial_market_sells(ns["_broker"], [0.2, 0.3])
        H.cycle(ns)                                   # pending (0.3 left)
        db, n = ns["db"], {"w": 0}
        orig = {m: getattr(db, m) for m in ("clear_position_state", "clear_liquidation_state", "mark_breaker_tripped")}

        def wrap(m):
            def f(*a, **kw):
                orig[m](*a, **kw)
                n["w"] += 1
                if n["w"] == k:
                    raise Crash()
            return f
        for m in orig:
            setattr(db, m, wrap(m))
        try:
            H.cycle(ns)
        except Crash:
            pass
        for m in orig:
            setattr(db, m, orig[m])
        n2 = restart(ns)
        n2["get_today_pl"] = lambda baseline: 0.0     # P/L recovered after the crash
        n2["strategies"].get_signal = lambda *a, **k: "buy"
        entries_possible(n2)
        H.cycle(n2)
        H.cycle(n2)
        return n2

    def test_crash_between_completion_writes_keeps_latch_and_converges(self):
        for k in (1, 2):
            with self.subTest(crash_after_write=k):
                n2 = self.crash_after_write(k)
                self.assertEqual(buys(n2["_broker"]), [], f"entry reopened after a crash at completion write {k}")
                self.assertEqual(n2["db"].baseline["breaker_tripped_stamp"], n2["db"].baseline["day_stamp"])
                self.assertFalse(pending(n2))
                self.assertEqual(n2["db"].states.get(SYM), {})

    def test_intent_persisted_but_never_sent_is_resubmitted_once(self):
        ns = breaker_ns(0.5)
        b = ns["_broker"]
        b.drop_order_kinds = {"market"}               # request never reaches the broker; submit raises
        H.cycle(ns)
        liq = pending(ns)
        self.assertTrue(liq and liq.get("client_order_id") and not liq.get("order_id"))
        b.drop_order_kinds = set()
        H.cycle(ns)
        self.assertAlmostEqual(b.qty, 0.0)
        accepted = [o for o in b.orders.values() if o.kind == "market"]
        self.assertEqual(len(accepted), 1, "more than one liquidation sell reached the broker")
        self.assertEqual(len(ns["_marks"]), 1)


if __name__ == "__main__":
    unittest.main()
