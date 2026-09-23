"""DRAFT (not adopted) — D10 legacy-state check against a DAVID-SUPPLIED broker snapshot.

Pure and read-only: no broker calls, no DB writes. An offline check cannot establish current broker truth; it only
tells whether a snapshot taken at time T is internally complete and consistent with the DB, and what it implies.
Any resolution must ALSO pass a live gate at apply time (live_gate()): the bot's own strict reads must still match.

snapshot = {"taken_at": ISO-8601 UTC, "environment": "paper"|"live", "account_key": str, "symbol": "BTC/USD",
            "position_qty": float|None,
            "open_orders": [{"id", "client_order_id", "side", "status", "qty", "filled_qty", "type"}],
            "fills_since": ISO-8601 UTC,
            "fills": [{"order_id", "client_order_id", "side", "qty", "price", "time"}]}
db = {"breaker_tripped_stamp": "YYYY-MM-DD"|None, "position_state": {...}, "liquidation_state": {...}}
expected = {"environment", "account_key", "symbol", "now": ISO-8601 UTC, "max_age_s": int}
"""
from datetime import datetime, timezone

OPEN_STATUSES = {"new", "accepted", "held", "pending_new", "partially_filled", "pending_cancel", "pending_replace",
                 "accepted_for_bidding", "calculated"}
KNOWN_STATUSES = OPEN_STATUSES | {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped",
                                  "suspended"}


def _t(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def check(snapshot, db, expected):
    block, flags, facts = [], [], {}
    for k in ("environment", "account_key", "symbol"):
        if snapshot.get(k) != expected.get(k):
            block.append(f"{k} mismatch: snapshot={snapshot.get(k)!r} expected={expected.get(k)!r}")
    try:
        taken, now = _t(snapshot["taken_at"]), _t(expected["now"])
        age = (now - taken).total_seconds()
        facts["age_s"] = age
        if age < 0:
            block.append("snapshot taken_at is in the future")
        elif age > expected.get("max_age_s", 600):
            block.append(f"snapshot stale: {age:.0f}s > {expected.get('max_age_s', 600)}s")
    except (KeyError, ValueError):
        block.append("taken_at/now missing or unparseable")
    stamp = db.get("breaker_tripped_stamp")
    day_start = _t(stamp + "T00:00:00+00:00") if stamp else None
    try:
        if day_start and _t(snapshot["fills_since"]) > day_start:
            block.append("fill history starts after the breaker day began (incomplete)")
        if day_start and _t(snapshot["taken_at"]) < day_start:
            block.append("snapshot predates the breaker day")
    except (KeyError, ValueError):
        block.append("fills_since missing or unparseable")
    qty = snapshot.get("position_qty")
    if not isinstance(qty, (int, float)):
        block.append("position_qty missing/unknown")
    facts["position_qty"] = qty
    ps, lq = db.get("position_state") or {}, db.get("liquidation_state") or {}
    known_ids = {x for x in (ps.get("stop_order_id"), ps.get("take_profit_order_id"), lq.get("order_id")) if x}
    known_cids = {x for x in [lq.get("client_order_id")] + [a.get("cid") for a in lq.get("abandoned_attempts") or []] if x}
    for o in snapshot.get("open_orders") or []:
        st = str(o.get("status"))
        if st not in KNOWN_STATUSES:
            block.append(f"order {o.get('id')} has unknown status {st!r}")
        elif st not in OPEN_STATUSES:
            block.append(f"order {o.get('id')} listed as open but status {st!r} is terminal (inconsistent snapshot)")
        if o.get("id") not in known_ids and o.get("client_order_id") not in known_cids:
            flags.append(f"unrecognized open {o.get('side')} order {o.get('id')} (cid {o.get('client_order_id')})")
    buys = [f for f in snapshot.get("fills") or [] if f.get("side") == "buy" and day_start and _t(f["time"]) >= day_start]
    if buys:
        flags.append(f"{len(buys)} BUY fill(s) since the breaker day began: re-entry (manual or other process). "
                     "Option B would liquidate it.")
    ref_cids = {lq.get("client_order_id")} - {None}
    seen = {o.get("client_order_id") for o in snapshot.get("open_orders") or []} | \
           {f.get("client_order_id") for f in snapshot.get("fills") or []}
    for c in ref_cids - seen:
        flags.append(f"DB attempt {c} appears in neither open orders nor fills of this snapshot "
                     "(NOT proof it was never placed: check the broker's full order history for that client id)")
    legacy = bool(stamp and isinstance(qty, (int, float)) and qty > 0.0001 and not lq)
    facts["d10_condition"] = legacy
    verdict = "BLOCK" if block else ("FLAGS" if flags else "OK_FOR_REVIEW")
    return {"verdict": verdict, "block": block, "flags": flags, "facts": facts}


def live_gate(snapshot, live_qty, live_open_order_ids):
    """At APPLY time, inside the bot: its own strict reads must still match the snapshot, else refuse."""
    if live_qty is None or live_open_order_ids is None:
        return False, "live reads UNKNOWN"
    if abs(float(live_qty) - float(snapshot.get("position_qty") or 0)) > 1e-9:
        return False, f"live qty {live_qty} != snapshot {snapshot.get('position_qty')}"
    snap_ids = {o.get("id") for o in snapshot.get("open_orders") or []}
    if set(live_open_order_ids) != snap_ids:
        return False, "live open orders differ from snapshot"
    return True, "live state matches snapshot"
