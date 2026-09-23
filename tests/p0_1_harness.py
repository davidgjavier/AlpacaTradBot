"""Offline harness for P0-1 regression tests (no network, no credentials, no real DB).

Loads the ORIGINAL function bodies of a crypto_trading_bot.py (path from env
BOT_SOURCE, default: this worktree's copy) via its syntax tree, exactly like the
audit harness, but with a stricter fake broker that models what caused the
incident:

  * order lifecycle: status + CUMULATIVE filled_qty + filled_avg_price
  * BTC balance RESERVATION: a sell is rejected ("insufficient balance ...
    available: X") if its qty exceeds position minus qty still reserved by open
    sell orders. This is the behaviour seen in cryptobot.log:934.
  * cancel that can be made to fail (order stays open)

It never talks to Alpaca and never imports the bot module (so no dotenv/keys).
"""
import ast
import copy
import math
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
BOT_SOURCE = Path(os.environ.get("BOT_SOURCE", HERE.parent / "crypto_trading_bot.py"))
STRATEGIES_SOURCE = Path(os.environ.get("STRATEGIES_SOURCE", HERE.parent / "strategies.py"))


def _blocked(*a, **k):
    raise RuntimeError("network forbidden in P0-1 harness")


socket.socket.connect = _blocked  # type: ignore[assignment]
socket.create_connection = _blocked  # type: ignore[assignment]

OPEN = {"new", "accepted", "held", "pending_new", "partially_filled", "pending_cancel", "pending_replace"}


class EndCycle(BaseException):
    pass


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def time(self):
        self.t += 0.1
        return self.t

    def sleep(self, s):
        if s >= 180:            # main-loop cycle sleep ends the test cycle
            raise EndCycle()
        self.t += s


class MemoryDB:
    def __init__(self):
        self.states, self.trades, self.events = {}, [], []
        self.baseline = {"day_stamp": "2026-09-23", "breaker_tripped_stamp": None, "eod_flattened_stamp": None}

    def get_position_state(self, s): return copy.deepcopy(self.states.get(s, {}))
    def set_position_state(self, s, **kw): self.states[s] = kw
    def clear_position_state(self, s): self.states[s] = {}
    def log_activity(self, *a, **k): self.events.append(a)
    def get_strategy_params(self): return {}
    def get_strategy_params_for_symbol(self, s): return {}
    def get_regime_state(self): return {}
    def set_regime_state(self, **k): pass
    def get_active_strategy(self): return "trend"
    def is_paused(self, s): return False
    def mark_breaker_tripped(self, *a): pass

    def log_trade(self, *a, **kw):
        self.trades.append(kw)
        return dict(gross_pnl=0.0, fees_paid=0.0, net_pnl=0.0, slippage=kw.get("slippage") or 0.0)


class Broker:
    """Fake Alpaca with reservation + cumulative fills.

    next_tp: how the next plain LIMIT SELL behaves:
        ("reject",)                  -> raise insufficient/other rejection
        ("filled", qty)              -> status filled, filled_qty=qty
        ("partial_then_cancel", q)   -> status canceled, filled_qty=q   (IOC remainder cancelled)
        ("partial_open", q)          -> status partially_filled, filled_qty=q (still working)
        ("open",)                    -> status new, filled 0
        ("unknown",)                 -> accepted, but get_order_by_id raises afterwards
    """

    def __init__(self, qty=0.0, bid=100.0):
        self.qty, self.bid = qty, bid
        self.orders = {}
        self.rejections = []            # every "insufficient balance" rejection = oversubscription attempt
        self.submitted = []
        self.cancel_fails = False
        self.unknown_ids = set()
        self.next_tp = ("open",)
        self._n = 0

    # --- helpers for tests
    def add_order(self, oid, kind, qty, status="new", filled=0.0, avg=None, **extra):
        self.orders[oid] = NS(id=oid, kind=kind, qty=qty, side="sell", status=NS(value=status),
                              filled_qty=str(filled), filled_avg_price=None if avg is None else str(avg), **extra)

    def open_sells(self):
        return [o for o in self.orders.values() if o.status.value in OPEN and o.side == "sell"]

    def reserved(self):
        return sum(float(o.qty) - float(o.filled_qty) for o in self.open_sells())

    def open_stops(self):
        return [o for o in self.open_sells() if o.kind == "stop_limit"]

    # --- Alpaca-like surface
    def get_open_position(self, *a):
        if self.qty <= 1e-12:
            raise Exception('{"code":40410000,"message":"position does not exist"}')
        return NS(qty=str(self.qty))

    def submit_order(self, req):
        self.submitted.append(req)
        self._n += 1
        oid = f"o{self._n}"
        if req.side == "sell":
            avail = self.qty - self.reserved()
            if float(req.qty) > avail + 1e-12:
                msg = (f'{{"code":40310000,"message":"insufficient balance for BTC '
                       f'(requested: {req.qty}, available: {max(avail, 0):g})"}}')
                self.rejections.append(msg)
                raise Exception(msg)
        if req._kind == "stop_limit":
            self.add_order(oid, "stop_limit", float(req.qty), stop_price=req.stop_price, limit_price=req.limit_price)
        elif req._kind == "limit" and req.side == "sell":
            beh = self.next_tp
            if beh[0] == "reject":
                raise Exception('{"code":42210000,"message":"order rejected by fake broker"}')
            status, filled = {"filled": ("filled", beh[1] if len(beh) > 1 else float(req.qty)),
                              "partial_then_cancel": ("canceled", beh[1] if len(beh) > 1 else 0.0),
                              "partial_open": ("partially_filled", beh[1] if len(beh) > 1 else 0.0),
                              "open": ("new", 0.0), "unknown": ("new", 0.0)}[beh[0]]
            self.add_order(oid, "limit", float(req.qty), status=status, filled=filled,
                           avg=req.limit_price if filled else None, limit_price=req.limit_price,
                           time_in_force=getattr(req, "time_in_force", None))
            self.qty -= filled
            if beh[0] == "unknown":
                self.unknown_ids.add(oid)
        elif req._kind == "market":
            fill = float(req.qty)
            self.add_order(oid, "market", fill, status="filled", filled=fill, avg=self.bid)
            self.qty -= fill
        return NS(id=oid, legs=[])

    def get_order_by_id(self, oid):
        if oid in self.unknown_ids or oid not in self.orders:
            raise TimeoutError("fake broker: order lookup failed")
        return self.orders[oid]

    def cancel_order_by_id(self, oid):
        o = self.orders.get(oid)
        if o is None or self.cancel_fails:
            if self.cancel_fails:
                raise Exception("fake broker: cancel failed")
            return
        if o.status.value in OPEN:
            o.status = NS(value="canceled")

    def get_orders(self, *a, **k):
        return self.open_sells()

    def get_account(self):
        return NS(equity="100000", last_equity="100000")


def _strategies():
    import importlib.util
    spec = importlib.util.spec_from_file_location("p01_strategies", STRATEGIES_SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.refresh_master_spec_snapshot = lambda *a, **k: None
    return mod


def load_bot(qty=0.0, bid=100.0):
    tree = ast.parse(BOT_SOURCE.read_text())
    clock = Clock()
    broker = Broker(qty, bid)
    ns = {"__name__": "p01_harness", "math": math, "datetime": datetime, "timedelta": timedelta,
          "timezone": timezone, "ZoneInfo": ZoneInfo, "time": clock, "strategies": _strategies(),
          "db": MemoryDB(), "trading_client": broker, "PAPER": True,
          "OrderSide": NS(BUY="buy", SELL="sell"),
          "TimeInForce": NS(DAY="day", GTC="gtc", IOC="ioc"),
          "QueryOrderStatus": NS(OPEN="open")}
    for node in tree.body:              # literal module constants only
        if isinstance(node, ast.Assign):
            try:
                val = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    ns[t.id] = val
    ns["StopLimitOrderRequest"] = lambda **kw: NS(_kind="stop_limit", **kw)
    ns["LimitOrderRequest"] = lambda **kw: NS(_kind="limit", **kw)
    ns["MarketOrderRequest"] = lambda **kw: NS(_kind="market", **kw)
    ns["GetOrdersRequest"] = lambda **kw: NS(**kw)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(BOT_SOURCE), "exec"), ns)
    ns["log"] = lambda *a: ns["db"].events.append(a)
    ns.setdefault("SYMBOL", "BTC/USD")
    ns["MAX_SPREAD_PCT"] = 0.003
    ns["CRYPTO_ESTIMATED_FEE_RATE"] = 0.0025
    ns["CRYPTO_EXIT_SLIPPAGE_BUFFER"] = 0.001
    # main-loop dependencies unrelated to P0-1
    ns["reset_day_if_needed"] = lambda: ns["db"].baseline.copy()
    ns["get_today_pl"] = lambda baseline: 0.0
    ns["get_bars"] = lambda *a, **k: ([100.2] * 50, [99.8] * 50, [100.0] * 50, [100.0] * 50)
    ns["get_live_quote"] = lambda: (broker.bid, broker.bid + 0.01)
    ns["print_cycle_telemetry"] = lambda *a, **k: None
    ns["is_macro_uptrend"] = lambda: True
    ns["strategies"].get_signal = lambda *a, **k: None
    ns["strategies"].detect_regime_stable = lambda closes, st: ("trend", {})
    ns["strategies"].STRATEGIES = {"trend": lambda c: None, "breakout": lambda c: None, "reversion": lambda c: None}
    ns["_broker"], ns["_clock"] = broker, clock
    return ns


def cycle(ns):
    try:
        ns["main"]()
    except EndCycle:
        pass


def text(ns):
    return "\n".join(" ".join(str(x) for x in e) for e in ns["db"].events)
