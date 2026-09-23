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
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests
from requests.adapters import HTTPAdapter

SUPPORTED_SDK_VERSION = "0.43.5"
DEFAULT_TIMEOUT = (3.05, 10.0)          # (connect, read) seconds — proposal, tune after P-tests
READ_DEADLINE_S = 8.0                   # total budget for one logical read incl. retries
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
@dataclass
class ReadResult:
    state: str                     # OK | NOT_FOUND | UNAVAILABLE | ERROR
    value: Any = None
    attempts: int = 0
    error: Optional[str] = None


def read(fn: Callable[[], Any], deadline_s: float = READ_DEADLINE_S, max_attempts: int = READ_MAX_ATTEMPTS,
         base_backoff_s: float = 0.25, max_backoff_s: float = 2.0,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
         rng: random.Random = random.Random(0)) -> ReadResult:
    """Bounded retries for READ-ONLY calls. Never returns NOT_FOUND for anything but a
    structured 404/40410000; exhausted retries are UNAVAILABLE, never 'absent'."""
    start, attempt, last = clock(), 0, None
    while attempt < max_attempts:
        attempt += 1
        try:
            return ReadResult("OK", fn(), attempt)
        except Exception as e:  # noqa: BLE001
            last = e
            if is_confirmed_not_found(e):
                return ReadResult("NOT_FOUND", None, attempt, str(e)[:200])
            if not is_retryable(e):
                return ReadResult("ERROR", None, attempt, f"{type(e).__name__}: {str(e)[:200]}")
            delay = min(max_backoff_s, base_backoff_s * (2 ** (attempt - 1))) * (0.5 + rng.random() / 2)
            if clock() - start + delay >= deadline_s:
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
"""
# States (round 2):
#   SUBMITTING  intent durable; the POST may or may not have reached the broker
#   ACCEPTED    broker order id known (terminal for this layer)
#   REJECTED    definitive 4xx on the ONLY POST ever made for this id (terminal)
#   UNRESOLVED  outcome unknown; stays in the recovery queue until broker evidence appears
# Allowed transitions (compare-and-set; anything else is refused):
#   SUBMITTING -> ACCEPTED | REJECTED(only if submit_attempts == 1) | UNRESOLVED
#   UNRESOLVED -> ACCEPTED | UNRESOLVED
#   ACCEPTED / REJECTED -> (none)
# Retired: NOT_FOUND_AFTER_WINDOW (absence is never inferred; legacy rows are treated as UNRESOLVED).
PENDING_STATES = ("SUBMITTING", "UNRESOLVED", "NOT_FOUND_AFTER_WINDOW")
_ALLOWED_FROM = {"ACCEPTED": ("SUBMITTING", "UNRESOLVED", "NOT_FOUND_AFTER_WINDOW"),
                 "REJECTED": ("SUBMITTING",),
                 "UNRESOLVED": ("SUBMITTING", "UNRESOLVED", "NOT_FOUND_AFTER_WINDOW")}


class IntentStore:
    def __init__(self, path: str, fail_hook: Optional[Callable[[str], None]] = None):
        self.path, self.fail_hook = path, fail_hook
        with closing(self._c()) as c:
            c.executescript(INTENT_SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")      # durable before we act on it
        return c

    def _write(self, op: str, sql: str, args: tuple) -> int:
        """Returns rowcount. Raises PersistenceError on storage failure;
        sqlite3.IntegrityError is re-raised unchanged (callers treat it as 'already exists')."""
        try:
            if self.fail_hook:
                self.fail_hook(op)                 # test fault injection, handled like a real failure
            with closing(self._c()) as c:
                c.execute("BEGIN IMMEDIATE")
                cur = c.execute(sql, args)
                n = cur.rowcount
                c.execute("COMMIT")
                return n
        except sqlite3.IntegrityError:
            raise
        except Exception as e:  # noqa: BLE001
            raise PersistenceError(f"{op}: {e}") from e

    def get(self, cid: str) -> Optional[dict]:
        with closing(self._c()) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM order_intents WHERE client_order_id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def pending(self) -> list:
        """The recovery queue: every intent whose broker outcome is not yet known."""
        with closing(self._c()) as c:
            c.row_factory = sqlite3.Row
            q = "SELECT * FROM order_intents WHERE state IN (%s)" % ",".join("?" * len(PENDING_STATES))
            return [dict(r) for r in c.execute(q, PENDING_STATES)]

    def create_submitting(self, cid, purpose, symbol, payload, now_ns) -> bool:
        """True if this caller created the intent (and therefore owns the single POST);
        False if the id already exists (another caller owns it). Storage failure raises."""
        pj = json.dumps(payload, sort_keys=True, default=str)
        try:
            self._write("create_submitting",
                        "INSERT INTO order_intents (client_order_id,purpose,symbol,payload,payload_sha,state,"
                        "submit_attempts,created_ns,last_submit_ns,updated_ns) VALUES (?,?,?,?,?,?,1,?,?,?)",
                        (cid, purpose, symbol, pj, hashlib.sha256(pj.encode()).hexdigest(), "SUBMITTING",
                         now_ns, now_ns, now_ns))
            return True
        except sqlite3.IntegrityError:
            return False

    def transition(self, cid, to_state, now_ns, order_id=None, error=None) -> bool:
        """Compare-and-set. True only if the row was in an allowed source state and was updated.
        REJECTED additionally requires submit_attempts == 1 (a single, definitive POST)."""
        src = _ALLOWED_FROM[to_state]
        extra = " AND submit_attempts = 1" if to_state == "REJECTED" else ""
        sql = ("UPDATE order_intents SET state=?, order_id=COALESCE(?,order_id), last_error=?, updated_ns=? "
               "WHERE client_order_id=? AND state IN (%s)%s" % (",".join("?" * len(src)), extra))
        return self._write(f"mark_{to_state}", sql, (to_state, order_id, error, now_ns, cid) + src) == 1

    # Backwards-compatible alias used by older call sites/tests: now also compare-and-set.
    def mark(self, cid, state, now_ns, order_id=None, error=None, bump_attempt=False):
        if bump_attempt:
            raise PersistenceError("re-submission is disabled; attempts cannot be bumped")
        return self.transition(cid, state, now_ns, order_id=order_id, error=error)


def new_client_order_id(prefix: str) -> str:
    """<= 128 chars (documented limit)."""
    cid = f"{prefix}-{uuid.uuid4().hex}"
    assert len(cid) <= 128
    return cid


# ------------------------------------------------------------------ submissions
RESUBMISSION_ENABLED = False   # Round 2 policy: never re-POST an ambiguous intent automatically.


@dataclass
class SubmitResult:
    """Outcome of one gateway call. Callers MUST read `state` AND `persisted`.

    state:
      ACCEPTED      the broker has this order; `order_id` is set.
      REJECTED      the single POST was definitively refused (400/401/403/422 on attempt 1).
      UNRESOLVED    outcome unknown. NOT "not placed". Exposure may exist.
      NOT_SUBMITTED nothing was sent (intent could not be made durable).
      CONFLICT      broker evidence contradicts the stored terminal state; needs a human.
    persisted: False if the result could not be written to the intent store. The intent row then
      stays in the recovery queue, and recover_pending()/reconcile() will record it later.
    """
    state: str
    client_order_id: str
    order_id: Optional[str] = None
    order: Any = None
    detail: str = ""
    posts_this_call: int = 0
    persisted: bool = True
    persistence_error: Optional[str] = None

    @property
    def exposure_may_exist(self) -> bool:
        return self.state in ("ACCEPTED", "UNRESOLVED", "CONFLICT")


class OrderGateway:
    def __init__(self, client, store: IntentStore, symbol: str,
                 now_ns: Callable[[], int] = time.time_ns, read_kw: Optional[dict] = None,
                 visibility_window_s: float = VISIBILITY_WINDOW_S):
        if not getattr(client, "_broker_io_configured", False):
            raise ConfigurationError("client must be passed through configure_client() first")
        self.client, self.store, self.symbol = client, store, symbol
        self.now_ns, self.read_kw = now_ns, (read_kw or {})
        self.window_ns = int(visibility_window_s * 1e9)

    @staticmethod
    def _payload(order_request) -> dict:
        return order_request.to_request_fields()

    @staticmethod
    def _sha(payload) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _row(self, cid):
        """(row, error). Never raises: a store read failure is reported, not thrown."""
        try:
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
        return SubmitResult(st, cid, order_id=row["order_id"] if row else None, detail=detail,
                            posts_this_call=posts, **kw)

    def _record(self, cid, to_state, order_id=None, error=None):
        """(applied, persisted, persistence_error). Never raises."""
        try:
            return self.store.transition(cid, to_state, self.now_ns(), order_id=order_id, error=error), True, None
        except PersistenceError as e:
            return False, False, str(e)

    def submit(self, order_request, purpose: str) -> SubmitResult:
        cid = getattr(order_request, "client_order_id", None)
        if not cid:
            raise ValueError("every order must carry a client_order_id (use new_client_order_id)")
        payload = self._payload(order_request)
        existing, err = self._row(cid)
        if err:
            return SubmitResult("NOT_SUBMITTED", cid, detail="intent store unreadable; nothing sent",
                                persisted=False, persistence_error=err)
        if existing is not None:
            if existing["payload_sha"] != self._sha(payload):
                raise ValueError(f"client_order_id {cid} already used with a different payload")
            return self._from_row(cid, detail="existing intent; no POST")
        try:
            created = self.store.create_submitting(cid, purpose, self.symbol, payload, self.now_ns())
        except PersistenceError as e:
            return SubmitResult("NOT_SUBMITTED", cid, detail="intent not durable; nothing sent",
                                persisted=False, persistence_error=str(e))
        if not created:                             # another caller won the insert and owns the POST
            return self._from_row(cid, detail="intent owned by another caller; no POST")
        return self._post_and_record(order_request, cid)

    def _post_and_record(self, order_request, cid) -> SubmitResult:
        """The single POST for this id. Never raises after the POST has been attempted."""
        try:
            order = self.client.submit_order(order_request)          # exactly one POST (SDK retry=0)
        except Exception as e:  # noqa: BLE001
            s = _status(e)
            row, _ = self._row(cid)
            # This caller created the row and resubmission is disabled, so if the row can't be
            # read, this POST is still known to be the only one for the id.
            attempts = (row or {}).get("submit_attempts", 1)
            if s in DEFINITIVE_REJECT_HTTP and attempts == 1:
                applied, ok, perr = self._record(cid, "REJECTED", error=f"{s}:{_code(e)}:{str(e)[:160]}")
                if ok and not applied:
                    return self._from_row(cid, detail="state changed concurrently", posts=1)
                return SubmitResult("REJECTED", cid, detail=f"HTTP {s} on the only POST", posts_this_call=1,
                                    persisted=ok, persistence_error=perr)
            # Ambiguous (429/5xx/timeout/connection/non-JSON, or a 4xx that is not provably first).
            _, ok, perr = self._record(cid, "UNRESOLVED", error=f"{type(e).__name__}:{s}:{str(e)[:160]}")
            r = self.reconcile(cid)
            r.posts_this_call = 1
            if not ok and r.persisted:              # the first write failed; report it even if later ones worked
                r.persisted, r.persistence_error = False, perr
            return r
        oid = str(order.id)
        applied, ok, perr = self._record(cid, "ACCEPTED", order_id=oid)
        # Known identity is returned even if it could not be saved.
        return SubmitResult("ACCEPTED", cid, order_id=oid, order=order, posts_this_call=1,
                            persisted=ok, persistence_error=perr,
                            detail="" if ok else "accepted; result NOT saved (row stays in recovery queue)")

    def reconcile(self, cid) -> SubmitResult:
        """Resolve an intent from broker evidence. Positive evidence (lookup or list hit) may
        move it to ACCEPTED. Negative evidence NEVER resolves it: it stays UNRESOLVED and queued.
        Never raises for storage failures; never downgrades a terminal state."""
        row, err = self._row(cid)
        if err:
            return SubmitResult("UNRESOLVED", cid, detail=err, persisted=False, persistence_error=err)
        if row is None:
            raise KeyError(cid)
        if row["state"] in ("ACCEPTED", "REJECTED"):
            return self._from_row(cid, detail="already resolved")
        r = read(lambda: self.client.get_order_by_client_id(cid), **self.read_kw)
        found = r.value if r.state == "OK" else None
        how = "found by client id"
        if found is None and r.state == "NOT_FOUND" and \
                self.now_ns() - (row["last_submit_ns"] or row["created_ns"]) >= self.window_ns:
            # Supplementary POSITIVE evidence only; an incomplete/lagging list proves nothing.
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            after = datetime.fromtimestamp((row["created_ns"] / 1e9) - 60, tz=timezone.utc)
            lst = read(lambda: self.client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, after=after, symbols=[self.symbol], limit=500)), **self.read_kw)
            if lst.state == "OK":
                hit = [o for o in lst.value if getattr(o, "client_order_id", None) == cid]
                if hit:
                    found, how = hit[0], "found by list"
        if found is not None:
            oid = str(found.id)
            applied, ok, perr = self._record(cid, "ACCEPTED", order_id=oid)
            if ok and not applied:
                cur = self._from_row(cid, detail="state changed concurrently")
                if cur.state == "ACCEPTED":
                    return cur
                return SubmitResult("CONFLICT", cid, order_id=oid, order=found,
                                    detail=f"broker has order {oid} but intent is {cur.state}")
            return SubmitResult("ACCEPTED", cid, order_id=oid, order=found, detail=how,
                                persisted=ok, persistence_error=perr)
        why = {"NOT_FOUND": "no broker evidence yet (NOT proof of absence)",
               "UNAVAILABLE": "lookup UNAVAILABLE", "ERROR": f"lookup ERROR: {r.error}"}.get(r.state, r.state)
        applied, ok, perr = self._record(cid, "UNRESOLVED", error=why)
        if ok and not applied:                      # a concurrent caller resolved it: report that, don't downgrade
            return self._from_row(cid, detail="resolved concurrently")
        return SubmitResult("UNRESOLVED", cid, order_id=row.get("order_id"), detail=why,
                            persisted=ok, persistence_error=perr)

    def resubmit(self, order_request) -> SubmitResult:
        """DISABLED (round 2). Elapsed time and negative lookups never authorize another POST.
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
        """On startup and every cycle: reconcile each queued intent. Never POSTs."""
        try:
            rows = self.store.pending()
        except Exception as e:  # noqa: BLE001
            err = f"recovery queue unreadable: {type(e).__name__}: {e}"
            return [SubmitResult("UNRESOLVED", "*", detail=err, persisted=False, persistence_error=err)]
        return [self.reconcile(row["client_order_id"]) for row in rows]
