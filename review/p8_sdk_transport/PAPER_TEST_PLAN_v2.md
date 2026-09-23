# Paper broker-contract test plan v2 (P1–P7) — PREPARED, NOT EXECUTED

**Status: requires explicit approval before any step that contacts the broker.** That includes the
read-only preflight, because it uses test-account credentials. Nothing here changes the bots, their
account, their settings, or their services.

## 0. Scope: contract only, not execution quality

These tests establish **API contract behavior**: which requests are accepted or rejected, the status
codes and error codes returned, order status fields, reservations, fee accounting, lookup semantics
and symbol handling.

They **cannot** establish liquidity, slippage, fill probability, queue position, stop-trigger timing
during gaps, or live latency. Paper trading fills only when an order becomes marketable, gives random
partial fills about 10% of the time, and ignores available size.

HTTP codes, Alpaca error codes, messages and timing that Alpaca doesn't document are recorded as
**observations**. They are never pass/fail criteria. A run passes if every step executes within its
bounds, every observation is recorded, and cleanup completes.

## 1. Account isolation, verified without exposing credentials

1. Use a **separate paper account** created for these tests, with its own keys in a separate env file
   (e.g. `review/paper_contract/.env.contract`, mode 600, not in git). The script must refuse to read
   the bots' `/Users/davidj/AlpacaTradeBot/.env`.
2. Preflight, using read-only `GET /v2/account`:
   - Compute `sha256(account.id)[:12]` for the test account, and the same for the bot account (from the
     bot's env, read by a separate process that prints **only the hash**).
   - **Abort if the hashes are equal.** Also compare `sha256(API key id)[:12]` and abort if equal.
   - Never print, log or persist a key, secret or full account number. Logs contain hashes only.
3. The endpoint must be `paper-api.alpaca.markets`. Abort if the configured base URL is anything else.
4. **Baseline must be clean:** zero open orders and zero BTC position on the test account; otherwise
   abort. Record the USD balance (paper starts at $100k by default).

## 2. Hard bounds, enforced by the script

| Bound | Limit |
|---|---|
| Max quantity per order | 0.0004 BTC (≈ $35 at $86k) |
| Max BTC held at any time | 0.0008 BTC |
| **Cumulative gross notional** (all fills, both sides) | **$250**; the script stops before any order that could exceed it |
| Max orders submitted, whole run | 25 |
| Max wall-clock duration | 45 minutes, then stop and clean up |
| Resting order prices | non-marketable orders at least 20% away from the market (buy limits below, sell limits above) |
| Retries on ambiguous submission | **none by the script**. The SDK's internal 429/504 retry still applies (see P8); every ambiguous result is reconciled by client id before anything else |
| Abort triggers | budget or count reached; unexpected fill; unexpected position change; any cleanup failure; any non-paper endpoint |

Every order uses a client id `p1-7-<run8>-<step>`. The script writes each **order id and client id to
a local ledger file before and after submission**, so cleanup can always work from the ledger.

## 3. Funding and inventory prerequisites for sell tests

- Sell tests (P1 sells, P2, P5, P6) need BTC in the account. The only way to get it on paper is a buy,
  so **step I0 buys the inventory**: a market buy of 0.0008 BTC. It counts against the notional budget.
- The buy fee is deducted **in BTC**. Record the actual position `qty` after I0, and size every sell
  from that recorded number, never from 0.0008.
- A residual below the pair's minimum order size (about $1 notional) may be **unsellable**. It is
  recorded as an accepted residual, not a cleanup failure.

## 4. Tests

| Test | Steps (all bounded) | Record as observation | Pass condition (contract) |
|---|---|---|---|
| **P7** Symbols and 404 (read-only GETs; run first, before I0) | Get `BTCUSD` and `BTC/USD` positions while flat. After I0, repeat both and also list all positions | status, code and message of each | All calls complete; the 404 variants are recorded to decide the "confirmed flat" rule |
| **I0** Inventory | One market buy, 0.0008 BTC | order `filled_qty` and `filled_avg_price` versus position `qty` and `qty_available` | Position above 0; fee difference recorded (feeds P6) |
| **P1** Order type / TIF matrix | Non-marketable sell limits: IOC limit (expect canceled, 0 filled), GTC stop-limit (resting; cancel afterwards), plus the unsupported ones: IOC stop-limit, FOK limit, plain stop. At most 5 orders; market orders are left to I0 and P6 | status and error code for each | Supported ones accepted, unsupported ones rejected. Any deviation means the docs or bot assumptions must change (recorded, not "fixed") |
| **P2** Reservation | Rest a GTC stop-limit sell for the full inventory, far below market. Then a 0.0002 GTC limit sell (non-marketable), then a 0.0002 market sell. Cancel the stop by id | status and code of each rejection; `qty_available` before and after | An oversell is **not accepted**. If one is accepted, that's a critical finding: stop the run and clean up |
| **P3** Client id duplicate and lookup timing | Non-marketable buy limit with client id X. Look up X immediately, then every 250 ms for up to 5 s. Resubmit X once. Look up a random unused id. Cancel X by id | duplicate response; lookup latency; 404 details | No step may create a second open order for X. If it does, cancel both by id, then stop and report |
| **P4** Cancel semantics | Cancel the resting P3 order by id, polling status every 250 ms for up to 10 s. Cancel it again. Cancel the filled I0 order | 204 / 422 codes; states seen (e.g. `pending_cancel`) | Final status is terminal; repeat cancels complete without error to the script |
| **P5** Partial fill reporting (**observational**) | **At most 6** marketable IOC limit sells of 0.0001 BTC, stopping early at the first partial fill, the budget, or the order count. Never repeated to "get" a partial | per order: `filled_qty`, status, `filled_avg_price`, and when each appeared | Completes within bounds. **"No partial observed" is inconclusive, not a failure** |
| **P6** Fees and position accounting | Market sell 0.0002 BTC | position `qty` delta versus `filled_qty` (the sell fee should be in USD); USD cash change | Recorded. Updates the Q1 formula assumptions |

Execution order: preflight → P7 (flat) → I0 → P7 (holding) → P1 → P2 → P3 → P4 → P5 → P6 → cleanup.

## 5. Cleanup, by exact ids only

1. For each order id in the ledger that isn't terminal: cancel **by id**, then poll up to 10 s for a
   terminal status. Cancel-all is never used.
2. Sell any remaining BTC down to the unsellable residual with one market sell sized from the observed
   position.
3. Verify zero open ledger orders and a BTC position at or below the minimum-order residual. Save the
   ledger and all responses to `review/paper_contract/run-<ts>/`.
4. **If cleanup fails** (an order stays non-terminal after 10 s plus one retry, or the sell fails):
   - stop immediately and don't loop;
   - print the unresolved order ids;
   - leave the account untouched;
   - ask the user to resolve them in the paper dashboard.

   Because the test account is isolated, the bots are unaffected. Close-all or cancel-all on the test
   account is allowed only with a new explicit approval.

## 6. What a pass does and does not mean

- **A pass means** the documented contract, plus the recorded observations, hold on paper today.
- **It does not mean** live fills, stop triggering during gaps, latency, or duplicate-id behavior
  under real 504s will behave the same way. P8 showed the SDK can resend a submission after a 504, but
  paper can't be made to return a 504 on demand.
- **Undocumented behaviors seen once** (duplicate-id handling, lookup timing) are single observations,
  not guarantees. Designs must still handle both P8 branches (S4a and S4b).
