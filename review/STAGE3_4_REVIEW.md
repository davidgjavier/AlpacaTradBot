# Stages 3–4 — isolated integration (partial, stopped at review gate) · OFFLINE

- **Worktree:** `/Users/davidj/AlpacaTradeBot_integration_worktree`, branch `review/stage3-integration`
  (from `94b2efd`).
- **Nothing** here touches the running checkout, services, credentials, settings, the SDK or the broker.
  Operational lock release stays disabled.

## Commits

| Commit | Content |
|---|---|
| `20c28a9` | Codex accounting idempotency, verbatim (`crash_review a35f86a..cbf1b9b`; the baseline files were verified identical to `94b2efd`) |
| `2664dae` | P8 `broker_io.py` and its 103 tests, verbatim (`eb02791`; sha `195fc3c2…`) |
| `6eb7df1` | Stage-3 exit-confirmation/breaker tests (before-evidence) plus combined baseline evidence |
| `84b0318` | Fix: unconfirmed sell exits never read as flat; breaker marked only when confirmed |
| `1f6171b` | Cosmetic: inert markers → explicit `pass` (no behavior change) |

## Combined baseline (`2664dae`, before any caller change)

- `tests/`: 54/54 (44 P0-1/P0-2 + 8 accounting + 2 spec-sync) with Codex's `discover` command.
- P8: 103/103.
- **Total 157/157.**
- **Integration finding:** `tests/test_accounting_restart.py` errors when run as `-m unittest
  tests.test_accounting_restart`, because it imports `p0_1_harness` relying on `discover`'s path. It's
  an invocation dependency, not behavior; `discover` is used as canonical.

## Caller map (`crypto_trading_bot.py`, `1f6171b`)

| Call site (function @ line) | Current behavior | Proposed wrapper contract | Durable state | Audit link |
|---|---|---|---|---|
| `place_buy` @688 (both entry paths) | market notional, no client id; exception → "Order failed", nothing recorded | `OrderGateway.submit` with a persisted client id; UNRESOLVED blocks entries | **needs a pending-entry intent** (none today) | "Pending crypto entry must prevent duplicate entry" FAIL |
| buy-fill check @~1537, @~1617 | `get_position_qty()` (**error → 0**) after `sleep(2)` | strict read; unknown ≠ 0 | pending-entry intent | "crypto lookup outage" FAIL (legacy fn) |
| `_submit_stop_limit_sell` @565, `replace_protective_stop` @631, `place_protective_stop` | no client id; exception → None ("unprotected") | submit + intent; UNRESOLVED ≠ "no stop" (possible duplicate reservation) | `position_state.stop_order_id` only | "Failed stop replacement preserves prior floor" FAIL |
| `place_take_profit_limit` @603 | legacy resting TP (P0-1 no longer calls it for scalps) | retire, or route through the gateway | — | — |
| `_scalp_execute_target1` @990 | IOC limit, `p01t1-` client id persisted before POST (P0-1) | already intent-like; migrate to the gateway | `take_profit_order_id='cid:…'` | P0-1 fixed |
| `place_market_sell` @695 (6 exit paths) | no client id | submit + intent; late acceptance possible | `position_state` | — |
| `verify_sell_filled` @652 (6 callers) | **fixed:** strict reads, None = unconfirmed | wrapper `read()` with budget | unchanged state on None | "Exit must not be confirmed on lookup error" **PASS (was FAIL)** |
| `flatten_position` @701 + breaker @1116 | **fixed:** returns FLAT/PARTIAL/UNCONFIRMED/FAILED; breaker marked only on FLAT/PARTIAL | — | baseline breaker stamp | "Breaker must retry residual" still FAIL (pre-marked scenario) |
| `cancel_order_if_open` @381 / `cancel_and_confirm` @535 | swallows errors; polls the open-order count | wrapper `cancel()` = request accepted, not terminal; poll the order | — | "Cancel/fill race must re-read position" FAIL |
| `stop_order_still_open` @399 | lookup error → False ("not open") | tri-state (OPEN/NOT_OPEN/UNKNOWN) | — | P0-2 remainder |
| `get_order_fill_state` @441, `_position_qty_strict` @483, `_open_sell_reserved_strict` @492 | already tri-state (P0-1/P0-2) | wrap with `read()` + budget | — | — |
| `get_actual_fill_price` @669 | falls back to the estimate silently | keep, but flag price-unconfirmed | trade row | P0-1 `price_unconfirmed` |
| `get_account` @752/763 (breaker P/L) | error → P/L 0.0 → breaker not tripped | unknown P/L → block entries | — | "Unmeasurable account risk blocks new entry" FAIL |
| `data_client` bars/quotes @228/250/279/734 | exceptions → skip cycle / None | wrapper `read()`; stale/NaN quotes rejected | — | "Reject NaN/crossed quotes" FAIL |
| `db.log_trade` ×7 inline in `main` | 1 keyed (Target-1 `execution_key`); others unkeyed | key every terminal exit by broker order id | ledger | Codex `cbf1b9b` covers Target-1 only |
| `db.set/clear_position_state` ×~30 inline in `main` | single row per SYMBOL; no account key | account + symbol key; intent ids per leg | `position_state` | "Concurrent state writer…" FAIL |

## Blocking design gap (stops dependent integration at this gate)

The bot has **no durable per-order identity** apart from Target-1. Position state is one row per
symbol, holding one `stop_order_id`. Routing entries, stops and exits through `OrderGateway` needs
these decisions, which the brief says must not be made silently:

1. **Account key (D7).** Where the intent store lives and how it's keyed so paper and live, or two
   accounts, never mingle. Proposed: `{mode}:{sha256(account_id)[:12]}` from `GET /v2/account` at
   startup, failing closed if it's unreadable. Paper and live get separate DB files.
2. **Unresolved-entry protection policy (D8).** If a buy is UNRESOLVED but a strict read shows
   `qty > 0`, do we protect the *observed* position (a stop sized from the broker qty, never from the
   intent)? Proposed yes. Unknown qty → no stop, no claim of protection, entries locked.
3. **Unresolved stop placement (D9).** An ambiguous stop submission may or may not reserve BTC.
   Proposed: don't place a second stop while the first is UNRESOLVED; alert; reconcile by client id.
4. **Legacy-row quarantine (D10).** Existing unkeyed trades and position rows can't be matched to new
   execution keys. Proposed: at cutover, snapshot `trade_history` and `position_state` into
   `legacy_quarantine` tables. No backfill. Refuse to start if a Target-1 transition is in progress
   (per Codex's rollout note).
5. **Buy-fill confirmation (deferred defect).** Needs a durable pending-entry marker plus a
   cycle-start rule: `qty==0` with a pending buy that isn't terminal must NOT clear state or allow an
   entry. Implementation is ready to start once D7/D8 are decided, since the marker belongs in the
   keyed intent store.

## Recovery protocol (state machine, proposed)

A broker side effect and a DB write are never atomic. Order per side effect:
**(a)** persist intent (client id) → **(b)** POST → **(c)** record the outcome → **(d)** update
position state → **(e)** keyed ledger row.

| Crash window | Result on restart | Required handling |
|---|---|---|
| (a)–(b) | intent SUBMITTING, `post_inflight=1`, no order | UNRESOLVED; entries locked; never auto-resubmit |
| (b)–(c) | order may exist | reconcile by client id → ACCEPTED/UNVERIFIED/CONFLICT |
| (c)–(d) | order known, position state stale | re-derive from the broker position (strict read) + intent |
| (d)–(e) | state advanced, ledger missing | replay from the intent's terminal order → keyed row (`cbf1b9b` makes the replay idempotent) |
| after (e) | complete | — |

**Invariants:**
- Unknown position ≠ flat.
- Unconfirmed exit ≠ closed; the breaker isn't marked done.
- No new exposure while any intent for the account and symbol is SUBMITTING, UNRESOLVED, CONFLICT or
  ACCEPTED_UNVERIFIED.
- Protection is sized only from a confirmed broker qty.
- Strategy parameters are unchanged.

## Stage 4 so far: the original audit (identical harness; only the bot sources swapped)

| Run | PASS | FAIL | SENSITIVITY | HARNESS_ERROR |
|---|---|---|---|---|
| audited snapshot | 6 | 31 | 1 | 0 |
| `94b2efd` (P0-1/P0-2) | 9 | 27 | 1 | 1 |
| integrated `1f6171b` | 10 | 26 | 1 | 1 |

- **Changed checks:** "Exit must not be confirmed on lookup error" FAIL → FAIL → **PASS** (this
  stage). Three active-status checks passed at `94b2efd`.
- The HARNESS_ERROR is the known audit-harness indexing issue.
- **All 26 remaining FAILs are listed individually** in `stage4_evidence/audit_comparison.txt`. None is
  claimed closed.
- The full combined adversarial E2E suite (lost responses, late acceptance, partial fills, concurrent
  processes, etc. through the integrated bot) **depends on the gated integration** and is not built
  yet. The wrapper-level versions exist in the 103 P8 tests.

## Service restarts (read-only evidence; no attribution)

- `com.davidj.alpacabot`, `alpacacryptobot` and `alpacadashboard` plists were **modified 2026-09-23
  09:19:43**.
- The crypto bot and dashboard started 09:21:09.
- The equity bot started 09:32:26 after its previous process exited 1. `bot_error.log` ends with an
  uncaught `requests.exceptions.ConnectionError`; `KeepAlive=true`.
- The plists still point to the same scripts.
- `com.davidj.research.daemon` was created 09:06.
- The evidence establishes *when*, not *who*.

## Stage 5

Not started. It's independent of this gate and is next: revise P1–P8 with broker rules verified from
official sources.
