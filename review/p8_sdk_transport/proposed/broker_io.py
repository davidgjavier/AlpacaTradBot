"""broker_io.py — PROPOSED thin I/O layer over alpaca-py 0.43.5. NOT wired into any bot.

Why a wrapper: in alpaca-py 0.43.5 retry behavior is a single per-client setting
(RESTClient._retry / _retry_codes) applied to EVERY method, and TradingClient's public
constructor does not expose it; no request timeout is ever passed to requests.
So this module:
  1. configure_client(): disables SDK retries for the whole client (_retry = 0) and mounts a
     requests adapter that applies explicit (connect, read) timeouts. Fails closed if the SDK
     version or the private attributes it relies on differ.
  2. read(): bounded, deadline-limited retries for READ-ONLY calls, returning OK / NOT_FOUND /
     UNAVAILABLE / ERROR. NOT_FOUND only from a structured HTTP 404 + code 40410000.
  3. OrderGateway.submit(): at most ONE POST per client_order_id, ever. The durable intent row
     (client id + payload) is committed BEFORE the POST; if that write fails nothing is sent
     (NOT_SUBMITTED). Only the caller that created the row may POST (insert = ownership).
     Ambiguous outcomes (5xx, 429, timeouts, connection errors, non-JSON) become UNRESOLVED.
     After a POST has been attempted, results are RETURNED, never raised, including when the
     result cannot be saved (persisted=False, row stays in the recovery queue).
  4. OrderGateway.reconcile(): only POSITIVE broker evidence (lookup or list hit) resolves an
     intent (ACCEPTED). Negative evidence, elapsed time and incomplete listings never prove
     absence; the intent stays UNRESOLVED and queued. State changes are compare-and-set, so a
     stale caller cannot downgrade ACCEPTED/REJECTED. REJECTED only from a definitive 4xx on the
     single POST. resubmit() is DISABLED in round 2 (read-only reconcile, never POSTs).
  Timeout/deadline bounds and paper-plan sizing are OPEN (next round).

It does not bypass broker validation: every order still goes through submit_order and the
broker's own checks. Undocumented broker behavior (duplicate client ids, lookup visibility
timing) is NOT assumed; see review/p8_sdk_transport/PAPER_TEST_PLAN_v2.md (P3).
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests
from requests.adapters import HTTPAdapter

SUPPORTED_SDK_VERSION = "0.43.5"
DEFAULT_TIMEOUT = (3.05, 10.0)          # (connect, read) seconds — proposal, tune after P-tests
READ_DEADLINE_S = 8.0                   # WALL-CLOCK budget for one logical read incl. retries (stage 2)
SUBMIT_DEADLINE_S = 15.0                # WALL-CLOCK budget the caller waits for the single POST (stage 2)
MAX_INFLIGHT_CALLS = 8                  # bound on worker threads (incl. ones abandoned at a deadline)
# Stage 2b: SUBMIT_DEADLINE_S is the END-TO-END budget of submit(): POST wait + DB writes + any
# reconcile reads/scan. When it runs out the call returns UNRESOLVED and reconciliation is deferred.
FINAL_WRITE_RESERVE_S = 0.05            # budget kept back for the final state write
SCHED_MARGIN_S = 0.01                   # thread start / wake-up overhead
DB_BUSY_TIMEOUT_S = 5.0                 # sqlite busy timeout when no budget applies (was 10 s)
LOCK_RELEASE_ENABLED = False            # operational lock release OFF until mechanism + policy approved
READ_MAX_ATTEMPTS = 5
VISIBILITY_WINDOW_S = 30.0              # before "not found" may be treated as absence (UNVERIFIED value)
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
DEFINITIVE_REJECT_HTTP = {400, 401, 403, 422}   # documented: 403 insufficient, 422 bad input


# ------------------------------------------------------------------ transport
class TimeoutHTTPAdapter(HTTPAdapter):
    """Applies a default (connect, read) timeout when the caller passed none — alpaca-py
    0.43.5 never passes one. Subclasses may override _transport_send (used by tests)."""

    def __init__(self, timeout=DEFAULT_TIMEOUT, **kw):
        self.default_timeout = timeout
        super().__init__(**kw)

    def send(self, request, **kw):
        if kw.get("timeout") is None:
            kw["timeout"] = self.default_timeout
        return self._transport_send(request, **kw)

    def _transport_send(self, request, **kw):
        return super().send(request, **kw)


class ConfigurationError(RuntimeError):
    pass


def apply_timeouts_only(client, timeout=DEFAULT_TIMEOUT, adapter: Optional[HTTPAdapter] = None,
                        expected_version: str = SUPPORTED_SDK_VERSION):
    """Stage A: explicit timeouts on an EXISTING client while KEEPING the SDK's own 429/504
    retries (so current read paths are no worse off). Does NOT remove the duplicate-POST risk:
    submissions on this client are still retried by the SDK. Fails closed like configure_client."""
    import alpaca
    if alpaca.__version__ != expected_version:
        raise ConfigurationError(f"alpaca-py {alpaca.__version__} != supported {expected_version}")
    if not hasattr(client, "_session"):
        raise ConfigurationError("SDK client lacks _session; cannot configure safely")
    ad = adapter or TimeoutHTTPAdapter(timeout=timeout)
    client._session.mount("https://", ad)
    client._session.mount("http://", ad)
    return client


def configure_client(client, timeout=DEFAULT_TIMEOUT, adapter: Optional[HTTPAdapter] = None,
                     expected_version: str = SUPPORTED_SDK_VERSION):
    """Mutates ONE client instance. Fails closed rather than silently running with defaults."""
    import alpaca
    if alpaca.__version__ != expected_version:
        raise ConfigurationError(f"alpaca-py {alpaca.__version__} != supported {expected_version}; "
                                 f"re-verify _retry/_session semantics before use")
    for attr in ("_session", "_retry", "_retry_codes"):
        if not hasattr(client, attr):
            raise ConfigurationError(f"SDK client lacks {attr}; cannot configure safely")
    client._retry = 0                      # SDK: RetryException only raised when retry > 0
    ad = adapter or TimeoutHTTPAdapter(timeout=timeout)
    client._session.mount("https://", ad)
    client._session.mount("http://", ad)
    client._broker_io_configured = True
    return client


# ------------------------------------------------------------------ classification
def _status(e) -> Optional[int]:
    return getattr(e, "status_code", None)


def _code(e) -> Optional[int]:
    try:
        c = getattr(e, "code", None)
        return int(c) if c is not None else None
    except Exception:                      # APIError.code json-parses the body; may raise
        return None


def is_confirmed_not_found(e) -> bool:
    return _status(e) == 404 and _code(e) == 40410000


def is_retryable(e) -> bool:
    if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    s = _status(e)
    return s in RETRYABLE_HTTP or (s is not None and s >= 500)


# ------------------------------------------------------------------ reads
# ------------------------------------------------------------------ wall-clock deadlines (stage 2)
@dataclass
class CallOutcome:
    state: str                     # OK | ERROR | DEADLINE | SATURATED
    value: Any = None
    error: Optional[BaseException] = None


class CallRunner:
    """Runs one broker call in a daemon worker thread; the CALLER waits at most `timeout_s`
    wall-clock seconds.

    A timed-out wait does NOT cancel the request: the worker keeps running and may complete
    later (late completion). Its result is DISCARDED: never returned and never applied, because
    only caller threads write state. The number of workers, including abandoned ones still in
    flight, is bounded. When the bound is reached, calls fail closed (SATURATED) without sending
    anything. Workers inherit the caller's thread name for tracing."""

    def __init__(self, max_inflight: int = MAX_INFLIGHT_CALLS):
        self.max_inflight = max_inflight
        self._lock = threading.Lock()
        self.inflight = 0
        self.abandoned = 0          # waits that hit the deadline
        self.late_completions = 0   # abandoned calls that later finished (result discarded)

    def reserve(self) -> bool:
        with self._lock:
            if self.inflight >= self.max_inflight:
                return False
            self.inflight += 1
            return True

    def release_unused(self):
        with self._lock:
            self.inflight -= 1

    def run_reserved(self, fn: Callable[[], Any], timeout_s: float,
                     on_done: Optional[Callable[[Any, Optional[BaseException]], None]] = None) -> CallOutcome:
        """on_done runs in the WORKER when the request actually finishes (even after the caller
        gave up), so callers can track what is still in flight. It must not write intent state."""
        box, done, flag = {}, threading.Event(), {"abandoned": False}

        def work():
            try:
                box["v"] = fn()
            except BaseException as e:  # noqa: BLE001
                box["e"] = e
            finally:
                if on_done:
                    try:
                        on_done(box.get("v"), box.get("e"))
                    except Exception:  # noqa: BLE001
                        pass
                with self._lock:
                    self.inflight -= 1
                    if flag["abandoned"]:
                        self.late_completions += 1
                done.set()
        threading.Thread(target=work, daemon=True, name=threading.current_thread().name).start()
        if not done.wait(max(0.0, timeout_s)):
            with self._lock:
                if not done.is_set():
                    flag["abandoned"] = True
                    self.abandoned += 1
            if flag["abandoned"]:
                return CallOutcome("DEADLINE", error=TimeoutError(
                    f"no result within {timeout_s:.2f}s; request NOT cancelled and may complete late"))
        if "e" in box:
            return CallOutcome("ERROR", error=box["e"])
        return CallOutcome("OK", value=box.get("v"))

    def try_run(self, fn: Callable[[], Any], timeout_s: float) -> CallOutcome:
        if not self.reserve():
            return CallOutcome("SATURATED", error=RuntimeError(
                f"{self.max_inflight} broker calls already in flight; failing closed"))
        return self.run_reserved(fn, timeout_s)


DEFAULT_RUNNER = CallRunner()


class Budget:
    """One monotonic wall-clock budget carried through a complete operation (stage 2b)."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class ReadResult:
    state: str                     # OK | NOT_FOUND | UNAVAILABLE | ERROR
    value: Any = None
    attempts: int = 0
    error: Optional[str] = None


def read(fn: Callable[[], Any], deadline_s: float = READ_DEADLINE_S, max_attempts: int = READ_MAX_ATTEMPTS,
         base_backoff_s: float = 0.25, max_backoff_s: float = 2.0,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
         rng: random.Random = random.Random(0), runner: Optional["CallRunner"] = None) -> ReadResult:
    """Bounded retries for READ-ONLY calls within a WALL-CLOCK budget (stage 2).

    Every attempt runs through a CallRunner and is given only the REMAINING budget, and backoff
    never sleeps past it, so the caller never waits materially longer than `deadline_s`, including
    on slow-drip responses the socket read timeout cannot catch. A timed-out attempt may still
    complete late; its result is discarded. Never returns NOT_FOUND for anything but a structured
    404/40410000; exhausted budget, saturation or deadline are UNAVAILABLE, never 'absent'."""
    runner = runner or DEFAULT_RUNNER
    wall_start = time.monotonic()                 # real time bounds the caller even with a fake clock

    def remaining():
        return min(deadline_s - (clock() - start), deadline_s - (time.monotonic() - wall_start))
    start, attempt, last = clock(), 0, None
    while attempt < max_attempts:
        left = remaining()
        if left <= 0:
            break
        attempt += 1
        out = runner.try_run(fn, left)
        if out.state == "OK":
            return ReadResult("OK", out.value, attempt)
        if out.state in ("DEADLINE", "SATURATED"):
            return ReadResult("UNAVAILABLE", None, attempt, f"{out.state}: {out.error}")
        e = out.error
        last = e
        if is_confirmed_not_found(e):
            return ReadResult("NOT_FOUND", None, attempt, str(e)[:200])
        if not is_retryable(e):
            return ReadResult("ERROR", None, attempt, f"{type(e).__name__}: {str(e)[:200]}")
        delay = min(max_backoff_s, base_backoff_s * (2 ** (attempt - 1))) * (0.5 + rng.random() / 2)
        if delay >= remaining():
            break
        sleep(delay)
    return ReadResult("UNAVAILABLE", None, attempt, f"{type(last).__name__}: {str(last)[:200]}")


def cancel(client, order_id, **read_kw) -> ReadResult:
    """DELETE /orders/{id}. Retrying a cancel request cannot create exposure, so it uses the
    bounded read policy. 204 = request ACCEPTED, NOT terminal cancellation — callers must poll
    the order until canceled/filled/expired. 422 = not cancelable (e.g. already filled)."""
    _not_cancelable = object()

    def attempt():
        try:
            client.cancel_order_by_id(order_id)
            return None
        except Exception as e:  # noqa: BLE001
            if _status(e) == 422:              # structured status, not message text
                return _not_cancelable
            raise

    r = read(attempt, **read_kw)
    if r.state == "OK":
        if r.value is _not_cancelable:
            return ReadResult("NOT_CANCELABLE", None, r.attempts, "HTTP 422")
        return ReadResult("CANCEL_REQUEST_ACCEPTED", None, r.attempts)
    return r


# ------------------------------------------------------------------ durable intents
class PersistenceError(RuntimeError):
    pass


INTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_intents (
  client_order_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, symbol TEXT NOT NULL,
  payload TEXT NOT NULL, payload_sha TEXT NOT NULL, state TEXT NOT NULL,
  order_id TEXT, submit_attempts INTEGER NOT NULL DEFAULT 0,
  created_ns INTEGER NOT NULL, last_submit_ns INTEGER, updated_ns INTEGER NOT NULL, last_error TEXT);
CREATE TABLE IF NOT EXISTS intent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL, ts_ns INTEGER NOT NULL,
  kind TEXT NOT NULL, from_state TEXT, to_state TEXT, operator TEXT, note TEXT, detail TEXT);
"""
INTENT_MIGRATIONS = [("post_inflight", "INTEGER NOT NULL DEFAULT 0"), ("post_started_ns", "INTEGER"),
                     ("post_finished_ns", "INTEGER"), ("post_result", "TEXT"),
                     ("acknowledged_by", "TEXT"), ("acknowledged_ns", "INTEGER"), ("abandoned_by", "TEXT"),
                     ("abandoned_ns", "INTEGER"), ("lock_released_by", "TEXT"), ("lock_released_ns", "INTEGER"),
                     ("conflict_detail", "TEXT")]
# States (round 3):
#   SUBMITTING  intent durable; the single POST may or may not have reached the broker
#   ACCEPTED    one broker order, identity matches the intent (terminal for this layer)
#   REJECTED    definitive 4xx on the ONLY POST ever made for this id (terminal)
#   UNRESOLVED  outcome unknown; monitored; entries locked
#   CONFLICT    broker evidence contradicts the intent (identity mismatch, >1 order for the id,
#               or a late order after abandonment); monitored; entries locked; human only
#   ABANDONED   operator declared the intent abandoned; STILL monitored for a late order;
#               entries stay locked until a separate, explicit release_entry_lock()
# Transitions are compare-and-set and every one appends to intent_events (history is never erased).
#   ACCEPTED_UNVERIFIED (round 3b)  one identity-matching broker order is known, but a COMPLETE
#               bounded scan has not yet confirmed it is the only order for the client id.
#               Monitored; entries locked; re-scanned every cycle; persists across restarts.
PENDING_STATES = ("SUBMITTING", "UNRESOLVED", "NOT_FOUND_AFTER_WINDOW", "CONFLICT", "ABANDONED",
                  "ACCEPTED_UNVERIFIED")
_OPEN = ("SUBMITTING", "UNRESOLVED", "NOT_FOUND_AFTER_WINDOW")
_ALLOWED_FROM = {"ACCEPTED": _OPEN + ("ACCEPTED_UNVERIFIED",),
                 "ACCEPTED_UNVERIFIED": _OPEN + ("ACCEPTED_UNVERIFIED",),
                 "REJECTED": ("SUBMITTING",),
                 "UNRESOLVED": _OPEN,
                 "CONFLICT": _OPEN + ("ABANDONED", "ACCEPTED_UNVERIFIED"),
                 "ABANDONED": ("UNRESOLVED", "NOT_FOUND_AFTER_WINDOW")}


class IntentStore:
    def __init__(self, path: str, fail_hook: Optional[Callable[[str], None]] = None,
                 busy_timeout_s: float = DB_BUSY_TIMEOUT_S):
        self.path, self.fail_hook, self.busy_timeout_s = path, fail_hook, busy_timeout_s
        self._tl = threading.local()               # per-thread busy-timeout override (budgeted callers)
        with closing(self._c()) as c:
            c.executescript(INTENT_SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(order_intents)")}
            for name, typ in INTENT_MIGRATIONS:
                if name not in have:
                    c.execute(f"ALTER TABLE order_intents ADD COLUMN {name} {typ}")

    def bounded(self, timeout_s: Optional[float]):
        """Context: cap sqlite busy waits for this thread's store calls (stage 2b)."""
        store = self

        class _Ctx:
            def __enter__(self_):
                self_.prev = getattr(store._tl, "timeout", None)
                store._tl.timeout = timeout_s

            def __exit__(self_, *a):
                store._tl.timeout = self_.prev
        return _Ctx()

    def _c(self):
        t = getattr(self._tl, "timeout", None)
        t = self.busy_timeout_s if t is None else max(0.001, min(t, self.busy_timeout_s))
        c = sqlite3.connect(self.path, timeout=t, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")      # durable before we act on it
        return c

    def _txn(self, op: str, fn):
        """Run fn(conn) in one IMMEDIATE transaction. Raises PersistenceError on storage failure;
        sqlite3.IntegrityError is re-raised unchanged (callers treat it as 'already exists')."""
        try:
            if self.fail_hook:
                self.fail_hook(op)                 # test fault injection, handled like a real failure
            with closing(self._c()) as c:
                c.execute("BEGIN IMMEDIATE")
                try:
                    out = fn(c)
                    c.execute("COMMIT")
                    return out
                except Exception:
                    c.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PersistenceError(f"{op}: {e}") from e

    @staticmethod
    def _event(c, cid, ts, kind, frm=None, to=None, operator=None, note=None, detail=None):
        c.execute("INSERT INTO intent_events (client_order_id,ts_ns,kind,from_state,to_state,operator,note,detail)"
                  " VALUES (?,?,?,?,?,?,?,?)", (cid, ts, kind, frm, to, operator, note, detail))

    def get(self, cid: str) -> Optional[dict]:
        with closing(self._c()) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM order_intents WHERE client_order_id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def pending(self) -> list:
        """The monitoring queue: every intent whose broker exposure is not settled, INCLUDING
        CONFLICT and ABANDONED (a late order must still be detected)."""
        with closing(self._c()) as c:
            c.row_factory = sqlite3.Row
            q = "SELECT * FROM order_intents WHERE state IN (%s)" % ",".join("?" * len(PENDING_STATES))
            return [dict(r) for r in c.execute(q, PENDING_STATES)]

    def locking_intents(self, symbol: str) -> list:
        with closing(self._c()) as c:
            return [r[0] for r in c.execute(
                "SELECT client_order_id FROM order_intents WHERE symbol=? AND ("
                " state IN ('SUBMITTING','UNRESOLVED','NOT_FOUND_AFTER_WINDOW','CONFLICT','ACCEPTED_UNVERIFIED')"
                " OR (state='ABANDONED' AND lock_released_ns IS NULL))", (symbol,))]

    def events(self, cid: str) -> list:
        with closing(self._c()) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(
                "SELECT * FROM intent_events WHERE client_order_id=? ORDER BY id", (cid,))]

    def create_submitting(self, cid, purpose, symbol, payload, now_ns) -> bool:
        """True if this caller created the intent (and therefore owns the single POST);
        False if the id already exists (another caller owns it). Storage failure raises."""
        pj = json.dumps(payload, sort_keys=True, default=str)

        def fn(c):
            # post_inflight=1 in the SAME transaction: ownership is durable and visible to every
            # gateway and process before any POST can start (stage 2b).
            c.execute("INSERT INTO order_intents (client_order_id,purpose,symbol,payload,payload_sha,state,"
                      "submit_attempts,created_ns,last_submit_ns,updated_ns,post_inflight,post_started_ns)"
                      " VALUES (?,?,?,?,?,?,1,?,?,?,1,?)",
                      (cid, purpose, symbol, pj, hashlib.sha256(pj.encode()).hexdigest(), "SUBMITTING",
                       now_ns, now_ns, now_ns, now_ns))
            self._event(c, cid, now_ns, "CREATED", None, "SUBMITTING")
        try:
            self._txn("create_submitting", fn)
            return True
        except sqlite3.IntegrityError:
            return False

    def transition(self, cid, to_state, now_ns, order_id=None, error=None, conflict_detail=None) -> bool:
        """Compare-and-set with an appended history event. True only if the row was in an allowed
        source state. REJECTED additionally requires submit_attempts == 1."""
        src = _ALLOWED_FROM[to_state]
        extra = " AND submit_attempts = 1" if to_state == "REJECTED" else ""

        def fn(c):
            row = c.execute("SELECT state FROM order_intents WHERE client_order_id=?", (cid,)).fetchone()
            frm = row[0] if row else None
            n = c.execute("UPDATE order_intents SET state=?, order_id=COALESCE(?,order_id), last_error=?, updated_ns=?,"
                          " conflict_detail=COALESCE(?,conflict_detail)"
                          " WHERE client_order_id=? AND state IN (%s)%s" % (",".join("?" * len(src)), extra),
                          (to_state, order_id, error, now_ns, conflict_detail, cid) + src).rowcount
            if n == 1 and frm != to_state:          # same-state refreshes update the row, not history
                self._event(c, cid, now_ns, "TRANSITION", frm, to_state, detail=conflict_detail or error)
            return n == 1
        return self._txn(f"mark_{to_state}", fn)

    def operator_action(self, cid, kind, operator, note, now_ns, require_state=None, set_cols=None) -> bool:
        """Records ACKNOWLEDGED / ABANDONED / LOCK_RELEASED. State change only for ABANDONED."""
        def fn(c):
            row = c.execute("SELECT state FROM order_intents WHERE client_order_id=?", (cid,)).fetchone()
            if row is None or (require_state and row[0] not in require_state):
                return False
            frm = row[0]
            to = "ABANDONED" if kind == "ABANDONED" else frm
            cols = dict(set_cols or {})
            cols["updated_ns"] = now_ns
            if kind == "ABANDONED":
                cols["state"] = "ABANDONED"
            sets = ", ".join(f"{k}=?" for k in cols)
            n = c.execute(f"UPDATE order_intents SET {sets} WHERE client_order_id=? AND state=?",
                          tuple(cols.values()) + (cid, frm)).rowcount
            if n == 1:
                self._event(c, cid, now_ns, kind, frm, to, operator=operator, note=note)
            return n == 1
        return self._txn(f"op_{kind}", fn)

    def mark_post_finished(self, cid, result: str, now_ns: int) -> bool:
        """Called by the POST worker when its request actually ends (possibly after the caller left).
        Bookkeeping only: clears post_inflight and records the transport outcome; never changes
        `state`. If this write fails, post_inflight stays 1 (conservative)."""
        def fn(c):
            # Columns only: no history event, no `state`/`updated_ns` change. The stage-2 invariant is
            # that a late response never mutates intent state or history; post_result is used only to
            # BLOCK resolution (an 'ok:' outcome refuses abandon), never to advance state.
            n = c.execute("UPDATE order_intents SET post_inflight=0, post_finished_ns=?, post_result=?"
                          " WHERE client_order_id=?", (now_ns, result[:300], cid)).rowcount
            return n == 1
        return self._txn("post_finished", fn)

    def mark(self, cid, state, now_ns, order_id=None, error=None, bump_attempt=False):
        """Backwards-compatible alias; compare-and-set. Re-submission (attempt bumps) is disabled."""
        if bump_attempt:
            raise PersistenceError("re-submission is disabled; attempts cannot be bumped")
        return self.transition(cid, state, now_ns, order_id=order_id, error=error)


def new_client_order_id(prefix: str) -> str:
    """<= 128 chars (documented limit)."""
    cid = f"{prefix}-{uuid.uuid4().hex}"
    assert len(cid) <= 128
    return cid


# ------------------------------------------------------------------ identity validation
_ENUM_PREFIXES = ("OrderSide", "OrderType", "TimeInForce", "OrderClass", "PositionIntent")


def _tok(v) -> Optional[str]:
    """Normalize enum-ish values: OrderSide.SELL / 'sell' / <OrderSide.SELL: 'sell'> -> 'sell'."""
    if v is None:
        return None
    s = str(getattr(v, "value", v)).strip()
    if "." in s and s.split(".")[0] in _ENUM_PREFIXES:
        s = s.split(".", 1)[1]
    return s.lower()


def _sym(v) -> Optional[str]:
    """Broker may report BTC/USD as BTCUSD (Alpaca position/asset symbology)."""
    return None if v is None else str(v).replace("/", "").upper()


def _dec(v):
    if v is None or v == "" or str(v) == "None":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return "INVALID"


def identity_mismatches(intent: dict, order) -> list:
    """Compare a broker order with the stored intent payload. Numeric fields compare by value,
    so broker formatting ('0.00020000', '100000.00') is not a mismatch. Qty-versus-notional:
    a notional intent must match on notional (qty is ignored; Alpaca may leave it null or fill it);
    a qty intent must match on qty and the broker must not report a notional."""
    g = (lambda k: getattr(order, k, None)) if not isinstance(order, dict) else order.get
    out = []
    if (g("client_order_id") or None) != intent.get("client_order_id"):
        out.append(f"client_order_id {g('client_order_id')!r}")
    if _sym(g("symbol")) != _sym(intent.get("symbol")):
        out.append(f"symbol {g('symbol')!r} != {intent.get('symbol')!r}")
    for f in ("side", "type", "time_in_force"):
        bv = g(f) if f != "type" else (g("type") or g("order_type"))
        if _tok(bv) != _tok(intent.get(f)):
            out.append(f"{f} {_tok(bv)!r} != {_tok(intent.get(f))!r}")
    if _dec(intent.get("notional")) is not None:
        bn = _dec(g("notional"))
        if bn is None:
            out.append("notional intent but broker reports no notional (qty-vs-notional)")
        elif bn != _dec(intent.get("notional")):
            out.append(f"notional {bn} != {_dec(intent.get('notional'))}")
    else:
        bq, iq = _dec(g("qty")), _dec(intent.get("qty"))
        if bq != iq:
            out.append(f"qty {bq} != {iq}")
        if _dec(g("notional")) is not None:
            out.append("qty intent but broker reports a notional (qty-vs-notional)")
    for f in ("limit_price", "stop_price"):
        if _dec(g(f)) != _dec(intent.get(f)):
            out.append(f"{f} {_dec(g(f))} != {_dec(intent.get(f))}")
    return out


# ------------------------------------------------------------------ bounded positive-evidence scan
SCAN_PAGE_LIMIT = 500          # Alpaca max per page (GetOrdersRequest.limit)
SCAN_MAX_PAGES = 3             # hard bound per reconcile; exceeding it is "incomplete", never "absent"


@dataclass
class ScanResult:
    matches: list
    complete: bool
    reason: str
    pages: int


# ------------------------------------------------------------------ submissions
RESUBMISSION_ENABLED = False   # Never re-POST an ambiguous intent automatically (rounds 2-3 policy).


@dataclass
class SubmitResult:
    """Outcome of one gateway call. Callers MUST read `state` AND `persisted`.

    state:
      ACCEPTED      one broker order, identity matches; `order_id` is set.
      REJECTED      the single POST was definitively refused (400/401/403/422 on attempt 1).
      UNRESOLVED    outcome unknown. NOT "not placed". Exposure may exist.
      CONFLICT      broker evidence contradicts the intent (mismatch, several orders, late order).
      ABANDONED     operator-abandoned, still monitored; exposure still possible.
      NOT_SUBMITTED nothing was ever sent: intent history was READABLE and empty, and the new intent
                    could not be stored. Never returned when prior history is unreadable.
    persisted: False if the store could not be read or written for this result.
    uniqueness_verified: True only if a COMPLETE bounded scan found exactly this one order for
      the client id; False if the scan was incomplete; None if no scan ran.
    """
    state: str
    client_order_id: str
    order_id: Optional[str] = None
    order: Any = None
    detail: str = ""
    posts_this_call: int = 0
    persisted: bool = True
    persistence_error: Optional[str] = None
    broker_order_ids: list = field(default_factory=list)
    mismatches: list = field(default_factory=list)
    uniqueness_verified: Optional[bool] = None
    # Evidence quality of THIS call's broker check (round 3b):
    #   POSITIVE           a broker order for the id was observed
    #   NEGATIVE_COMPLETE  structured 404 on the by-id lookup AND a complete bounded scan with no match.
    #                      A successful negative observation. Still NOT proof that exposure is absent.
    #   INCOMPLETE         any read failed/unavailable, the scan was truncated, bounded, stuck or not run
    #   NOT_CHECKED        no broker check was made (e.g. already terminal, or store unreadable)
    evidence: str = "NOT_CHECKED"

    @property
    def exposure_may_exist(self) -> bool:
        return self.state in ("ACCEPTED", "ACCEPTED_UNVERIFIED", "UNRESOLVED", "CONFLICT", "ABANDONED")


class OrderGateway:
    def __init__(self, client, store: IntentStore, symbol: str,
                 now_ns: Callable[[], int] = time.time_ns, read_kw: Optional[dict] = None,
                 visibility_window_s: float = VISIBILITY_WINDOW_S,
                 scan_max_pages: int = SCAN_MAX_PAGES, scan_page_limit: int = SCAN_PAGE_LIMIT):
        if not getattr(client, "_broker_io_configured", False):
            raise ConfigurationError("client must be passed through configure_client() first")
        self.client, self.store, self.symbol = client, store, symbol
        self.now_ns, self.read_kw = now_ns, (read_kw or {})
        self.window_ns = int(visibility_window_s * 1e9)
        self.scan_max_pages, self.scan_page_limit = scan_max_pages, scan_page_limit
        self.runner = CallRunner()                  # stage 2: bounded workers, wall-clock waits
        self.submit_deadline_s = SUBMIT_DEADLINE_S
        self.lock_release_enabled = LOCK_RELEASE_ENABLED   # stage 2b: operationally OFF by default
        self._tl = threading.local()                       # current end-to-end Budget (per caller thread)

    def submit_in_flight(self, cid) -> bool:
        """DURABLE ownership (stage 2b): true while the intent row says its POST has not finished,
        whichever gateway/process sent it. Never expires; an owner crash leaves it true (uncertainty
        preserved). Fail-closed: an unreadable row counts as in flight."""
        row, err = self._row(cid)
        if err:
            return True
        return bool(row and row.get("post_inflight"))

    def _post_outcome_blocks_resolution(self, cid) -> Optional[str]:
        row, err = self._row(cid)
        if err:
            return f"intent unreadable ({err})"
        if row is None:
            return None
        if row.get("post_inflight"):
            return "a POST for this id has not finished (any gateway/process; survives crashes)"
        if (row.get("post_result") or "").startswith("ok:"):
            return f"the POST for this id was accepted by the broker ({row['post_result']})"
        return None

    # ---- end-to-end budget helpers
    def _budget(self) -> Optional[Budget]:
        return getattr(self._tl, "budget", None)

    def _db_timeout(self) -> Optional[float]:
        b = self._budget()
        return None if b is None else max(0.001, b.remaining() - SCHED_MARGIN_S)

    def _rk(self) -> dict:
        """read() kwargs with this gateway's runner (tests may replace self.runner at any time)."""
        kw = dict(self.read_kw)
        kw.setdefault("runner", self.runner)
        b = self._budget()
        if b is not None:                            # reads get only what the caller's budget allows
            left = b.remaining() - FINAL_WRITE_RESERVE_S - SCHED_MARGIN_S
            kw["deadline_s"] = min(kw.get("deadline_s", READ_DEADLINE_S), max(0.0, left))
        return kw

    @staticmethod
    def _payload(order_request) -> dict:
        return order_request.to_request_fields()

    @staticmethod
    def _sha(payload) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _row(self, cid):
        """(row, error). Never raises: a store read failure is reported, not thrown."""
        try:
            with self.store.bounded(self._db_timeout()):
                return self.store.get(cid), None
        except Exception as e:  # noqa: BLE001
            return None, f"store read failed: {type(e).__name__}: {e}"

    def _from_row(self, cid, detail="", posts=0, **kw) -> SubmitResult:
        row, err = self._row(cid)
        if err:
            return SubmitResult("UNRESOLVED", cid, detail=f"{detail}; {err}".strip("; "), posts_this_call=posts,
                                persisted=False, persistence_error=err)
        st = row["state"] if row else "UNRESOLVED"
        if st == "NOT_FOUND_AFTER_WINDOW":        # legacy state: never trusted as absence
            st = "UNRESOLVED"
        extra = f"; {row['conflict_detail']}" if row and row.get("conflict_detail") and st == "CONFLICT" else ""
        return SubmitResult(st, cid, order_id=row["order_id"] if row else None, detail=detail + extra,
                            posts_this_call=posts, **kw)

    def _record(self, cid, to_state, order_id=None, error=None, conflict_detail=None):
        """(applied, persisted, persistence_error). Never raises."""
        try:
            with self.store.bounded(self._db_timeout()):
                return (self.store.transition(cid, to_state, self.now_ns(), order_id=order_id, error=error,
                                              conflict_detail=conflict_detail), True, None)
        except PersistenceError as e:
            return False, False, str(e)

    # ---------------------------------------------------------------- submit
    def submit(self, order_request, purpose: str) -> SubmitResult:
        """End-to-end budget = submit_deadline_s (stage 2b): POST wait, DB writes, reconcile reads and
        scan all draw on ONE budget. At exhaustion the result is UNRESOLVED and reconciliation is
        deferred to recover_pending(). A request that timed out may still complete later."""
        prev = self._budget()
        self._tl.budget = Budget(self.submit_deadline_s)
        try:
            return self._submit(order_request, purpose)
        finally:
            self._tl.budget = prev

    def _submit(self, order_request, purpose: str) -> SubmitResult:
        cid = getattr(order_request, "client_order_id", None)
        if not cid:
            raise ValueError("every order must carry a client_order_id (use new_client_order_id)")
        payload = self._payload(order_request)
        existing, err = self._row(cid)
        if err:
            # Prior history is UNREADABLE: this id may already have been submitted. Never report
            # NOT_SUBMITTED; never POST; keep the id. Use read-only broker evidence if available.
            return self._unreadable_history(cid, payload, err)
        if existing is not None:
            if existing["payload_sha"] != self._sha(payload):
                raise ValueError(f"client_order_id {cid} already used with a different payload")
            return self._from_row(cid, detail="existing intent; no POST")
        if not self.runner.reserve():               # stage 2: fail closed BEFORE creating any intent
            return SubmitResult("NOT_SUBMITTED", cid, persisted=True,
                                detail="broker call capacity saturated; nothing sent; no intent created")
        try:
            with self.store.bounded(self._db_timeout()):
                created = self.store.create_submitting(cid, purpose, self.symbol, payload, self.now_ns())
        except PersistenceError as e:
            self.runner.release_unused()
            # History was readable and had no row for this id, so nothing was sent under it.
            return SubmitResult("NOT_SUBMITTED", cid, detail="no prior intent; new intent not durable; nothing sent",
                                persisted=False, persistence_error=str(e))
        if not created:                             # another caller won the insert and owns the POST
            self.runner.release_unused()
            return self._from_row(cid, detail="intent owned by another caller; no POST")
        return self._post_and_record(order_request, cid, payload)

    def _unreadable_history(self, cid, payload, err) -> SubmitResult:
        base = dict(persisted=False, persistence_error=err)
        why = "prior intent history unreadable; client id retained; do NOT retry or replace"
        r = read(lambda: self.client.get_order_by_client_id(cid), **self._rk())
        if r.state == "OK":
            mm = identity_mismatches(json.loads(json.dumps(payload, default=str)), r.value)
            if mm:
                return SubmitResult("CONFLICT", cid, order_id=str(r.value.id), order=r.value, mismatches=mm,
                                    broker_order_ids=[str(r.value.id)], detail=f"{why}; identity mismatch", **base)
            return SubmitResult("ACCEPTED", cid, order_id=str(r.value.id), order=r.value,
                                broker_order_ids=[str(r.value.id)], detail=f"{why}; broker has the order", **base)
        return SubmitResult("UNRESOLVED", cid, detail=f"{why}; broker lookup {r.state}", **base)

    def _post_and_record(self, order_request, cid, payload) -> SubmitResult:
        """The single POST for this id. Never raises after the POST has been attempted."""
        store, now_ns = self.store, self.now_ns

        def _done(value, error):
            # Runs in the WORKER when the request really ends (maybe after the caller returned).
            # Bookkeeping only: clears the durable in-flight marker; never changes intent state.
            if error is None and value is not None:
                res = f"ok:{getattr(value, 'id', '?')}"
            else:
                res = f"error:{type(error).__name__}:{_status(error)}"
            store.mark_post_finished(cid, res, now_ns())
        b = self._budget()
        wait = self.submit_deadline_s if b is None else max(0.0, b.remaining() - FINAL_WRITE_RESERVE_S - SCHED_MARGIN_S)
        out = self.runner.run_reserved(lambda: self.client.submit_order(order_request), wait, on_done=_done)
        if out.state == "DEADLINE":
            # The POST may still reach the broker or complete late; its response will be DISCARDED.
            # Never "not sent": UNRESOLVED, reconciled by the same client id.
            _, ok, perr = self._record(cid, "UNRESOLVED", error=f"submit deadline {self.submit_deadline_s}s exceeded; "
                                                                "request not cancelled, may complete late")
            r = self.reconcile(cid)
            r.posts_this_call = 1
            if not ok and r.persisted:
                r.persisted, r.persistence_error = False, perr
            r.detail = f"submit deadline exceeded (late completion possible, result discarded); {r.detail}"
            return r
        try:
            if out.state == "ERROR":
                raise out.error
            order = out.value                                         # exactly one POST (SDK retry=0)
        except Exception as e:  # noqa: BLE001
            s = _status(e)
            row, _ = self._row(cid)
            attempts = (row or {}).get("submit_attempts", 1)
            if s in DEFINITIVE_REJECT_HTTP and attempts == 1:
                applied, ok, perr = self._record(cid, "REJECTED", error=f"{s}:{_code(e)}:{str(e)[:160]}")
                if ok and not applied:
                    return self._from_row(cid, detail="state changed concurrently", posts=1)
                return SubmitResult("REJECTED", cid, detail=f"HTTP {s} on the only POST", posts_this_call=1,
                                    persisted=ok, persistence_error=perr)
            _, ok, perr = self._record(cid, "UNRESOLVED", error=f"{type(e).__name__}:{s}:{str(e)[:160]}")
            r = self.reconcile(cid)
            r.posts_this_call = 1
            if not ok and r.persisted:
                r.persisted, r.persistence_error = False, perr
            return r
        oid = str(order.id)
        mm = identity_mismatches(json.loads(json.dumps(payload, default=str)), order)
        if mm:
            detail = f"POST response identity mismatch: {'; '.join(mm)}"
            applied, ok, perr = self._record(cid, "CONFLICT", order_id=oid, error=detail, conflict_detail=detail)
            return SubmitResult("CONFLICT", cid, order_id=oid, order=order, posts_this_call=1, mismatches=mm,
                                broker_order_ids=[oid], detail=detail, persisted=ok, persistence_error=perr)
        applied, ok, perr = self._record(cid, "ACCEPTED", order_id=oid)
        return SubmitResult("ACCEPTED", cid, order_id=oid, order=order, posts_this_call=1, broker_order_ids=[oid],
                            persisted=ok, persistence_error=perr, evidence="POSITIVE",
                            detail="" if ok else "accepted; result NOT saved (row stays in recovery queue)")

    # ---------------------------------------------------------------- evidence
    def _scan(self, cid, row) -> ScanResult:
        """Bounded, cursor-paginated order listing for POSITIVE evidence only. Truncation, a failed
        page, lack of progress, or the page bound all yield complete=False. Absence is never inferred."""
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        cursor = datetime.fromtimestamp((row["created_ns"] / 1e9) - 60, tz=timezone.utc)
        seen, matches = set(), []
        for page in range(1, self.scan_max_pages + 1):
            cur = cursor
            lst = read(lambda: self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, after=cur, symbols=[self.symbol], limit=self.scan_page_limit,
                direction=Sort.ASC)), **self._rk())
            if lst.state != "OK":
                return ScanResult(matches, False, f"page {page} {lst.state}", page)
            orders = list(lst.value)
            new = [o for o in orders if str(o.id) not in seen]
            for o in new:
                seen.add(str(o.id))
                if getattr(o, "client_order_id", None) == cid:
                    matches.append(o)
            if len(orders) < self.scan_page_limit:
                return ScanResult(matches, True, "end of listing", page)
            stamps = [getattr(o, "created_at", None) or getattr(o, "submitted_at", None) for o in orders]
            stamps = [s for s in stamps if s is not None]
            if not new or not stamps:
                return ScanResult(matches, False, f"no progress at page {page}", page)
            # Step back 1 µs so equal timestamps at a page edge are re-read (deduplicated by id).
            cursor = max(stamps) - timedelta(microseconds=1)
        return ScanResult(matches, False, f"page bound {self.scan_max_pages} reached", self.scan_max_pages)

    def reconcile(self, cid, force_scan: bool = False) -> SubmitResult:
        """Resolve an intent from broker evidence and report the evidence quality.

        Only POSITIVE, identity-matching evidence of exactly one order moves an open intent forward:
        to ACCEPTED if a COMPLETE bounded scan confirms uniqueness, else to ACCEPTED_UNVERIFIED
        (still monitored and locked). Negative evidence never resolves anything. CONFLICT and
        ABANDONED are re-checked (late orders) but never auto-cleared. force_scan=True runs the
        bounded scan even inside the visibility window (human-resolution checks)."""
        row, err = self._row(cid)
        if err:
            return SubmitResult("UNRESOLVED", cid, detail=err, persisted=False, persistence_error=err)
        if row is None:
            raise KeyError(cid)
        if row["state"] in ("ACCEPTED", "REJECTED"):
            return self._from_row(cid, detail="already resolved")
        intent = json.loads(row["payload"])
        r = read(lambda: self.client.get_order_by_client_id(cid), **self._rk())
        candidates, how = {}, []
        if r.state == "OK":
            candidates[str(r.value.id)] = r.value
            how.append("found by client id")
        aged = self.now_ns() - (row["last_submit_ns"] or row["created_ns"]) >= self.window_ns
        scan = None
        if candidates or aged or force_scan or row["state"] == "ACCEPTED_UNVERIFIED":
            scan = self._scan(cid, row)
            for o in scan.matches:
                if str(o.id) not in candidates:
                    candidates[str(o.id)] = o
                    if "found by list" not in how:
                        how.append("found by list")
        if candidates:
            evidence = "POSITIVE"
        elif r.state == "NOT_FOUND" and scan is not None and scan.complete:
            evidence = "NEGATIVE_COMPLETE"
        else:
            evidence = "INCOMPLETE"
        scan_note = f"scan: {scan.reason}, {scan.pages} page(s)" if scan else "scan: not run"
        out = self._reconcile_decide(cid, row, intent, r, candidates, how, scan, scan_note)
        out.evidence = evidence
        return out

    def _reconcile_decide(self, cid, row, intent, r, candidates, how, scan, scan_note) -> SubmitResult:
        ids = sorted(candidates)
        if len(ids) > 1:
            return self._conflict(cid, row, f"{len(ids)} broker orders share this client id: {', '.join(ids)}",
                                  ids=ids)
        if len(ids) == 1:
            o = candidates[ids[0]]
            mm = identity_mismatches(intent, o)
            if mm:
                return self._conflict(cid, row, f"order {ids[0]} identity mismatch: {'; '.join(mm)}", ids=ids, mm=mm)
            if row["state"] == "ABANDONED":
                return self._conflict(cid, row, f"late order {ids[0]} appeared after abandonment", ids=ids)
            if row["state"] == "CONFLICT":
                return self._from_row(cid, detail="conflict persists; human resolution required")
            verified = bool(scan and scan.complete)
            target = "ACCEPTED" if verified else "ACCEPTED_UNVERIFIED"
            note = " + ".join(how) if len(how) == 1 else "; ".join(how)
            if not verified:
                note += f"; uniqueness NOT verified ({scan_note}); remains monitored and entries locked"
            applied, ok, perr = self._record(cid, target, order_id=ids[0],
                                             error=None if verified else note)
            if ok and not applied and row["state"] != target:
                return self._from_row(cid, detail="state changed concurrently")
            return SubmitResult(target, cid, order_id=ids[0], order=o, broker_order_ids=ids, detail=note,
                                uniqueness_verified=verified, persisted=ok, persistence_error=perr)
        # No positive evidence.
        if row["state"] in ("CONFLICT", "ABANDONED", "ACCEPTED_UNVERIFIED"):
            return self._from_row(cid, detail=f"no new broker evidence; still {row['state']} "
                                              f"(lookup {r.state}; {scan_note})")
        why = {"NOT_FOUND": "no broker evidence yet (NOT proof of absence)",
               "UNAVAILABLE": "lookup UNAVAILABLE", "ERROR": f"lookup ERROR: {r.error}"}.get(r.state, r.state)
        why = f"{why}; {scan_note}"
        applied, ok, perr = self._record(cid, "UNRESOLVED", error=why)
        if ok and not applied:
            return self._from_row(cid, detail="resolved concurrently")
        return SubmitResult("UNRESOLVED", cid, order_id=row.get("order_id"), detail=why,
                            uniqueness_verified=False if scan else None, persisted=ok, persistence_error=perr)

    def _conflict(self, cid, row, detail, ids=(), mm=()) -> SubmitResult:
        applied, ok, perr = self._record(cid, "CONFLICT", error=detail, conflict_detail=detail)
        if ok and not applied and row["state"] != "CONFLICT":
            cur = self._from_row(cid, detail="state changed concurrently")
            if cur.state != "UNRESOLVED":
                return cur
        return SubmitResult("CONFLICT", cid, order_id=ids[0] if len(ids) == 1 else None, detail=detail,
                            broker_order_ids=list(ids), mismatches=list(mm), persisted=ok, persistence_error=perr)

    def resubmit(self, order_request) -> SubmitResult:
        """DISABLED. Elapsed time and negative lookups never authorize another POST.
        Verifies the payload, runs a read-only reconcile, and returns its result. No POST."""
        cid = order_request.client_order_id
        row, err = self._row(cid)
        if err:
            return SubmitResult("UNRESOLVED", cid, detail=err + "; automatic resubmission disabled",
                                persisted=False, persistence_error=err)
        if row is None:
            raise KeyError(cid)
        if row["payload_sha"] != self._sha(self._payload(order_request)):
            raise ValueError("resubmit payload differs from the recorded intent")
        r = self.reconcile(cid)
        if not RESUBMISSION_ENABLED:
            r.detail = (r.detail + "; " if r.detail else "") + "automatic resubmission disabled"
        return r

    def recover_pending(self) -> list:
        """At startup and every cycle: re-check every monitored intent (incl. CONFLICT/ABANDONED).
        Never POSTs."""
        try:
            rows = self.store.pending()
        except Exception as e:  # noqa: BLE001
            err = f"recovery queue unreadable: {type(e).__name__}: {e}"
            return [SubmitResult("UNRESOLVED", "*", detail=err, persisted=False, persistence_error=err)]
        return [self.reconcile(row["client_order_id"]) for row in rows]

    # ---------------------------------------------------------------- entry lock + human actions
    def entries_locked(self, symbol: Optional[str] = None):
        """(locked, [client ids]). Fail-closed: an unreadable store locks entries."""
        try:
            ids = self.store.locking_intents(symbol or self.symbol)
        except Exception as e:  # noqa: BLE001
            return True, [f"* store unreadable: {type(e).__name__}"]
        return bool(ids), ids

    def _require_operator(self, operator, note):
        if not operator or not str(operator).strip() or not note or not str(note).strip():
            raise ValueError("operator and note are required for human-resolution actions")

    @staticmethod
    def _negative_check_refusal(fresh: SubmitResult) -> Optional[str]:
        """Human-resolution actions need a SUCCESSFUL negative observation that was also recorded.
        Unavailable, failed, truncated, bounded or stuck evidence is not a negative observation.
        Even NEGATIVE_COMPLETE is only a precondition for an operator decision, not proof of absence."""
        if fresh.evidence != "NEGATIVE_COMPLETE":
            return f"broker evidence {fresh.evidence}, not a complete negative observation"
        if not fresh.persisted:
            return f"fresh check could not be persisted ({fresh.persistence_error})"
        return None

    def acknowledge(self, cid, operator: str, note: str) -> SubmitResult:
        """Operator has SEEN the unresolved intent. Changes no state, releases no lock, stops no
        monitoring. Recorded in history."""
        self._require_operator(operator, note)
        try:
            ok = self.store.operator_action(cid, "ACKNOWLEDGED", operator, note, self.now_ns(),
                                            require_state=PENDING_STATES,
                                            set_cols={"acknowledged_by": operator, "acknowledged_ns": self.now_ns()})
        except PersistenceError as e:
            return SubmitResult("UNRESOLVED", cid, detail="acknowledge not recorded", persisted=False,
                                persistence_error=str(e))
        return self._from_row(cid, detail="acknowledged" if ok else "acknowledge refused (state not monitored)")

    def abandon(self, cid, operator: str, note: str) -> SubmitResult:
        """Operator declares the intent abandoned. Allowed only from UNRESOLVED and only if a FRESH
        reconcile still finds no broker order. Keeps history, keeps monitoring for a late order,
        and does NOT release the entry lock (see release_entry_lock). Never POSTs."""
        self._require_operator(operator, note)
        row, err = self._row(cid)
        if err or row is None or row["state"] not in ("UNRESOLVED", "NOT_FOUND_AFTER_WINDOW"):
            return self._from_row(cid, detail="abandon refused: only UNRESOLVED intents can be abandoned")
        blocked = self._post_outcome_blocks_resolution(cid)
        if blocked:
            return self._from_row(cid, detail=f"abandon refused: {blocked}")
        fresh = self.reconcile(cid, force_scan=True)
        if fresh.state != "UNRESOLVED":
            fresh.detail = f"abandon refused: fresh check returned {fresh.state}; {fresh.detail}"
            return fresh
        blocked = self._post_outcome_blocks_resolution(cid)
        if blocked:
            fresh.detail = f"abandon refused: {blocked}; {fresh.detail}"
            return fresh
        refusal = self._negative_check_refusal(fresh)
        if refusal:
            fresh.detail = f"abandon refused: {refusal}; {fresh.detail}"
            return fresh
        try:
            ok = self.store.operator_action(cid, "ABANDONED", operator, note, self.now_ns(),
                                            require_state=("UNRESOLVED", "NOT_FOUND_AFTER_WINDOW"),
                                            set_cols={"abandoned_by": operator, "abandoned_ns": self.now_ns()})
        except PersistenceError as e:
            return SubmitResult("UNRESOLVED", cid, detail="abandon not recorded", persisted=False,
                                persistence_error=str(e))
        return self._from_row(cid, detail="abandoned by operator; still monitored; entry lock HELD" if ok
                              else "abandon refused (state changed concurrently)")

    def release_entry_lock(self, cid, operator: str, note: str) -> SubmitResult:
        """Separate, explicit operator decision to stop this ABANDONED intent from blocking entries.
        Runs a fresh reconcile first (a late order turns it into CONFLICT and keeps the lock).
        Monitoring continues afterwards; a late order still becomes CONFLICT and re-locks."""
        self._require_operator(operator, note)
        if not self.lock_release_enabled:
            return self._from_row(cid, detail="release refused (lock HELD): operational lock release is DISABLED "
                                              "until the reviewed mechanism and a separately approved policy allow it")
        row, err = self._row(cid)
        if err or row is None or row["state"] != "ABANDONED":
            return self._from_row(cid, detail="release refused: intent is not ABANDONED")
        blocked = self._post_outcome_blocks_resolution(cid)
        if blocked:
            return self._from_row(cid, detail=f"release refused (lock HELD): {blocked}")
        fresh = self.reconcile(cid, force_scan=True)
        if fresh.state != "ABANDONED":
            fresh.detail = f"release refused: fresh check returned {fresh.state}; {fresh.detail}"
            return fresh
        refusal = self._negative_check_refusal(fresh)
        if refusal:
            fresh.detail = f"release refused (lock HELD): {refusal}; {fresh.detail}"
            return fresh
        try:
            ok = self.store.operator_action(cid, "LOCK_RELEASED", operator, note, self.now_ns(),
                                            require_state=("ABANDONED",),
                                            set_cols={"lock_released_by": operator, "lock_released_ns": self.now_ns()})
        except PersistenceError as e:
            return SubmitResult("ABANDONED", cid, detail="release not recorded; lock still held", persisted=False,
                                persistence_error=str(e))
        return self._from_row(cid, detail="entry lock released by operator; monitoring continues" if ok
                              else "release refused (state changed concurrently)")
