# D8 / D10 decision options + crash-window inventory (Stage 5c; integration branch)

**No policy below is implemented.** The code keeps the current behavior until David decides. Acceptance
tests are specified here so the chosen option can be written test-first with the fake broker.

## D8 — behavior while the position is UNREADABLE

**Current (implemented, tested):** nothing that depends on the position runs. There are no new orders, no
entries, and state and pending references are preserved. The liquidation stays pending. Recovery is
automatic when a strict read succeeds.

**Gap:** a residual can sit with **no resting stop**: after liquidation cancelled it, or if the stop failed.
**Nothing bounds the duration or loss.** No alert exists beyond the log.

| Option | Behavior | Risks | Recovery |
|---|---|---|---|
| **A. Status quo + alert** | As now, plus a visible `POSITION_UNKNOWN` flag (DB/dashboard) with a since-timestamp after N cycles. | Same exposure; only visibility improves. The alert channel must itself work. | Auto on read; David can act manually meanwhile. |
| **B. Protect last confirmed qty** | After N unknown cycles, place a stop for the **last confirmed** qty, but only if no own sell is OPEN/UNKNOWN and open orders are listable. | Stale qty: oversized → broker rejects (spot, no short); undersized → partial protection. The stop reserves quantity and blocks a liquidation re-sell until cancelled. Needs its own reconciliation state. | Next readable cycle cancels or resizes the stop before liquidation continues. |
| **C. Order-derived qty** | Estimate qty = last confirmed − confirmed fills of *our* orders (order endpoint may work when the position endpoint fails). | Misses fills of orders we don't track (manual, other processes); wrong if the order list is also unknown. | As B. |
| **D. Escalate to halt** | After N unknown cycles, stop the bot process deliberately (fail-closed) and alert. | Bot down: no protection management at all; relies on the resting broker stop if any. Needs a supervised restart policy (the launchd `KeepAlive` would restart it). | David restarts after investigation. |

**Acceptance tests (offline, per option):**
- **A:** after N failed reads a flag is set with a timestamp; no orders; cleared on the first strict read.
- **B:**
  - no stop while the own sell is OPEN/UNKNOWN or open orders are unknown;
  - a stop sized at the last confirmed qty after N cycles;
  - on the next readable cycle, cancelled and confirmed before any re-sell;
  - no oversubscription rejection in the normal path.
- **C:** derived qty equals confirmed position in the synthetic cases; refuses when any sell is unknown.
- **D:** exits with a distinct code after N cycles; no orders; pending state intact on restart.
- **All options:** N and thresholds are David's choice; unchanged risk limits (`DAILY_LOSS_LIMIT_USD` 150).

## D10 — legacy "breaker marked today + residual qty > 0 + no pending liquidation" at cutover

**How it arises:** the pre-fix code marked the breaker done after a PARTIAL flatten (Stage 5 defect 1).
The fixed code can't create it, but a DB written earlier the **same UTC day** can hold it. On the next UTC
day the stamp resets and the residual becomes an ordinary position.

**Current (implemented):** the latch blocks entries (qty > 0 means no entry path anyway); the residual gets
normal protection management and is **not** liquidated. Audit "Breaker must retry residual liquidation"
still FAILs.

| Option | Behavior | Risks | Recovery |
|---|---|---|---|
| **A. Leave (status quo)** | Manage normally; liquidation not resumed. | Residual exposure contrary to the breaker's intent, for the rest of that UTC day. | None needed; ordinary position next day. |
| **B. Resume liquidation** | On the first cycle, if stamp == today and qty > 0 and no pending → create a pending liquidation (same reconcile-first machinery). | Would also liquidate a position David **re-entered manually** after the breaker, and anything opened by another process. | Cancel via clearing `liquidation_state` (manual). |
| **C. Refuse start (fail-closed)** | Exit at startup with a message until David resolves (flatten manually or clear the stamp). | Bot down that day; a resting broker stop protects only if present; `KeepAlive` restart loop. | David resolves, then restarts. |
| **D. One-time cutover check** | A separate offline script (not the bot) lists the state and requires an explicit choice (B or A) recorded in the DB before first start. | Operational step can be skipped; needs a documented runbook. | Re-run the script. |

**Acceptance tests:**
- **A:** unchanged behavior; no entries; protection managed.
- **B:**
  - pending created exactly once;
  - a sell sized from a post-reconciliation read;
  - a manually re-entered position is **also** liquidated (documented consequence);
  - the new day does not create it.
- **C:** distinct exit code; no orders; nothing written.
- **D:** the bot refuses to start while unresolved; either recorded choice leads to the A or B behavior.

## Crash windows (liquidation path)

**Tested with the fake broker:**
- Restart with a pending liquidation resumes (`test_stage5`).
- **Lost submit response:** the intent (client id) is persisted **before** the submit; the order is found
  by client id; no second sell while it works (`test_stage5`).
- **Intent persisted but never sent:** client id NOT_FOUND → resubmitted exactly once (`test_stage5c`).
- **Crash between completion writes:** write order is now latch → position → liquidation record last.
  A crash after write 1 or 2 re-completes on restart; the latch holds; no entry reopens (`test_stage5c`).
  Before the reorder, a crash after write 2 reopened entries.
- **Post-cancel read failure:** pending, nothing submitted (`test_stage5b`).

**Unresolved assumptions (not verifiable offline; need paper contract tests, D3/D4):**
1. **Client-id NOT_FOUND is authoritative and immediately consistent.** If the broker briefly returns 404
   for an accepted order, it is treated as never sent and a second sell is submitted. The consequence
   relies on Alpaca crypto being spot with no shorting: the second sell is sized from a fresh read and
   is expected to be rejected or partially filled, never to create a short. **Untested against the
   broker.**
2. **Structured 404 = code 40410000** for an unknown client id and an absent position (existing
   `_is_not_found` assumption).
3. **A persistently UNKNOWN own-order lookup keeps the liquidation pending indefinitely:** entries
   blocked, no alert (see D8-A).
4. **A fill after the post-cancel re-read but before the submit:** the sell may exceed holdings, so the
   broker rejects it. The submit exception keeps the client id; the next cycle looks it up (rejected →
   terminal, or NOT_FOUND) and resizes. **Exercised only via generic submit failure, not this exact
   race.**
5. **Each DB write is its own SQLite transaction.** Order-resilience is tested; power-loss durability
   (journal/synchronous settings) is not verified.
6. **Concurrent writers** (dashboard vs bot) can still clobber `position_state`. The audit "Concurrent
   state writer…" still FAILs; this is out of scope here.
7. **Completing on a later UTC day** stamps the liquidation's original day. The new day is unlatched,
   intentionally; entries were blocked by the pending liquidation until completion.
