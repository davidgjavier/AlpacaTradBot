# P8 — alpaca-py retry / timeout behavior (offline evidence)

- **Run:** 2026-09-23, `/usr/bin/python3 review/p8_sdk_transport/p8_sdk_transport.py`
- **Versions:** alpaca-py **0.43.5**, requests 2.32.5, Python 3.9.
- **Method:** the installed SDK is used unmodified. Only the `requests` transport is replaced, by an
  adapter mounted on the client's session that acts as a simulated broker.
- **Network:** all non-loopback connections were blocked. Verified: a probe to 93.184.216.34:443 was
  blocked, and it was the only blocked attempt.
- **Timing:** retry sleeps were recorded rather than slept (patched in this test process only).
- **Isolation:** no broker, no credentials, and no changes to the running bots or their SDK install.
- **Artifacts:** `p8_sdk_transport.py` (sha256 prefix `3a59ae64dd180c77`) and `p8_results.json`
  (`1a813286f9014e3d`).

## Observed SDK behavior

| Scenario | POSTs | Retry waits | Outcome to caller | Orders at simulated broker | Recovery by client id |
|---|---|---|---|---|---|
| S1 HTTP 429 every attempt | 4 | 3,3,3 s | `APIError` 429 / 42910000 | 0 | 404 (none) |
| S2 HTTP 504 every attempt (retry exhaustion) | 4 | 3,3,3 s | `APIError` 504 / 50410000 | 0 | 404 (none) |
| S3 504, 504, then 200 | 3 | 3,3 s | `Order` returned | 1 | found |
| S4a accepted, response lost (504); **broker rejects duplicate client id — HYPOTHETICAL** | 2 | 3 s | **`APIError` 422 although the order exists** | 1 | found, 0 extra POSTs |
| S4b accepted, response lost (504); **broker accepts duplicate client id — HYPOTHETICAL** | 4 | 3,3,3 s | `APIError` 504 | **4 orders, 1 client id** | finds only one of them |
| S4c accepted, response lost (504); **no client id sent** | 4 | 3,3,3 s | `APIError` 504 | **4 orders, 4 server ids** | impossible |
| S4d accepted, then 429; duplicate rejected (HYPOTHETICAL) | 2 | 3 s | `APIError` 422 | 1 | found |
| S5 connect timeout (`requests.ConnectTimeout`) | 1 | none | `ConnectTimeout` (not `APIError`) | 0 | 404 |
| S6 read timeout after acceptance | 1 | none | `ReadTimeout` (not `APIError`) | 1 | found |
| S6b connection reset after acceptance | 1 | none | `ConnectionError` | 1 | found |
| S7 504 with HTML (non-JSON) body | 4 | 3,3,3 s | `APIError` 504; **`.code` raises `JSONDecodeError`** | 0 | 404 |
| S8 GET by client id, 504 every attempt | 4 GETs | 3,3,3 s | `APIError` 504 | n/a | n/a |
| S10 **real loopback socket, server never responds** | 1 | none | **blocked 8.0 s**, until the server closed the socket; then `ConnectionError (RemoteDisconnected)` | n/a | n/a |

**Conclusions:**
- **Payload across retries:** byte-identical for every scenario, including the same `client_order_id`.
- **Timeout:** the transport received `timeout=None` in every call. The SDK sets no request timeout.
- **Configurability (S9):**
  - `TradingClient.__init__` rejects `retry_attempts` (`TypeError`), so retries can't be configured
    through the public client.
  - On `RESTClient`, `retry_attempts=0` and `retry_exception_codes=[]` are **ignored** (both falsy), and
    the defaults of 3 retries on 429/504 remain. `retry_attempts=1` works.

## What this means (observation vs. hypothesis)

- **Verified (SDK):**
  - "Four POSTs" holds **only for 429/504 status responses**.
  - Transport-level failures (connect timeout, read timeout, reset) are raised once, without retry, as
    `requests` exceptions rather than `APIError`.
  - A request with no response blocks until the peer closes the connection. S10 bounded this at 8 s
    only because the test server closed it; nothing in the SDK bounds it.
- **Hypothesis still to observe on paper (P3):** what Alpaca actually does with a repeated
  `client_order_id`. S4a and S4b bracket the two possibilities:
  - If Alpaca rejects duplicates, a successful submission can be reported to the caller as a **422
    failure**.
  - If Alpaca accepts duplicates, a single call can create **up to 4 orders**.
- **Bot-side implications (not changed here; tracked for the owners of that code):**
  - Code paths that submit **without** a client id (`place_buy`, `_submit_stop_limit_sell`,
    `place_market_sell`, emergency and flatten paths) can create up to 4 orders from one call if the
    broker accepted each attempt but every response returned 504. For entry buys that is up to 4× the
    intended notional.
  - Any path that treats an exception as "not placed" is wrong for S4a, S4d, S6 and S6b.
  - A stalled socket freezes the whole loop, including protection checks.
  - 504 retries alone add about 9 s per call; a cycle that makes several calls during an incident can
    take much longer.
  - `APIError.code` can raise on a non-JSON body (S7). Any code that reads `.code` must guard it; the
    `94b2efd` `_is_not_found` already does.
- **Not tested here:** real Alpaca latency, whether a 504 from Alpaca implies acceptance, how quickly an
  order becomes visible by client id, and TLS/proxy behavior.
