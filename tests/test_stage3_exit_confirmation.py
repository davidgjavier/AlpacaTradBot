"""Stage 3 regression tests: sell-exit confirmation and the circuit breaker must not treat an
UNREADABLE position as flat.

Run (integrated code):  /usr/bin/python3 -m unittest discover -s tests -p 'test_stage3_*.py' -v
Run against baseline:   BOT_SOURCE=/path/to/baseline/crypto_trading_bot.py (same command)
Uses the existing P0-1 harness unchanged; the only local patch is a fake-broker behavior in which a
market sell is ACCEPTED but not yet filled, so a failed confirmation read cannot be mistaken for success.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p0_1_harness as H  # noqa: E402


def TIMEOUT():
    return TimeoutError("fake broker: position read timed out")


def fail_from(n):
    return lambda k: TIMEOUT() if k >= n else None


def market_sells_accept_without_filling(broker):
    orig = broker._accept

    def accept(req):
        if getattr(req, "_kind", None) == "market" and req.side == "sell":
            broker._n += 1
            oid = f"o{broker._n}"
            broker.orders[oid] = NS(id=oid, kind="market", qty=float(req.qty), side="sell",
                                    status=NS(value="accepted"), filled_qty="0", filled_avg_price=None,
                                    client_order_id=getattr(req, "client_order_id", None))
            return NS(id=oid, legs=[])
        return orig(req)
    broker._accept = accept


def hung_stop_position(ns, strategy="TREND"):
    b = ns["_broker"]
    b.add_order("s1", "stop_limit", 0.5, stop_price=99.0, limit_price=98.9)
    ns["db"].states["BTC/USD"] = dict(entry_price=100.0, stop_order_id="s1", stop_price=99.0,
                                      entry_time=datetime.now(timezone.utc).isoformat(), peak_price=100.0,
                                      entry_strategy=strategy)


def breaker_calls(ns):
    calls = []
    ns["db"].mark_breaker_tripped = lambda *a: calls.append(a)
    return calls


class EmergencyExit(unittest.TestCase):
    def test_unconfirmed_emergency_sell_logs_no_trade_and_keeps_state(self):
        ns = H.load_bot(qty=0.5, bid=90.0)               # bid far below the stop's limit -> hung stop
        hung_stop_position(ns)
        market_sells_accept_without_filling(ns["_broker"])
        ns["_broker"].position_fail = fail_from(2)       # cycle-start read OK; every confirmation read fails
        H.cycle(ns)
        self.assertEqual(ns["db"].trades, [], "trade logged for an exit whose fill was never confirmed")
        self.assertNotEqual(ns["db"].states.get("BTC/USD"), {}, "state cleared although the exit was unconfirmed")

    def test_confirmed_emergency_sell_logs_and_clears_control(self):
        ns = H.load_bot(qty=0.5, bid=90.0)
        hung_stop_position(ns)                           # fake broker fills the market sell; reads succeed
        H.cycle(ns)
        self.assertEqual(len(ns["db"].trades), 1)
        self.assertEqual(ns["db"].states.get("BTC/USD"), {})


class CircuitBreaker(unittest.TestCase):
    def breaker_ns(self):
        ns = H.load_bot(qty=0.5, bid=100.0)
        ns["db"].states["BTC/USD"] = dict(entry_price=100.0, stop_order_id=None, stop_price=None,
                                          entry_time=datetime.now(timezone.utc).isoformat(), peak_price=100.0,
                                          entry_strategy="TREND")
        ns["get_today_pl"] = lambda baseline: -1e9       # loss limit breached
        return ns

    def test_unconfirmed_flatten_does_not_mark_breaker_done_or_clear_state(self):
        ns = self.breaker_ns()
        calls = breaker_calls(ns)
        market_sells_accept_without_filling(ns["_broker"])
        ns["_broker"].position_fail = fail_from(2)
        H.cycle(ns)
        self.assertEqual(calls, [], "breaker marked as flattened although the flatten was unconfirmed")
        self.assertNotEqual(ns["db"].states.get("BTC/USD"), {}, "state cleared although the flatten was unconfirmed")

    def test_confirmed_flatten_marks_breaker_control(self):
        ns = self.breaker_ns()
        calls = breaker_calls(ns)
        H.cycle(ns)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ns["db"].states.get("BTC/USD"), {})


if __name__ == "__main__":
    unittest.main()
