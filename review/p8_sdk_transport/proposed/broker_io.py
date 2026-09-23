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
  3. OrderGateway.submit(): exactly one POST per client_order_id attempt. Durable intent row
     (client id + payload) is committed BEFORE the POST; if that write fails nothing is sent.
     Ambiguous outcomes (5xx, 429, timeouts, connection errors, non-JSON) become UNRESOLVED
     and are reconciled by the SAME client id. No automatic resubmission.
  4. OrderGateway.reconcile(): lookup by client id; after a visibility window, an absence
     confirmed by BOTH the lookup and an order-list query becomes NOT_FOUND_AFTER_WINDOW.
     Only then may resubmit() POST again, with the same client id and payload.

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
# States: SUBMITTING (intent durable, POST may or may not have reached the broker)
#         ACCEPTED (broker order id known) | REJECTED (definitive 4xx on POST)
#         UNRESOLVED (ambiguous; reconcile by client id)
#         NOT_FOUND_AFTER_WINDOW (absent by lookup AND list after the window; resubmit allowed)


class IntentStore:
    def __init__(self, path: str, fail_hook: Optional[Callable[[str], None]] = None):
        self.path, self.fail_hook = path, fail_hook
        with self._c() as c:
            c.executescript(INTENT_SCHEMA)

    def _c(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")      # durable before we act on it
        return c

    def _write(self, op: str, sql: str, args: tuple):
        try:
            if self.fail_hook:
                self.fail_hook(op)                 # test fault injection, handled like a real failure
            with self._c() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(sql, args)
                c.execute("COMMIT")
        except Exception as e:  # noqa: BLE001
            raise PersistenceError(f"{op}: {e}") from e

    def get(self, cid: str) -> Optional[dict]:
        with self._c() as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM order_intents WHERE client_order_id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def pending(self) -> list:
        with self._c() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(
                "SELECT * FROM order_intents WHERE state IN ('SUBMITTING','UNRESOLVED')")]

    def create_submitting(self, cid, purpose, symbol, payload, now_ns):
        pj = json.dumps(payload, sort_keys=True, default=str)
        self._write("create_submitting",
                    "INSERT INTO order_intents (client_order_id,purpose,symbol,payload,payload_sha,state,"
                    "submit_attempts,created_ns,last_submit_ns,updated_ns) VALUES (?,?,?,?,?,?,1,?,?,?)",
                    (cid, purpose, symbol, pj, hashlib.sha256(pj.encode()).hexdigest(), "SUBMITTING",
                     now_ns, now_ns, now_ns))

    def mark(self, cid, state, now_ns, order_id=None, error=None, bump_attempt=False):
        self._write(f"mark_{state}",
                    "UPDATE order_intents SET state=?, order_id=COALESCE(?,order_id), last_error=?, "
                    "updated_ns=?, submit_attempts=submit_attempts+?, "
                    "last_submit_ns=CASE WHEN ? THEN ? ELSE last_submit_ns END WHERE client_order_id=?",
                    (state, order_id, error, now_ns, 1 if bump_attempt else 0,
                     1 if bump_attempt else 0, now_ns, cid))


def new_client_order_id(prefix: str) -> str:
    """<= 128 chars (documented limit)."""
    cid = f"{prefix}-{uuid.uuid4().hex}"
    assert len(cid) <= 128
    return cid


# ------------------------------------------------------------------ submissions
@dataclass
class SubmitResult:
    state: str
    client_order_id: str
    order: Any = None
    detail: str = ""
    posts_this_call: int = 0


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

    def submit(self, order_request, purpose: str) -> SubmitResult:
        cid = getattr(order_request, "client_order_id", None)
        if not cid:
            raise ValueError("every order must carry a client_order_id (use new_client_order_id)")
        existing = self.store.get(cid)
        if existing is not None:
            if existing["payload_sha"] != hashlib.sha256(
                    json.dumps(self._payload(order_request), sort_keys=True, default=str).encode()).hexdigest():
                raise ValueError(f"client_order_id {cid} already used with a different payload")
            # Idempotent: never POST again from submit(); use reconcile()/resubmit().
            return SubmitResult(existing["state"], cid, detail="existing intent; no POST")
        # 1) durable intent BEFORE the POST (raises PersistenceError -> nothing sent)
        self.store.create_submitting(cid, purpose, self.symbol, self._payload(order_request), self.now_ns())
        return self._post_and_record(order_request, cid)

    def _post_and_record(self, order_request, cid) -> SubmitResult:
        try:
            order = self.client.submit_order(order_request)          # exactly one POST (SDK retry=0)
        except Exception as e:  # noqa: BLE001
            s = _status(e)
            if s in DEFINITIVE_REJECT_HTTP:
                self.store.mark(cid, "REJECTED", self.now_ns(), error=f"{s}:{_code(e)}:{str(e)[:160]}")
                return SubmitResult("REJECTED", cid, detail=f"HTTP {s}", posts_this_call=1)
            # ambiguous: 429/5xx/timeout/connection/non-JSON — reconcile by the SAME id, never re-POST
            self.store.mark(cid, "UNRESOLVED", self.now_ns(), error=f"{type(e).__name__}:{s}:{str(e)[:160]}")
            r = self.reconcile(cid)
            r.posts_this_call = 1
            return r
        # POST succeeded; recording may still fail -> row stays SUBMITTING and reconcile() fixes it
        self.store.mark(cid, "ACCEPTED", self.now_ns(), order_id=str(order.id))
        return SubmitResult("ACCEPTED", cid, order=order, posts_this_call=1)

    def reconcile(self, cid) -> SubmitResult:
        row = self.store.get(cid)
        if row is None:
            raise KeyError(cid)
        if row["state"] in ("ACCEPTED", "REJECTED"):
            return SubmitResult(row["state"], cid, detail="already resolved")
        r = read(lambda: self.client.get_order_by_client_id(cid), **self.read_kw)
        if r.state == "OK":
            self.store.mark(cid, "ACCEPTED", self.now_ns(), order_id=str(r.value.id))
            return SubmitResult("ACCEPTED", cid, order=r.value, detail="found by client id")
        if r.state != "NOT_FOUND":
            self._mark_unresolved(row, cid, f"lookup {r.state}: {r.error}")
            return SubmitResult("UNRESOLVED", cid, detail=f"lookup {r.state}")
        # Lookup says not found. Only after the visibility window AND an independent list query.
        if self.now_ns() - (row["last_submit_ns"] or row["created_ns"]) < self.window_ns:
            self._mark_unresolved(row, cid, "not found yet (inside visibility window)")
            return SubmitResult("UNRESOLVED", cid, detail="not found; inside window")
        after = datetime.fromtimestamp((row["created_ns"] / 1e9) - 60, tz=timezone.utc)
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        lst = read(lambda: self.client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=after, symbols=[self.symbol], limit=500)), **self.read_kw)
        if lst.state != "OK":
            self._mark_unresolved(row, cid, f"list {lst.state}")
            return SubmitResult("UNRESOLVED", cid, detail=f"list {lst.state}")
        hit = [o for o in lst.value if getattr(o, "client_order_id", None) == cid]
        if hit:
            self.store.mark(cid, "ACCEPTED", self.now_ns(), order_id=str(hit[0].id))
            return SubmitResult("ACCEPTED", cid, order=hit[0], detail="found by list")
        self.store.mark(cid, "NOT_FOUND_AFTER_WINDOW", self.now_ns())
        return SubmitResult("NOT_FOUND_AFTER_WINDOW", cid,
                            detail="absent by lookup and list after window (inference, not a guarantee)")

    def _mark_unresolved(self, row, cid, err):
        if row["state"] != "UNRESOLVED" or row.get("last_error") != err:
            self.store.mark(cid, "UNRESOLVED", self.now_ns(), error=err)

    def resubmit(self, order_request) -> SubmitResult:
        """Explicit, caller-initiated. Reconciles FIRST with the same identity; POSTs again only
        from NOT_FOUND_AFTER_WINDOW, with the same client id and identical payload."""
        cid = order_request.client_order_id
        row = self.store.get(cid)
        if row is None:
            raise KeyError(cid)
        if row["payload_sha"] != hashlib.sha256(
                json.dumps(self._payload(order_request), sort_keys=True, default=str).encode()).hexdigest():
            raise ValueError("resubmit payload differs from the recorded intent")
        r = self.reconcile(cid)
        if r.state != "NOT_FOUND_AFTER_WINDOW":
            return r                                      # found, rejected, or still unknown: no POST
        self.store.mark(cid, "SUBMITTING", self.now_ns(), bump_attempt=True)
        return self._post_and_record(order_request, cid)

    def recover_pending(self) -> list:
        """On startup: every SUBMITTING/UNRESOLVED intent is reconciled (never re-POSTed)."""
        return [self.reconcile(row["client_order_id"]) for row in self.store.pending()]
