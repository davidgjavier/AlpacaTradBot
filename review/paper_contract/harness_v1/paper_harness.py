"""OFFLINE paper-contract harness (v1). NOT connected to any broker. There is deliberately NO default transport:
a caller must inject one. This module imports no bot code, reads no credentials and no environment.

Invariant (same as the bot's R1 Option-1 candidate): while ANY tracked order has an unknown outcome, a cancellation
is unconfirmed, or the position is unreadable, the harness submits NOTHING, including cleanup. It stops UNRESOLVED
and reports residual exposure and unresolved client ids for human reconciliation. It never resubmits blindly.

Budget: every submission ATTEMPT counts (accepted, rejected, duplicate, lost response). Cleanup capacity is reserved
inside the total order cap, so test steps cannot consume it. Sells are capped by confirmed owned AND unreserved
inventory, never by intent. Nothing here manufactures exposure to force a behavior (P6 returns INCONCLUSIVE).

Termination: a graceful KeyboardInterrupt stops test steps and runs cleanup under the same invariant. A second
interrupt during cleanup is recorded as FORCED; tracked orders may remain; the journal (written and fsynced before
every submission) lets a restarted harness rebuild state and refuse new orders until reconciled. A SIGKILL cannot be
handled in-process; the journal is the only recovery path.
"""
import json
import math
import os
import time

OPEN = {"new", "accepted", "pending_new", "partially_filled", "held", "pending_cancel", "pending_replace",
        "accepted_for_bidding", "calculated"}
TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}
DUST = 1e-9


class Rejected(Exception):
    """Transport: the broker positively rejected the submission (nothing placed)."""


class LocalRefusal(Exception):
    """The harness refused BEFORE any transport call (limits/invariant)."""


class StopCondition(Exception):
    pass


class Harness:
    def __init__(self, transport, journal_path, run_id, symbol="BTC/USD", max_orders=20, cleanup_reserve=4,
                 max_order_notional=25.0, max_total_buy_notional=100.0, max_wall_s=1800.0, clock=time.monotonic):
        if transport is None:
            raise ValueError("a transport must be injected; there is no default broker adapter")
        if cleanup_reserve < 1 or cleanup_reserve >= max_orders:
            raise ValueError("cleanup_reserve must be >= 1 and < max_orders")
        self.t, self.path, self.run_id, self.symbol = transport, journal_path, run_id, symbol
        self.max_orders, self.reserve = max_orders, cleanup_reserve
        self.max_order_notional, self.max_buy_total = max_order_notional, max_total_buy_notional
        self.max_wall_s, self.clock, self.t0 = max_wall_s, clock, clock()
        self.orders, self.attempts, self.buy_notional, self.position = {}, 0, 0.0, None
        self.anomalies, self.status, self.forced = [], "RUNNING", False
        self.local_refusals = []
        if os.path.exists(journal_path):
            self._replay()

    # ------------------------------------------------------------------ journal
    def _j(self, kind, **body):
        rec = dict(body, kind=kind, t=round(self.clock() - self.t0, 6))
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _replay(self):
        with open(self.path) as f:
            for line in f:
                r = json.loads(line)
                k = r["kind"]
                if k == "attempt":
                    self.attempts += 1
                    self.orders[r["cid"]] = {"cid": r["cid"], "side": r["side"], "qty": r["qty"], "type": r["type"],
                                             "limit": r.get("limit"), "state": "UNKNOWN", "filled": 0.0,
                                             "order_id": None, "purpose": r["purpose"]}
                    if r["side"] == "buy":
                        self.buy_notional += r["notional"]
                elif k == "order_state" and r["cid"] in self.orders:
                    self.orders[r["cid"]].update(state=r["state"], filled=r["filled"], order_id=r.get("order_id"))
                elif k == "anomaly":
                    self.anomalies.append(r["what"])
        self._j("restart", unresolved=self.unresolved())

    # ------------------------------------------------------------------ state
    def unresolved(self):
        return sorted(c for c, o in self.orders.items() if o["state"] in ("UNKNOWN", "CANCEL_UNKNOWN"))

    def open_orders(self):
        return [o for o in self.orders.values() if o["state"] == "OPEN"]

    def reserved(self):
        return sum(max(0.0, o["qty"] - o["filled"]) for o in self.open_orders() if o["side"] == "sell")

    def available(self):
        """Confirmed owned minus reserved by our open sells; None if anything is unknown."""
        if self.position is None or self.unresolved():
            return None
        return max(0.0, self.position - self.reserved())

    def blocked(self):
        why = []
        if self.unresolved():
            why.append(f"unresolved orders {self.unresolved()}")
        if self.anomalies:
            why.append(f"anomalies {self.anomalies}")
        return why

    # ------------------------------------------------------------------ broker interactions
    def read_position(self):
        try:
            q = self.t.get_position(self.symbol)
            q = float(q)
            if not math.isfinite(q) or q < 0:
                raise ValueError(f"invalid position {q!r}")
            self.position = q
        except Exception as e:
            self.position = None
            self._j("position_unknown", error=f"{type(e).__name__}: {e}")
        else:
            self._j("position", qty=self.position)
        return self.position

    def _apply(self, cid, o):
        """Idempotent: cumulative filled_qty; duplicate callbacks are harmless. Growth after terminal = anomaly."""
        rec = self.orders[cid]
        status, filled = str(o.get("status")), float(o.get("filled_qty") or 0.0)
        was_terminal = rec["state"] in ("FILLED", "TERMINAL")
        if was_terminal and filled > rec["filled"] + DUST:
            self.anomalies.append(f"late fill on {cid} after terminal ({rec['filled']} -> {filled})")
            self._j("anomaly", what=self.anomalies[-1])
        if filled + DUST < rec["filled"]:
            self.anomalies.append(f"filled_qty decreased on {cid}")
            self._j("anomaly", what=self.anomalies[-1])
            return
        state = "FILLED" if status == "filled" else "TERMINAL" if status in TERMINAL else "OPEN" if status in OPEN \
            else rec["state"]
        if status not in OPEN | TERMINAL:
            self.anomalies.append(f"unrecognized status {status!r} on {cid}")
            self._j("anomaly", what=self.anomalies[-1])
        if rec["state"] == "CANCEL_UNKNOWN" and state == "OPEN":
            state = "CANCEL_UNKNOWN"          # a still-open order after an unconfirmed cancel stays unresolved
        rec.update(state=state, filled=max(rec["filled"], filled), order_id=o.get("id") or rec["order_id"])
        self._j("order_state", cid=cid, state=rec["state"], filled=rec["filled"], order_id=rec["order_id"])

    def reconcile(self):
        for cid, rec in self.orders.items():
            if rec["state"] in ("FILLED", "TERMINAL") and not rec.get("recheck"):
                continue
            try:
                o = self.t.get_order_by_client_id(cid)
            except Exception as e:                       # includes 404: never proof of non-placement
                self._j("lookup_failed", cid=cid, error=f"{type(e).__name__}: {e}")
                continue
            self._apply(cid, o)

    def _submit(self, side, qty, otype, price_ref, purpose, limit=None):
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or not math.isfinite(qty) or qty <= 0:
            self._refuse(f"invalid quantity {qty!r}")
        notional = float(qty) * float(limit if limit else price_ref)
        if notional > self.max_order_notional + 1e-9:
            self._refuse(f"order notional {notional:.2f} > {self.max_order_notional}")
        if side == "buy" and self.buy_notional + notional > self.max_buy_total + 1e-9:
            self._refuse(f"aggregate buy notional would exceed {self.max_buy_total}")
        if self.blocked():
            self._refuse("blocked: " + "; ".join(self.blocked()))
        if side == "sell":
            avail = self.available()
            if avail is None:
                self._refuse("sell refused: position or order state unknown")
            if qty > avail + DUST:
                self._refuse(f"sell {qty} exceeds confirmed unreserved inventory {avail}")
        cap = self.max_orders - (0 if purpose == "cleanup" else self.reserve)
        if self.attempts >= cap:
            self._refuse(f"{purpose} order budget exhausted ({self.attempts}/{cap})")
        cid = f"pt-{self.run_id}-{self.attempts + 1}"
        self.orders[cid] = {"cid": cid, "side": side, "qty": float(qty), "type": otype, "limit": limit,
                            "state": "UNKNOWN", "filled": 0.0, "order_id": None, "purpose": purpose}
        self.attempts += 1
        if side == "buy":
            self.buy_notional += notional
        self._j("attempt", cid=cid, side=side, qty=float(qty), type=otype, limit=limit, notional=notional,
                purpose=purpose)                          # durable BEFORE the transport call
        try:
            o = self.t.submit_order({"symbol": self.symbol, "side": side, "qty": float(qty), "type": otype,
                                     "limit_price": limit, "client_order_id": cid,
                                     "time_in_force": "ioc" if otype == "market" else "gtc"})
        except Rejected as e:
            self.orders[cid]["state"] = "TERMINAL"
            self._j("order_state", cid=cid, state="TERMINAL", filled=0.0, order_id=None, rejected=str(e))
            return cid
        except Exception as e:                            # lost response / timeout / unknown: outcome UNKNOWN
            self._j("submit_unknown", cid=cid, error=f"{type(e).__name__}: {e}")
            return cid
        self._apply(cid, o)
        return cid

    def _refuse(self, why):
        self.local_refusals.append(why)
        self._j("local_refusal", why=why)
        raise LocalRefusal(why)

    def cancel(self, cid):
        rec = self.orders[cid]
        try:
            self.t.cancel_order(rec["order_id"] or cid)
        except Exception as e:
            rec["state"] = "CANCEL_UNKNOWN"
            self._j("order_state", cid=cid, state="CANCEL_UNKNOWN", filled=rec["filled"], order_id=rec["order_id"],
                    error=str(e))
            return
        try:
            self._apply(cid, self.t.get_order_by_client_id(cid))
        except Exception as e:
            rec["state"] = "CANCEL_UNKNOWN"
            self._j("order_state", cid=cid, state="CANCEL_UNKNOWN", filled=rec["filled"], order_id=rec["order_id"],
                    error=f"confirm failed: {e}")
            return
        if rec["state"] == "OPEN":
            rec["state"] = "CANCEL_UNKNOWN"
            self._j("order_state", cid=cid, state="CANCEL_UNKNOWN", filled=rec["filled"], order_id=rec["order_id"],
                    error="still open after cancel")

    # ------------------------------------------------------------------ public test API
    def buy(self, qty, price_ref, otype="limit", limit=None):
        return self._submit("buy", qty, otype, price_ref, "test", limit=limit)

    def sell(self, qty, price_ref, otype="limit", limit=None):
        return self._submit("sell", qty, otype, price_ref, "test", limit=limit)

    def p6_partial_probe_qty(self, requested_qty, price_ref, min_qty):
        """P6 sizing: min(requested, confirmed unreserved inventory, notional cap). Never buys to create size."""
        self.reconcile()
        self.read_position()
        avail = self.available()
        if avail is None:
            return None, "INCONCLUSIVE: inventory unknown"
        q = min(float(requested_qty), avail, self.max_order_notional / float(price_ref))
        if q < min_qty:
            return None, f"INCONCLUSIVE: capped size {q} below minimum {min_qty} within limits"
        return q, "OK"

    def cleanup(self, price_ref):
        """Cancel our open orders (confirmed), reconcile, and sell remaining CONFIRMED inventory ONLY if nothing is
        unknown. At most ONE cleanup market sell per invocation; never a second while the first is unresolved."""
        self._j("cleanup_start")
        for o in list(self.open_orders()):
            self.cancel(o["cid"])
        self.reconcile()
        self.read_position()
        if self.blocked() or self.position is None:
            return self._finish("UNRESOLVED")
        avail = self.available()
        if avail and avail > DUST:
            try:
                cid = self._submit("sell", avail, "market", price_ref, "cleanup")
            except LocalRefusal:
                return self._finish("UNRESOLVED")
            self.reconcile()
            self.read_position()
            if self.orders[cid]["state"] not in ("FILLED", "TERMINAL") or self.position is None or self.blocked():
                return self._finish("UNRESOLVED")
            if self.position > DUST:
                return self._finish("UNRESOLVED")
        if self.open_orders():
            return self._finish("UNRESOLVED")
        return self._finish("CLEAN")

    def _finish(self, status):
        self.status = status
        rep = self.report()
        self._j("finish", **rep)
        return rep

    def report(self):
        return {"status": self.status, "forced": self.forced, "attempts": self.attempts, "max_orders": self.max_orders,
                "position": self.position, "residual_exposure_known": self.position,
                "unresolved_orders": self.unresolved(), "open_orders": [o["cid"] for o in self.open_orders()],
                "anomalies": list(self.anomalies), "local_refusals": list(self.local_refusals),
                "human_reconciliation_required": self.status != "CLEAN"}

    def run(self, steps, price_ref):
        """steps: callables(harness). Stops on StopCondition, anomalies, wall clock, or KeyboardInterrupt."""
        try:
            for step in steps:
                if self.clock() - self.t0 > self.max_wall_s:
                    raise StopCondition("wall clock limit")
                if self.blocked():
                    raise StopCondition("blocked: " + "; ".join(self.blocked()))
                step(self)
        except KeyboardInterrupt:
            self._j("interrupted", mode="graceful")
        except (StopCondition, LocalRefusal) as e:
            self._j("stopped", why=str(e))
        try:
            return self.cleanup(price_ref)
        except KeyboardInterrupt:
            self.forced = True
            self._j("interrupted", mode="forced_during_cleanup")
            return self._finish("UNRESOLVED")
