# Paper-test plan v2 (NOT executed; no broker calls or credentials authorized)

**Supersedes v1** (`candidate_option1_v1/3_PAPER_TEST_PLAN.md`). **Requires David's separate approval** (D3/D4)
and a **dedicated paper account**.
- **The harness is built and tested offline only:** `review/paper_contract/harness_v1/` (21 tests, fake
  transport). It has **no default broker adapter**; a broker transport would be a separate, reviewed file.
- **Paper observations characterize the paper environment only.** They establish neither live behavior nor
  any maximum delay.

## Corrections from v1 (Codex review items 2–5)

1. **Cleanup obeys the bot's unknown-order invariant.**
   - Cleanup never submits a market sell while any earlier sell, buy, cancellation or the position is
     unknown. It tracks identities, reservations (open-sell qty − filled) and cumulative fills.
   - It makes at most **one** cleanup sell per invocation, and never a second while the first is
     unresolved.
   - If cleanup can't be confirmed: stop with **UNRESOLVED**, report residual exposure and the unresolved
     client ids, request human reconciliation, block all further orders, and keep the journal.
   - v1's rule ("cancel everything, then market-sell any BTC") is **demonstrated defective** by two
     characterization tests.
2. **P6 is capped** by min(requested, **confirmed owned and unreserved** inventory, notional cap). It
   **never buys to create size or exceed depth.** If partials can't be produced within the limits, the
   result is **INCONCLUSIVE**.
3. **Far-away limit prices can still fill.** Any fill a step didn't predict is a **stop condition**, and
   cleanup then applies the same invariant.
4. **Budget:**
   - Every submission **attempt** counts: accepted, rejected, duplicate client id, or lost response.
   - **A cleanup reserve of 4 orders is held inside the 20-order cap**; test steps can't use it.
   - Notional caps apply to cleanup too ($25 per order, $100 aggregate buys).
   - If the reserve is exhausted with inventory left: UNRESOLVED, residual reported.
5. **Termination:**
   - A **graceful** Ctrl-C stops test steps and runs cleanup under the invariant.
   - A **second** Ctrl-C during cleanup is recorded as **FORCED**, and tracked orders may remain.
   - **Forced termination** (SIGKILL, crash, power loss) can't be handled in-process. The journal is
     written and fsynced **before** every submission, so a restarted harness rebuilds state, keeps the
     attempt count, and **refuses new orders until everything is reconciled**.
6. **P8 (lost-response injection) stays OFFLINE** for the first proposed subset. Broker-connected injection
   needs a separately reviewed transport (bounded requests, **order-submission retries disabled**,
   persistent client ids) and explicit approval. A short caller timeout doesn't cancel remote execution.

## First proposed subset (after approval)

| ID | Tests | Orders (max) | Needs inventory |
|---|---|---|---|
| P1 | Structured not-found (random id, random client id, flat position) | 0 | no |
| P2 | Client-id and id visibility timing for a resting limit buy far below market. **Adverse fill → stop.** | ≤ 5 | no |
| P3 | Duplicate client id rejection | ≤ 1 | no |
| P4 | Cancel status sequence and timing for P2 orders | 0 (cancels) | no |

- **P5–P7** (IOC finality, partials, market-sell lag) need inventory, so a buy. They're deferred to a second
  approval, and each is bounded by confirmed unreserved inventory.
- **P8** remains offline.
- **Budget for the first subset:** ≤ 6 test orders + 4 reserved cleanup = within 20; notional ≤ $25/order.

## Isolation, account verification, stop conditions, evidence (unchanged from v1 except as corrected)

- A dedicated paper account; the paper endpoint only; `GET /account` identity checks against values David
  types at launch; preflight requires no open orders and no position.
- Credentials are supplied by David at run time and never read or stored by Claude; keys are redacted.
- **Stop on:** any unpredicted fill, a limit or budget breach, account mismatch, an unexpected error class,
  any unresolved order, an anomaly (late fill after terminal, decreasing `filled_qty`, unrecognized status),
  or the wall clock (30 min).
- **Evidence:** the harness journal (append-only JSONL), redacted raw responses, per-test verdicts, the
  cleanup report, and script hashes. Reviewed by Codex before conclusions.
