# Decision package — D8, D10, R1 (drafts for David; nothing here is adopted)

Branch `review/stage3-integration`; supersedes `D8_D10_OPTIONS.md` where they differ.

## Terms used below

- **Block entries:** no new position may open. Already implemented for unknown position, pending
  liquidation and the same-day latch.
- **Stop position management:** no order that *depends on the position quantity* is placed or cancelled
  (protective stops, exits, liquidation sells). Resting broker orders are left as they are.

The two are separate switches. Entries can be blocked while management continues.

## R1 — delayed broker visibility (liquidation sells)

**Finding (tests `test_stage5d_visibility.py`):** the committed code submits a **second sell under a new
client id** when an accepted sell is not yet visible (client-id 404, absent from open orders). This
happens across cycles and after a restart, and it completes while its own sell is unresolved.

**Draft R1** (`review/drafts/crypto_trading_bot_R1.py`, `R1_visibility.patch`):
- **Identity:** each attempt has a persisted client id plus an `inflight` record (`cid`, `since`, `nf`),
  written **before** submit.
- **Rule:** an unproven submit outcome = IN FLIGHT. A client-id 404 inside the bound is not proof of
  non-placement, so there is **no resubmit and no completion**.
- **Bounds (draft values):** ≥ 3 not-found checks **and** ≥ 900 s. After both, `escalated` is persisted
  with a log line. **Still no automatic resubmit.**
- **Recovery:**
  - visible and terminal → normal reconciliation;
  - escalated → operator reconciles with broker evidence (see the operator procedure below).
- **Evidence:** 5/5 visibility tests pass on the draft.
  - **Conflict:** existing test `test_intent_persisted_but_never_sent_is_resubmitted_once` fails under
    R1, because the bot cannot distinguish "never sent" from "sent, not yet visible".
  - Adopting R1 means replacing that expectation with "escalate, no resubmit".

**Alternatives:**
- **R1b:** automatic resubmit after a longer bound. There is a residual double-sell risk whenever
  visibility exceeds the bound. Observed paper delays **cannot establish a universal maximum** (live
  behavior, load and incidents differ), so no bound makes an automatic resubmit provably safe.
  *(Corrected in R1 v2; see `R1v2_PROPOSAL.md`.)*
- **R2:** route liquidation through the P8 broker I/O layer. It already requires NEGATIVE_COMPLETE (404 +
  complete bounded scan + fresh check) before treating an intent as not placed. Gated on D7–D10
  integration.

**Decision:** R1 (and replace that one expectation) · R1b with a stated bound · R2 later · status quo
(known gap).

## D8 — unreadable position

**Alert timing (draft):**

| When | What |
|---|---|
| First unknown cycle | Log line; **entries blocked** (implemented). |
| 3 consecutive unknown cycles (~9 min at 180 s) | Persist `POSITION_UNKNOWN` with a since-timestamp in the DB, visible to the dashboard. |
| 30 min | Escalated flag. A notification channel is **not** built; it would be a new service/connector, a separate decision. |

**Recovery:** the first strict read clears the flag, logs the duration, and resumes normal reconciliation
(pending liquidation first).

**Options for position management while unknown:**
- **A. Status quo:** management stopped; relies on any resting broker stop.
- **B. Protect the last confirmed qty after N cycles:** a stop placed only when no own sell is
  OPEN/UNKNOWN/in flight and open orders are listable. Cancelled and confirmed on the next readable cycle
  before any liquidation re-sell.
- **D. Deliberate halt after M min.** Note that launchd `KeepAlive` restarts it, so a halt needs a
  distinct exit code plus a supervisor rule, which is itself a settings change.

**Remaining gap under every option:** while unreadable, an unprotected residual (for example after
liquidation cancelled its stop) has **no bound on duration or loss**.

## D10 — legacy "breaker marked today + residual + no pending liquidation" at cutover

- An offline script **cannot establish broker truth**.
- **Proposal:** at cutover, David supplies a **broker snapshot** exported at time T: the position qty,
  open orders with ids and client ids, and fills since the breaker stamp.
- A read-only script compares it with the DB (stamp, `position_state`, `liquidation_state`) and prints a
  report and a proposed resolution. **It writes nothing.**
- The snapshot is only true at T. The bot's first cycle re-reads strictly, and the resolution applies only
  if the live read still matches the snapshot's qty; otherwise it refuses and re-reports.

**Resolutions (David chooses; the breaker stamp is never cleared as a bypass):**
- **A. Leave:** manage the residual normally; the latch still blocks entries that day.
- **B. Resume liquidation:** a reviewed write creates a `liquidation_state` record (same reconcile-first
  machinery). This also liquidates any manual re-entry, so the snapshot must show whether one exists.
- **C. Refuse start** until A or B is recorded.

## Validation before any deployment

1. Offline suites green (including the chosen R1/D8/D10 tests).
2. Paper contract tests (D3/D4) of the client-id 404 semantics and **observed** visibility delays. These
   inform escalation thresholds only; they do not establish a maximum or a safe resubmit time.
3. Cutover dry-run with a real snapshot on the paper account.

**Rollback:**
- Redeploy the prior commit (live is still `6b43065`).
- The additive `liquidation_state` table is ignored by old code. But old code reintroduces the Stage 5
  defects, and any pending liquidation record would be ignored, so check for one before rolling back.

## Tested vs assumed

- **Tested offline:** everything named in `review/stage5_evidence/`.
- **Assumed (not tested):**
  - Alpaca's actual visibility delay, and 404/40410000 semantics;
  - spot crypto cannot be shorted (not relied on as a safety mechanism in R1);
  - SQLite power-loss durability;
  - no concurrent dashboard writes;
  - real clock/day transitions (the new-day test installs reset state manually).
