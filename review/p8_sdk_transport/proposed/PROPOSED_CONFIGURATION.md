# Proposed SDK configuration for the crypto bot (P8 follow-up) — PROPOSAL ONLY

- **Status:** nothing here is applied to any trading file, running service, or installed package.
- **Code:** `broker_io.py` (wrapper) and `test_broker_io.py` (30 offline tests, all passing; results in
  `test_results.txt`).
- **Diff:** `stage_A_timeouts.diff`, generated against a scratch copy of `crypto_trading_bot.py` at
  `94b2efd`. Not applied.
- **Ownership:** Codex owns the Target-1 accounting fix in the same file. Stages B and C change shared
  caller code and must be sequenced with that work.

## What alpaca-py 0.43.5 supports

| Need | SDK support | Consequence |
|---|---|---|
| Request timeouts | **None.** `_session.request()` is called without `timeout`, and no parameter exists | Add a timeout adapter mounted on the client's private `_session` |
| Retry control | One setting per client (`_retry`, `_retry_codes`) that applies to **every** method (GET/POST/DELETE). The public `TradingClient` constructor doesn't expose it. On `RESTClient`, `retry_attempts=0` and `retry_exception_codes=[]` are ignored | Retries can't be disabled for submissions only. Options: (a) set `_retry = 0` on a client used only for submissions; (b) set it on all clients and retry reads in a wrapper |
| Retry deadline | Fixed 3 s waits, no overall deadline | The wrapper's `read()` enforces a deadline and a maximum attempt count |
| Client order id | Supported on requests (≤ 128 characters); lookup via `get_order_by_client_id` | The wrapper requires one on every order and persists it before the POST |

Both hooks (`_retry`, `_session`) are **private**. The wrapper therefore checks the SDK version and the
attributes at startup and **fails closed** on any mismatch. The SDK version should be pinned
(`alpaca-py==0.43.5`) before this is adopted.

## Global-vs-submission trade-off

- **Two clients (recommended during transition):** a submission-only client with `_retry = 0` behind
  the gateway, plus the existing client, with timeouts, for reads.
  - Reads keep today's retry behavior.
  - Submissions are never blind-retried.
  - Cost: two HTTP sessions and one more object to configure.
- **One client with `_retry = 0` everywhere:** simplest end state, but every read must go through
  `broker_io.read()` first. Otherwise, paths that still treat errors as zero (`verify_sell_filled` and
  post-buy fill detection, both P0-2 leftovers) would see *more* failures, not fewer.

## Stages

| Stage | Change | Removes | Leaves | Touches Codex's area? |
|---|---|---|---|---|
| **A** (diff ready) | Timeouts on `trading_client` and `data_client`; SDK retries kept | Indefinite hangs. Worst-case read ≈ 4 × 13 s + 9 s ≈ 61 s, tested | **Duplicate POSTs after 504** (tested: 4 orders from 1 call) | No: construction lines only |
| **B** | Submission client with `_retry = 0`. `place_buy`, `place_market_sell`, `_submit_stop_limit_sell`, flatten/emergency and the Target-1 IOC all go through `OrderGateway.submit()` with a durable client id | Blind resubmission; untracked duplicates; "exception = not placed" | Duplicate-id handling still unverified (paper test P3) | **Yes**: callers must handle `UNRESOLVED` |
| **C** | All reads through `broker_io.read()`; `_retry = 0` on every client | Fixed-wait SDK retries; free-text "not found"; reads without a deadline | — | Yes: position/order read sites |

### Stage B: caller semantics that must change (the reason for coordination)

- `ACCEPTED` → continue as today, using the order id.
- `REJECTED` (a definitive 400/401/403/422 on the first POST) → not placed. Safe to treat as failure.
- **`UNRESOLVED`** → exposure is unknown. The caller must:
  - persist the client id in position state;
  - block new entries;
  - skip any action that assumes the order does or doesn't exist;
  - call `reconcile()` on following cycles.

  **It must not call `submit()` again with a new id.**
- `NOT_FOUND_AFTER_WINDOW` → absence is *inferred* (lookup and list both negative after 30 s). Only then
  may the caller call `resubmit()`, which reconciles again first and re-POSTs with the **same** id and
  payload.
- **On startup:** `recover_pending()` reconciles every intent left in `SUBMITTING` or `UNRESOLVED`. It
  never re-POSTs.

The intent store is its own SQLite file (`order_intents.db`, WAL, `synchronous=FULL`), separate from
`trading_system.db`. Linking intent rows to position state and to trade rows (exactly-once accounting)
is the Codex work item. The client id is the natural key for that link.

## Offline test evidence (30/30 pass; `test_results.txt`)

| Group | What's proven | Key result |
|---|---|---|
| A. Configuration | Retries disabled on the gateway client; timeout tuple reaches the transport; fails closed on version or attribute mismatch; unconfigured client refused | pass |
| B. Submissions | 504 and 429 give **1 POST**, then `UNRESOLVED`. Accepted-then-504, read-timeout or reset gives `ACCEPTED` via the same id, 1 POST, 1 order. 403 gives `REJECTED`. Non-JSON 504 doesn't crash. Missing client id refused before any POST. A repeated `submit()` never re-POSTs. Same id with a different payload is refused | pass |
| C. Persistence | Intent-write failure means **0 POSTs**. A record failure after an accepted POST leaves the row `SUBMITTING`; `recover_pending()` gives `ACCEPTED` with 1 POST total | pass |
| D. Reconcile and resubmit | Crash before the POST: `UNRESOLVED` inside the window, no POST. After the window, lookup plus list confirm absence, then **exactly one** resubmit with the same id. Crash after an accepted POST: `ACCEPTED`, 0 POSTs. Delayed visibility: found by the list. Lookup or list unavailable: stays `UNRESOLVED`, never "absent" | pass |
| E. Reads | Bounded by deadline and attempt count. Structured 404/40410000 is `NOT_FOUND` on the first attempt. Free-text "not found" is `UNAVAILABLE`. Transient-then-OK recovers. 401 is `ERROR` (not retried). Cancel: 204 means `CANCEL_REQUEST_ACCEPTED` (order still `pending_cancel`); 422 means `NOT_CANCELABLE` via the structured status | pass |
| F. Real socket | A silent loopback server raises `ReadTimeout` at **1.50 s**. With the stock SDK the same setup blocked for the full 8 s | pass |
| G. Fresh process | Process A dies after the broker accepted the order, before recording. Process B recovers `ACCEPTED` with **0 POSTs and 1 broker order** | pass |
| H. Stage A | SDK retries kept; timeouts applied; 504 path = 4 attempts with 3 s waits; **4 duplicate orders still possible** (documented risk) | pass |

**Control:** the same submission tests run with SDK retries restored (`_retry = 3`) fail 4 of 4. In
one of them, the blind retry silently created a second order that the SDK reported as a success.

## Assumptions still requiring paper observation (P3/P4/P7)

- Alpaca's response to a repeated `client_order_id`. The gateway never depends on it, because it never
  re-POSTs an id that might exist, except after `NOT_FOUND_AFTER_WINDOW`.
- The 30 s visibility window. It's a placeholder, not a measured value.
- That 404 + 40410000 is returned for an unknown client id.
- Whether a 429 on POST ever coincides with acceptance. The gateway treats it as ambiguous either way.
- The timeout values (3.05 s connect, 10 s read). These are proposals to tune.
