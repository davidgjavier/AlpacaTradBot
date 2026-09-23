# Order-identity gap map — integration bot `crypto_trading_bot.py` (`f48d7e71…`); the candidate is identical outside the liquidation path

**Legend:**
- **CID:** a `client_order_id` is sent.
- **Intent:** durable record written *before* submit.
- **Recon:** how the outcome is established.
- **Consequence:** what a lost response or delayed visibility can cause.

Line numbers refer to `f48d7e71…`.

| # | Site (line) | Purpose | CID | Intent | Retry behavior today | Recon today | Consequence |
|---|---|---|---|---|---|---|---|
| E1 | `main` L1706 → `place_buy` L696 | Path A trend **entry buy** (market, notional) | N | N (state written only if the fill is seen after `sleep(2)`) | None explicit; the next cycle re-evaluates the signal. If the position still reads 0 (buy hidden/unfilled), **buys again** | Position read after 2 s | **Duplicate entry → exposure up to 2× MAX_POSITION_USD**; unprotected until a later cycle |
| E2 | `main` L1790 → `place_buy` | Scalp **entry buy** (scalps paused) | N | Partial (SCALP state written even without a fill, no order id) | As E1 | Position read after 2 s | As E1 |
| X1 | `main` L1328 `place_market_sell` | Hung-stop **emergency exit** | N | N | Exception → logged; the next cycle re-evaluates and may sell again | `verify_sell_filled` (position, latest read) | Duplicate exit submission if the first is hidden; state kept when unconfirmed (Stage 3) |
| X2 | `main` L1459 | Scalp runner **trail-breach exit** | N | N | as X1 | as X1 | as X1 |
| X3 | `main` L1517 | **Time-decay exit** | N | N | as X1 | as X1 | as X1 |
| X4 | `main` L1615 | Trend **trail-breach exit** | N | N | as X1 | as X1 | as X1 |
| X5 | `main` L1732 | **Sell-signal exit** | N | N | as X1 | as X1 | as X1 |
| X0 | `flatten_position` L734 | Legacy breaker flatten (**unused since Stage 5**) | N | N | — | — | Dead path; remove or keep unused |
| S1 | `_submit_stop_limit_sell` L575 via `place_protective_stop` L600 (entry L1711, repair L1588, scalp entry L1813) | **Protective stop** | N | N (stop id stored only after return) | Lost response → stop id None → the next cycle's repair re-places if no open order is **listed** | Open-order count/list; `stop_order_still_open` | **Duplicate stop** if the first is hidden (a second full-qty stop is rejected or double-reserves, depending on the broker; not relied on), or an **unprotected** position if the repair believes none exists |
| S2 | `main` L1484 `_submit_stop_limit_sell` | **Trailing stop raise** (cancel → place) | N | N | as S1 | cancel-confirm + list | Unprotected window + duplicate risk |
| S3 | `replace_protective_stop` L640 (L1586, L1646) | **Stop replacement** | N | N | as S1 | as S2 | as S2 |
| S4 | `place_take_profit_limit` L620 | Legacy scalp TP | N | N | — | — | Superseded by P0-1 IOC Target-1 |
| S5 | `_place_validated_stop` L966/L969 | P0-1 validated stop | N | N | as S1 | as S1 | as S1 |
| OK | L863 liquidation sell; L977 validated-stop market fallback; L1139 Target-1 IOC | — | **Y** | Y (liquidation, Target-1) | reconcile by CID | by CID | covered by Stage 5 / P0-1 |

## Options

| | **A. Reuse the P8 `OrderGateway`** (`broker_io.py`, 103 tests) | **B. Smaller extension** (the liquidation pattern per order class) |
|---|---|---|
| Provides | Durable intents, persistent CIDs, bounded timeouts with **no submit retries**, submit-in-flight ownership, identity-mismatch checks, `entries_locked`, pending recovery | Deterministic CID + intent written before submit for E1/E2, X1–X5, S1–S5; reconcile by CID; any unresolved intent blocks new orders of that class |
| Size | Large (≈59 KB), already written, **0 references from the bot** | Estimated a few hundred lines + tests, reusing Stage 5 code |
| Blocking decisions | **D7** (account key for the intent DB), **D8** (protection for an unresolved entry), **D9** (unresolved stop placement), **D10** (legacy cutover) | **D9** for stops; the **R1 principle** (positive reconciliation) for exits and entries |
| Conflict to resolve | P8's `abandon`/`release_entry_lock` accept **NEGATIVE_COMPLETE (negative evidence)**. That conflicts with Option 1 unless aligned (lock release already disabled). | None beyond the R1/D9 choices |
| Known open items | Shared `requests.Session` across threads; DNS stalls; unvalidated deadline values | New code needs its own review |

## What can proceed independently (no pending policy)

1. **CIDs and write-before-submit intents for every order**, with no behavior change beyond identity and
   journaling. This makes lost responses **reconcilable** instead of invisible.
2. **The entry-duplicate invariant: no new entry while a prior entry intent is unresolved.** It's the same
   conservative rule already accepted for liquidation (entries blocked while unresolved), and needs no new
   policy.
3. **Removing the dead X0 path** (or marking it unused).
4. **An offline fake-broker regression suite** for E1–S5 lost responses and delayed visibility,
   written first.

## Smallest genuinely blocking decisions

1. **R1** (Option 1 / 2 / 3): whether negative evidence can ever resolve an attempt. It governs exits and
   entries, and P8's abandon path.
2. **D9:** what protection to hold while a **stop** submission is unresolved. Placing another risks
   duplication; not placing risks an unprotected position.
3. **D7:** only if reusing P8 (per-account intent DB). Option B can use the existing DB under the
   single-account assumption.

**D8 and D10 are not blocking for identity work itself.** They matter for protection policy and cutover.
