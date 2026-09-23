# Proposed paper subset P7-RO — READ-ONLY (NOT APPROVED, NOT EXECUTED)

**Separate from P3.** P3 submits orders; this subset sends **no orders and no cancels**. It performs only
HTTP GETs against a paper account. It still needs explicit approval, because it uses paper credentials
and contacts Alpaca. The paper-plan corrections in `PAPER_TEST_PLAN_v2.md` (sizing, P5/P6 shortfall)
remain **open** and are not addressed here.

## Purpose

Observe, without changing account state, the broker semantics the wrapper depends on:
- 404 structure for positions;
- symbol formats;
- order-list pagination behavior;
- client-id lookup for an id that was never used.

## Preconditions

- A **separate paper account** from the bots' account, verified by comparing
  `sha256(account.id)[:12]` and `sha256(key_id)[:12]` against the bot account and aborting if either
  matches. Nothing secret is printed.
- Base URL must be `paper-api.alpaca.markets`; abort otherwise.
- The script's HTTP layer allows **GET only**: any POST, PATCH or DELETE raises before sending (enforced
  in code, not by convention).
- The SDK is used through `broker_io.configure_client()`: timeouts on, SDK retries off. Each GET is
  recorded with status, Alpaca `code`, message, headers relevant to pagination, and latency.

## Steps (all GET; bounded to ≤ 20 requests and ≤ 5 minutes)

| Id | Request | Records (observation only) |
|---|---|---|
| RO-1 | `GET /v2/account` | Account id hash, status, `crypto_status`. Must be the separate account |
| RO-2 | `GET /v2/positions/BTCUSD` and `GET /v2/positions/BTC%2FUSD` | HTTP status, `code`, message for each (flat account expected) |
| RO-3 | `GET /v2/positions` | Symbol format of any positions (expected none) |
| RO-4 | `GET /v2/orders:by_client_order_id?client_order_id=<fresh random uuid>` | Status, `code`, message for a never-used id (hypothesis: 404 / 40410000) |
| RO-5 | `GET /v2/orders?status=all&limit=500&direction=asc&after=<t>` (1–3 pages with the wrapper's cursor rule) | Page sizes, ordering, and whether the `after` cursor behaves as the wrapper assumes (on an empty or near-empty account) |
| RO-6 | `GET /v2/assets/BTC/USD` (or by `BTCUSD`) | `min_order_size`, `min_trade_increment`, `price_increment`, for the deferred sizing fixes |

## Pass/fail

- **Pass:** every request executed, was recorded, and was GET-only; the account was verified separate;
  no state changed (orders and positions identical before and after).
- Observations are **not** pass criteria. Examples: whether RO-4 returns 404/40410000, and the exact
  RO-2 messages. They are recorded to update the wrapper's documented assumptions.

## What this cannot establish (needs P3, which submits orders)

- Duplicate client-order-id handling.
- Visibility delay of a new order.
- Lookup/list behavior for an order that exists.
- qty-versus-notional reporting on real orders.
- Reservation behavior.
