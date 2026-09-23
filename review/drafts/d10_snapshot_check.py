"""DRAFT v2.1 (not adopted) — D10 legacy-state check against a DAVID-SUPPLIED broker snapshot. Schema 2.

Pure and read-only: no broker calls, no DB writes. An offline check cannot establish current broker truth. It only
validates that a snapshot taken at time T is well-formed, complete by its own attestation, and consistent with the
DB. Any resolution must also pass live_gate() at apply time against the bot's own fresh strict reads.

CHRONOLOGY CONVENTION: taken_at is the single capture instant that every list reflects. Therefore fill coverage must
end exactly at taken_at (covers_from < covers_to == taken_at), and no fill may be later than taken_at. Freshness
rules (taken_at <= now, max_age_s) are unchanged.

REMAINING GAP (not closable here): live_gate() and the subsequent action are not atomic. State can change between
the gate's reads and the order that follows (manual trades, fills, other processes). The bot's action path still
reconciles-before-acting each cycle, but nothing in this module makes check-then-act atomic.

The completeness block is an ATTESTATION by the exporter (e.g. "has_more": false after paginating). This module
verifies it is present and consistent; it cannot verify that pagination really happened.

snapshot (schema 2):
  {"schema": 2, "taken_at": tz-aware ISO, "environment": "paper"|"live", "account_key": str, "symbol": str,
   "position_qty": finite float >= 0 (not bool),
   "open_orders": [order], "fills": [fill],
   "completeness": {"orders": {"has_more": false, "pages": int >= 1},
                    "fills":  {"has_more": false, "pages": int >= 1, "covers_from": ISO, "covers_to": ISO}}}
  order: {"id": nonempty str, "client_order_id": str|None, "symbol", "side": "buy"|"sell", "status": open status,
          "qty": finite > 0, "filled_qty": finite 0..qty, "type": nonempty str, [limit_price|stop_price: finite > 0]}
  fill:  {"order_id": nonempty str, "client_order_id": str|None, "symbol", "side", "qty": finite > 0,
          "price": finite > 0, "time": ISO within the coverage window}
live (apply time): {"environment", "account_key", "symbol", "read_at": ISO, "position_qty", "open_orders": [order],
                    "complete": true}
"""
import math
from datetime import datetime, timezone

OPEN_STATUSES = {"new", "accepted", "held", "pending_new", "partially_filled", "pending_cancel", "pending_replace",
                 "accepted_for_bidding", "calculated"}
SIDES = {"buy", "sell"}
ORDER_FIELDS = ("id", "client_order_id", "symbol", "side", "status", "qty", "filled_qty", "type", "limit_price",
                "stop_price")


def _ts(v):
    """Strict tz-aware timestamp or None."""
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def _num(v, minimum=None, strict_min=False):
    """Finite real number (bool rejected) satisfying the bound, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if minimum is not None and (v <= minimum if strict_min else v < minimum):
        return None
    return float(v)


def _order_problems(o, symbol, where):
    if not isinstance(o, dict):
        return [f"{where}: not an object"]
    p = []
    if not isinstance(o.get("id"), str) or not o.get("id"):
        p.append(f"{where}: id missing/empty")
    if o.get("client_order_id") is not None and not isinstance(o.get("client_order_id"), str):
        p.append(f"{where}: client_order_id not a string")
    if o.get("symbol") != symbol:
        p.append(f"{where}: symbol {o.get('symbol')!r} != {symbol!r}")
    if o.get("side") not in SIDES:
        p.append(f"{where}: side {o.get('side')!r} invalid")
    if o.get("status") not in OPEN_STATUSES:
        p.append(f"{where}: status {o.get('status')!r} is not an open status")
    q = _num(o.get("qty"), 0.0, strict_min=True)
    if q is None:
        p.append(f"{where}: qty invalid (finite > 0 required)")
    f = _num(o.get("filled_qty"), 0.0)
    if f is None:
        p.append(f"{where}: filled_qty invalid (finite >= 0 required)")
    elif q is not None and f > q + 1e-12:
        p.append(f"{where}: filled_qty > qty")
    if not isinstance(o.get("type"), str) or not o.get("type"):
        p.append(f"{where}: type missing")
    for k in ("limit_price", "stop_price"):
        if k in o and o[k] is not None and _num(o[k], 0.0, strict_min=True) is None:
            p.append(f"{where}: {k} invalid")
    return p


def _fill_problems(f, symbol, cov_from, cov_to, where):
    if not isinstance(f, dict):
        return [f"{where}: not an object"]
    p = []
    if not isinstance(f.get("order_id"), str) or not f.get("order_id"):
        p.append(f"{where}: order_id missing/empty")
    if f.get("symbol") != symbol:
        p.append(f"{where}: symbol mismatch")
    if f.get("side") not in SIDES:
        p.append(f"{where}: side invalid")
    if _num(f.get("qty"), 0.0, strict_min=True) is None:
        p.append(f"{where}: qty invalid")
    if _num(f.get("price"), 0.0, strict_min=True) is None:
        p.append(f"{where}: price invalid")
    t = _ts(f.get("time"))
    if t is None:
        p.append(f"{where}: time missing/naive/unparseable")
    elif cov_from and cov_to and not (cov_from <= t <= cov_to):
        p.append(f"{where}: time outside the attested coverage window")
    return p


def check(snapshot, db, expected):
    block, flags, facts = [], [], {}
    if not isinstance(snapshot, dict):
        return {"verdict": "BLOCK", "block": ["snapshot is not an object"], "flags": [], "facts": {}}
    if snapshot.get("schema") != 2:
        block.append("schema must be 2 (fills_since alone is not completeness evidence)")
    for k in ("environment", "account_key", "symbol"):
        if not isinstance(snapshot.get(k), str) or snapshot.get(k) != expected.get(k):
            block.append(f"{k} mismatch/invalid: snapshot={snapshot.get(k)!r} expected={expected.get(k)!r}")
    symbol = expected.get("symbol")
    taken, now = _ts(snapshot.get("taken_at")), _ts(expected.get("now"))
    if taken is None or now is None:
        block.append("taken_at/now missing, naive or unparseable")
    else:
        age = (now - taken).total_seconds()
        facts["age_s"] = age
        if age < 0:
            block.append("snapshot taken_at is in the future")
        elif age > expected.get("max_age_s", 600):
            block.append(f"snapshot stale: {age:.0f}s > {expected.get('max_age_s', 600)}s")
    stamp = db.get("breaker_tripped_stamp")
    day_start = _ts(stamp + "T00:00:00+00:00") if isinstance(stamp, str) else None
    if taken and day_start and taken < day_start:
        block.append("snapshot predates the breaker day")
    qty = _num(snapshot.get("position_qty"), 0.0)
    if qty is None:
        block.append("position_qty invalid (finite, >= 0, not boolean)")
    facts["position_qty"] = qty
    comp = snapshot.get("completeness")
    cov_from = cov_to = None
    if not isinstance(comp, dict) or not isinstance(comp.get("orders"), dict) or not isinstance(comp.get("fills"), dict):
        block.append("completeness evidence missing")
    else:
        for part in ("orders", "fills"):
            c = comp[part]
            if c.get("has_more") is not False:
                block.append(f"completeness.{part}.has_more must be false (pagination incomplete or unattested)")
            pages = c.get("pages")
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
                block.append(f"completeness.{part}.pages must be an integer >= 1")
        cov_from, cov_to = _ts(comp["fills"].get("covers_from")), _ts(comp["fills"].get("covers_to"))
        if cov_from is None or cov_to is None:
            block.append("completeness.fills coverage window missing/invalid")
        else:
            # Chronology convention (v2.1): taken_at is the single capture instant every list reflects.
            if cov_from >= cov_to:
                block.append("fill coverage window is inverted or empty (covers_from must be before covers_to)")
            if day_start and cov_from > day_start:
                block.append("fill coverage starts after the breaker day began")
            if taken and cov_to < taken:
                block.append("fill coverage ends before taken_at")
            if taken and cov_to > taken:
                block.append("fill coverage ends after taken_at (cannot cover time after the capture instant)")
    orders, fills = snapshot.get("open_orders"), snapshot.get("fills")
    if not isinstance(orders, list):
        block.append("open_orders missing or not a list")
        orders = []
    if not isinstance(fills, list):
        block.append("fills missing or not a list")
        fills = []
    for i, o in enumerate(orders):
        block.extend(_order_problems(o, symbol, f"open_orders[{i}]"))
    for i, f in enumerate(fills):
        block.extend(_fill_problems(f, symbol, cov_from, cov_to, f"fills[{i}]"))
        ft = _ts(f.get("time")) if isinstance(f, dict) else None
        if ft is not None and taken is not None and ft > taken:
            block.append(f"fills[{i}]: time is later than taken_at (impossible chronology)")
    if not block:
        ps, lq = db.get("position_state") or {}, db.get("liquidation_state") or {}
        known_ids = {x for x in (ps.get("stop_order_id"), ps.get("take_profit_order_id"), lq.get("order_id")) if x}
        known_cids = {x for x in [lq.get("client_order_id")] +
                      [a.get("cid") for a in lq.get("abandoned_attempts") or []] if x}
        for o in orders:
            if o["id"] not in known_ids and o.get("client_order_id") not in known_cids:
                flags.append(f"unrecognized open {o['side']} order {o['id']} (cid {o.get('client_order_id')})")
        buys = [f for f in fills if f["side"] == "buy" and day_start and _ts(f["time"]) >= day_start]
        if buys:
            flags.append(f"{len(buys)} BUY fill(s) since the breaker day began: re-entry (manual or other process). "
                         "Option B would liquidate it.")
        seen = {o.get("client_order_id") for o in orders} | {f.get("client_order_id") for f in fills}
        for c in {lq.get("client_order_id")} - {None} - seen:
            flags.append(f"DB attempt {c} appears in neither open orders nor fills of this snapshot "
                         "(NOT proof it was never placed: check the broker's full order history for that client id)")
        facts["d10_condition"] = bool(stamp and qty and qty > 0.0001 and not lq)
    verdict = "BLOCK" if block else ("FLAGS" if flags else "OK_FOR_REVIEW")
    return {"verdict": verdict, "block": block, "flags": flags, "facts": facts}


def _canon(o):
    return tuple((k, o.get(k)) for k in ORDER_FIELDS)


def live_gate(snapshot, live, expected, db=None):
    """Apply-time gate. Re-validates the snapshot, validates the live read, requires identity (account/environment/
    symbol) to match BOTH expected and the snapshot, freshness, equal valid quantities, and IDENTICAL authoritative
    order details (not just the same ids). Returns (ok, reason). See module docstring for the remaining non-atomic gap."""
    r = check(snapshot, db or {"breaker_tripped_stamp": None, "position_state": {}, "liquidation_state": {}}, expected)
    if r["verdict"] == "BLOCK":
        return False, "snapshot invalid: " + "; ".join(r["block"][:3])
    if not isinstance(live, dict):
        return False, "live read missing"
    if live.get("complete") is not True:
        return False, "live read not attested complete"
    for k in ("environment", "account_key", "symbol"):
        if not isinstance(live.get(k), str) or live.get(k) != expected.get(k) or live.get(k) != snapshot.get(k):
            return False, f"live {k} {live.get(k)!r} does not match expected/snapshot"
    read_at, taken, now = _ts(live.get("read_at")), _ts(snapshot.get("taken_at")), _ts(expected.get("now"))
    if read_at is None or taken is None or now is None:
        return False, "live read_at/snapshot taken_at/now invalid"
    if read_at < taken:
        return False, "live read is older than the snapshot"
    if read_at > now:
        return False, "live read_at is in the future"
    if (now - read_at).total_seconds() > expected.get("live_max_age_s", 60):
        return False, "live read stale"
    lq, sq = _num(live.get("position_qty"), 0.0), _num(snapshot.get("position_qty"), 0.0)
    if lq is None or sq is None:
        return False, "live/snapshot quantity invalid"
    if abs(lq - sq) > 1e-9:
        return False, f"live qty {lq} != snapshot {sq}"
    lo = live.get("open_orders")
    if not isinstance(lo, list):
        return False, "live open_orders missing"
    probs = [p for i, o in enumerate(lo) for p in _order_problems(o, expected.get("symbol"), f"live[{i}]")]
    if probs:
        return False, "live order invalid: " + "; ".join(probs[:3])
    if sorted(map(_canon, lo)) != sorted(map(_canon, snapshot.get("open_orders") or [])):
        return False, "live open orders differ from the snapshot (ids and/or authoritative details)"
    return True, "live state matches snapshot (NOT atomic with the action that follows)"
