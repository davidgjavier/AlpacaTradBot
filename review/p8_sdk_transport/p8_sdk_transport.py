"""P8 — offline test of alpaca-py's REAL retry/timeout behavior (no broker, no network).

Uses the INSTALLED SDK (TradingClient -> RESTClient -> requests.Session) unmodified.
Only the transport under requests is replaced (a mounted adapter = simulated broker).
Non-loopback network access is blocked for the whole process and verified.
Retry sleeps are recorded instead of slept (rest.time.sleep patched in THIS process only).

Hypotheses are recorded as observations; nothing here asserts what Alpaca's real
servers do. Simulated broker duplicate-client_order_id policies are labelled HYPOTHETICAL.
Run: /usr/bin/python3 review/p8_sdk_transport/p8_sdk_transport.py
"""
import json
import socket
import threading
import time
import uuid
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------- network block
_real_connect = socket.socket.connect
_real_create = socket.create_connection
BLOCKED = []


def _is_loopback(addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    return host in ("127.0.0.1", "::1", "localhost")


def _guard_connect(self, addr):
    if not _is_loopback(addr):
        BLOCKED.append(str(addr))
        raise RuntimeError(f"P8: network blocked ({addr})")
    return _real_connect(self, addr)


def _guard_create(addr, *a, **k):
    if not _is_loopback(addr):
        BLOCKED.append(str(addr))
        raise RuntimeError(f"P8: network blocked ({addr})")
    return _real_create(addr, *a, **k)


socket.socket.connect = _guard_connect
socket.create_connection = _guard_create

import requests  # noqa: E402
from requests.adapters import BaseAdapter  # noqa: E402

import alpaca  # noqa: E402
from alpaca.common import rest  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402
from alpaca.trading.client import TradingClient  # noqa: E402
from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: E402
from alpaca.trading.requests import LimitOrderRequest  # noqa: E402

SLEEPS = []
rest.time.sleep = lambda s: SLEEPS.append(s)   # record retry waits (this process only)

RESULTS = {"generated": datetime.now(timezone.utc).isoformat(), "alpaca_py": alpaca.__version__,
           "requests": requests.__version__,
           "sdk_defaults": {"retry_attempts": rest.DEFAULT_RETRY_ATTEMPTS,
                            "retry_codes": rest.DEFAULT_RETRY_EXCEPTION_CODES,
                            "retry_wait_s": rest.DEFAULT_RETRY_WAIT_SECONDS},
           "scenarios": {}}


# ---------------------------------------------------------------- simulated broker transport
def order_json(oid, cid, body):
    return {"id": oid, "client_order_id": cid, "created_at": "2026-09-23T14:00:00Z",
            "updated_at": "2026-09-23T14:00:00Z", "submitted_at": "2026-09-23T14:00:00Z",
            "filled_at": None, "expired_at": None, "canceled_at": None, "failed_at": None,
            "replaced_at": None, "replaced_by": None, "replaces": None,
            "asset_id": "276e2673-764b-4ab6-a611-caf665ca6340", "symbol": body.get("symbol", "BTC/USD"),
            "asset_class": "crypto", "notional": None, "qty": str(body.get("qty")), "filled_qty": "0",
            "filled_avg_price": None, "order_class": "simple", "order_type": body.get("type"),
            "type": body.get("type"), "side": body.get("side"), "time_in_force": body.get("time_in_force"),
            "limit_price": body.get("limit_price"), "stop_price": None, "status": "new",
            "extended_hours": False, "legs": None, "trail_percent": None, "trail_price": None,
            "hwm": None, "position_intent": None}


class SimBroker(BaseAdapter):
    """script: list of per-call behaviors for POST /orders; GET by client id answered from state.
      ("status", code, text)       respond with status + body; order NOT created
      ("accept",)                  create order, respond 200
      ("accept_then", code, text)  create order (broker accepted), then respond error (response lost)
      ("dup_policy_then", code, t) same as accept_then, but a repeated client id hits dup_policy first
      ("raise", exc)               transport raises; order NOT created
      ("accept_then_raise", exc)   order created, then transport raises (response lost)
    dup_policy: 'reject_422' | 'accept' — HYPOTHETICAL handling of a repeated client_order_id.
    """

    def __init__(self, script, dup_policy="reject_422"):
        super().__init__()
        self.script, self.dup_policy = list(script), dup_policy
        self.calls, self.orders = [], []

    def _resp(self, request, code, text):
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = code, text.encode(), request.url, request
        r.headers["Content-Type"] = "application/json"
        return r

    def _create(self, body):
        cid = body.get("client_order_id") or str(uuid.uuid4())   # server-generated if absent
        if body.get("client_order_id") and any(o["client_order_id"] == cid for o in self.orders):
            if self.dup_policy == "reject_422":
                return None, (422, json.dumps({"code": 40010001,
                                               "message": "client_order_id must be unique (HYPOTHETICAL)"}))
        o = order_json(str(uuid.uuid4()), cid, body)
        self.orders.append(o)
        return o, None

    def send(self, request, **kwargs):
        body = json.loads(request.body) if request.body else None
        self.calls.append({"method": request.method, "url": request.url.split("?")[0].split("/v2")[-1],
                           "body": request.body.decode() if isinstance(request.body, bytes) else request.body,
                           "timeout_kwarg": repr(kwargs.get("timeout"))})
        if request.method == "GET" and "by_client_order_id" in request.url:
            cid = requests.utils.urlparse(request.url).query.split("client_order_id=")[-1]
            hits = [o for o in self.orders if o["client_order_id"] == cid]
            if not hits:
                return self._resp(request, 404, json.dumps({"code": 40410000, "message": "order not found"}))
            return self._resp(request, 200, json.dumps(hits[0]))
        beh = self.script.pop(0) if self.script else ("accept",)
        kind = beh[0]
        if kind == "status":
            return self._resp(request, beh[1], beh[2])
        if kind == "raise":
            raise beh[1]
        o, dup = self._create(body)
        if dup:
            return self._resp(request, *dup)
        if kind == "accept":
            return self._resp(request, 200, json.dumps(o))
        if kind in ("accept_then", "dup_policy_then"):
            return self._resp(request, beh[1], beh[2])
        if kind == "accept_then_raise":
            raise beh[1]
        raise AssertionError(kind)

    def close(self):
        pass


def client(broker):
    c = TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real", paper=True)
    c._session.mount("https://", broker)
    c._session.mount("http://", broker)
    return c


def order_req(cid="p8-cid-0001"):
    kw = dict(symbol="BTC/USD", qty=0.0002, side=OrderSide.SELL, time_in_force=TimeInForce.IOC, limit_price=100000)
    if cid:
        kw["client_order_id"] = cid
    return LimitOrderRequest(**kw)


def run(name, script, dup_policy="reject_422", cid="p8-cid-0001", recover=True):
    SLEEPS.clear()
    b = SimBroker(script, dup_policy)
    c = client(b)
    t0 = time.time()
    try:
        o = c.submit_order(order_req(cid))
        outcome = {"returned": "Order", "order_id": str(o.id), "client_order_id": o.client_order_id}
    except APIError as e:
        try:
            code = e.code
        except Exception as ce:  # noqa: BLE001
            code = f"<.code raised {type(ce).__name__}>"
        outcome = {"raised": "APIError", "status_code": e.status_code, "code": code, "text": str(e)[:120]}
    except Exception as e:  # noqa: BLE001
        outcome = {"raised": type(e).__name__, "text": str(e)[:160]}
    posts = [x for x in b.calls if x["method"] == "POST"]
    bodies = {x["body"] for x in posts}
    rec = {"posts": len(posts), "retry_sleeps_s": list(SLEEPS),
           "real_elapsed_s_excluding_sleeps": round(time.time() - t0, 3),
           "would_sleep_total_s": sum(SLEEPS),
           "identical_payload_all_attempts": len(bodies) <= 1,
           "client_order_id_in_payload": [json.loads(x["body"]).get("client_order_id") for x in posts],
           "timeout_kwarg_seen_by_transport": sorted({x["timeout_kwarg"] for x in b.calls}),
           "sdk_outcome": outcome,
           "broker_orders_created": len(b.orders),
           "broker_distinct_client_ids": len({o["client_order_id"] for o in b.orders}),
           "dup_policy": dup_policy + " (HYPOTHETICAL)"}
    if recover and cid:
        n_before = len([x for x in b.calls if x["method"] == "POST"])
        try:
            o = c.get_order_by_client_id(cid)
            rec["recovery_by_client_id"] = {"found": True, "order_id": str(o.id),
                                            "new_posts_during_recovery":
                                                len([x for x in b.calls if x["method"] == "POST"]) - n_before}
        except APIError as e:
            rec["recovery_by_client_id"] = {"found": False, "status_code": e.status_code, "code": e.code}
    RESULTS["scenarios"][name] = rec
    return rec


J504 = json.dumps({"code": 50410000, "message": "request timed out"})
J429 = json.dumps({"code": 42910000, "message": "rate limit exceeded"})
HTML504 = "<html><body>504 Gateway Time-out</body></html>"

# ---------------------------------------------------------------- scenarios
try:
    socket.create_connection(("93.184.216.34", 443), timeout=2)
    RESULTS["network_block_verified"] = False
except RuntimeError:
    RESULTS["network_block_verified"] = True

run("S1_429_always", [("status", 429, J429)] * 10)
run("S2_504_always_retry_exhaustion", [("status", 504, J504)] * 10)
run("S3_504_504_then_200", [("status", 504, J504), ("status", 504, J504), ("accept",)])
run("S4a_accepted_response_lost_504_dup_rejected",
    [("accept_then", 504, J504)] + [("dup_policy_then", 504, J504)] * 5, dup_policy="reject_422")
run("S4b_accepted_response_lost_504_dup_accepted", [("accept_then", 504, J504)] * 5, dup_policy="accept")
run("S4c_NO_client_id_accepted_response_lost_504", [("accept_then", 504, J504)] * 5, dup_policy="accept", cid=None)
run("S4d_accepted_then_429_dup_rejected", [("accept_then", 429, J429)] + [("dup_policy_then", 429, J429)] * 5)
run("S5_connect_timeout", [("raise", requests.exceptions.ConnectTimeout("simulated connect timeout"))] * 5)
run("S6_read_timeout_after_accept",
    [("accept_then_raise", requests.exceptions.ReadTimeout("simulated read timeout"))] * 5)
run("S6b_connection_reset_after_accept",
    [("accept_then_raise", requests.exceptions.ConnectionError("simulated reset"))] * 5)
run("S7_504_non_json_body", [("status", 504, HTML504)] * 10)

# S8: GET lookup on 504 (recovery-path latency)
SLEEPS.clear()
b8 = SimBroker([])
c8 = client(b8)
_orig_send = b8.send
g = {"n": 0}


def flaky_get(request, **kw):
    if request.method == "GET":
        g["n"] += 1
        return b8._resp(request, 504, J504)
    return _orig_send(request, **kw)


b8.send = flaky_get
try:
    c8.get_order_by_client_id("p8-cid-0001")
    out8 = "returned"
except APIError as e:
    out8 = {"status_code": e.status_code, "code": e.code}
RESULTS["scenarios"]["S8_GET_lookup_504_always"] = {"gets": g["n"], "retry_sleeps_s": list(SLEEPS),
                                                   "would_sleep_total_s": sum(SLEEPS), "sdk_outcome": out8}

# S9: configurability via public constructor and RESTClient
tc = TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real", paper=True)
cfg = {"TradingClient_effective": {"retry": tc._retry, "retry_wait": tc._retry_wait, "retry_codes": tc._retry_codes}}
try:
    TradingClient("PKP8TESTONLY000000000", "x", paper=True, retry_attempts=0)  # type: ignore[call-arg]
    cfg["TradingClient_accepts_retry_kwargs"] = True
except TypeError as e:
    cfg["TradingClient_accepts_retry_kwargs"] = False
    cfg["TradingClient_retry_kwarg_error"] = str(e)[:100]
for label, kw in {"retry_attempts=0": {"retry_attempts": 0},
                  "retry_exception_codes=[]": {"retry_exception_codes": []},
                  "retry_attempts=1": {"retry_attempts": 1}}.items():
    r = rest.RESTClient(base_url="https://paper-api.alpaca.markets", api_key="PKP8TESTONLY000000000",
                        secret_key="x", **kw)
    cfg[f"RESTClient({label})"] = {"retry": r._retry, "retry_codes": r._retry_codes}
RESULTS["scenarios"]["S9_configurability"] = cfg


# S10: REAL socket to a loopback server that accepts and never responds
def silent_server(ready, stop, port_box):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_box.append(srv.getsockname()[1])
    ready.set()
    conn, _ = srv.accept()
    conn.recv(65536)            # read the request, never answer
    stop.wait(30)
    conn.close()                # then drop the connection
    srv.close()


ready, stop, port_box = threading.Event(), threading.Event(), []
threading.Thread(target=silent_server, args=(ready, stop, port_box), daemon=True).start()
ready.wait(5)
hc = TradingClient("PKP8TESTONLY000000000", "p8-test-secret-not-real",
                   url_override=f"http://127.0.0.1:{port_box[0]}")
res10 = {}


def do_call():
    t = time.time()
    try:
        hc.submit_order(order_req("p8-hang-0001"))
        res10["outcome"] = "returned"
    except Exception as e:  # noqa: BLE001
        res10["outcome"] = f"{type(e).__name__}: {str(e)[:120]}"
    res10["returned_after_s"] = round(time.time() - t, 2)


th = threading.Thread(target=do_call, daemon=True)
th.start()
th.join(8.0)
res10["still_blocked_after_8s"] = th.is_alive()
stop.set()                      # server now closes the socket
th.join(10.0)
res10["unblocked_only_after_server_closed"] = not th.is_alive()
RESULTS["scenarios"]["S10_real_socket_no_response_no_timeout"] = res10

RESULTS["network_attempts_blocked"] = BLOCKED
out = "/Users/davidj/AlpacaTradeBot/review/p8_sdk_transport/p8_results.json"
with open(out, "w") as fh:
    json.dump(RESULTS, fh, indent=2, default=str)
print(json.dumps(RESULTS, indent=1, default=str))
