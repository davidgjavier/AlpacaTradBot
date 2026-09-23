# Option 1 candidate — report v1 (experiment for comparison; NOT adopted)

- **Baseline (verified before starting):** package `ff6793b` (clean); frozen draft `c3744a9` (tag
  `draft-r1v2.1-frozen`). The draft and all original tests are unchanged (no diff). Live checkout
  `6b43065` (`08d4c028…`) is untouched; its 22 pre-existing uncommitted files are not mine and were left
  alone.
- **Candidate:** `review/candidates/option1/crypto_trading_bot_opt1.py` (`947df4cb…`).
  - Diff vs frozen draft: `opt1_vs_frozen_draft.patch` (`9898bc2d…`).
  - Diff vs committed bot: `opt1_vs_committed_bot.patch` (`3cd35fa7…`).
- **Tests:** `tests/test_candidate_opt1.py` (`c2b7b2a9…`). Gated: `CANDIDATE_OPT1=1 DRAFT_R1V2=1 DRAFT_R1=1
  BOT_SOURCE=review/candidates/option1/crypto_trading_bot_opt1.py`.

## 1. What changed from the frozen draft (3 edits)

1. **`not_placed` removed.** Any `not_placed` record is rejected and consumed to history. The
   authorizing branch is deleted, not merely unreachable.
2. **An attempt with no identity always escalates**; there is no authorized exception.
3. **The submit path refuses any record carrying `authorized_attempt`** (e.g. written by Option-2 code).
   It never sells on an authorization.

Only broker visibility of the attempt as terminal, or a broker-verified `found` (client id, symbol and
side must match), resolves an attempt.

## 2. Every remaining resubmission path

**Inside the liquidation workflow (candidate):** a new liquidation sell is submitted only after all of the
following hold: its own last attempt is resolved by **positive** broker evidence; every other open sell is
cancelled **and** confirmed gone; and a post-reconciliation strict read shows quantity > 0.0001 BTC.

| # | Path | Evidence relied on | Residual assumption |
|---|---|---|---|
| L1 | First attempt | no prior attempt | — |
| L2 | Own attempt **TERMINAL** (canceled/rejected/expired, any fill) → sell the remaining confirmed qty | broker order status | a terminal status is final (no late fill), **unverified** (paper test P5) |
| L3 | Own attempt **FILLED** but position still > 0 (inventory from elsewhere) → sell the remainder | broker order status + position read | position reads are accurate and current |
| L4 | Verified `found` → the reconciled order is terminal → as L2/L3 | client id, symbol and side of the fetched order | a client-bound order belongs to this account (D7) |
| L5 | Own attempt OPEN while the position reads flat → cancel it, then re-check; sell only if still > flat | cancel + re-check | — |

**Removed:** `not_placed` (human assertion + negative lookups). **Never a path:** client-id 404, repeated
404s, elapsed time, a flat position, or an operator assertion.

**Outside the liquidation workflow (unchanged; NOT governed by R1).** These sites have **no client order
id** (only the liquidation sell, the scalp Target-1 IOC and one scalp stop re-placement do). A lost
response or delayed visibility there can't be reconciled by identity, and a later cycle may submit again:
- **exit market sells:** hung-stop emergency, both trail-breaches, time-decay, sell signal;
- **entry buys** (both paths): potential **double exposure beyond sizing** if a buy is accepted but not yet
  visible in the position;
- **protective and trailing stops.**

This is execution-correctness blocker #1 (§ BLOCKERS). The P8 broker-I/O wrapper was designed for it;
integrating it is gated on D7–D10.

## 3. Test results (fake broker; offline)

**Candidate tests (15)** cover:
- A: a hidden accepted original (never a second sell or false completion; a bound `not_placed` is
  rejected);
- B: an intent saved but never sent (pending, entries blocked, `escalated` + an `ESCALATED` log);
- C: zero position with an unknown order (not reconciled; never-sent and hidden);
- D: the original later appears working / partially filled (working) / filled / partial-then-canceled
  (sells only the remainder) / canceled / rejected;
- E: restart **and** a manually installed new-day state in 5 unresolved states (hidden, never-sent,
  in-flight, UNKNOWN lookups, NO_IDENTITY): no sell, no entry, still pending;
- F: position, open-order and lookup failures during recovery (no action, then recovery on positive
  evidence);
- G: a late original fill after an operator's wrong "never placed" conclusion (one sell total); manual
  flatten, then the original reported rejected (resolves, no new sell).

| Run | Result |
|---|---|
| Before, on a byte-identical copy of the frozen draft | **13 pass, 2 fail.** Both failures are the `not_placed` path double-selling. **Correction:** commit `3498b2e`'s message and my previous status said "16 tests / 14 pass". The file has **15** tests; the true before-state is 13 pass / 2 fail. |
| After, on the candidate | **15/15**, three runs |

The 13 before-passes are **genuine**: the frozen draft already had those safety properties. Option 1's
difference is precisely the `not_placed` path.

**All relevant suites against the candidate (91 tests)**, classified against **predictions fixed before
the run** (`classification_summary.txt`, `classification.json`, `all_suites_vs_candidate_verbose.txt`):

| Class | Count | Tests |
|---|---|---|
| Safety assertions passing | **85** | Committed Stage 5 (except the known conflict), draft R1/D10/resolution tests outside the `not_placed` path, visibility, the 15 candidate tests |
| Changed expectations, failing as predicted | **4** | G4 `valid_not_placed…authorizes_exactly_one_new_attempt`; E `authorization_is_exactly_once…`; E `crash_after_applying_resolution…`; E `rejected_resolution_stays_rejected…` (its second, valid `not_placed` step) |
| Intentional-risk demonstration, no longer reproducing | **1** | `R_DocumentedRisk` (the double sell is gone) |
| Known conflict, failing as predicted | **1** | Committed `test_intent_persisted_but_never_sent_is_resubmitted_once` (expects an automatic resubmit; same as the frozen draft) |
| Skipped / unexpected | 0 / **0** | |

**Why each changed expectation changes:**
- The 4 changed tests assert Option-2 behavior: that a valid `not_placed` authorizes one new attempt,
  exactly once, surviving a crash. Option 1 removes that authorization entirely, so those assertions
  can't hold.
- Their Option-1 counterparts are the candidate tests A2/G1: the record is rejected and there's no second
  sell.
- The conflicting committed test expects an automatic resubmit after a never-sent intent. Both R1 options
  deliberately replace that with escalation.

**The proposed replacement test** (`review/drafts/proposed_replacement_for_5c_never_sent.py`) is Option-2
specific (its operator step authorizes a sell). Under Option 1 its replacement would be candidate test B1.

**Committed bot (unchanged):** tests/ 149 (81 pass + 4 expected failures + 64 draft/candidate-only skips)
+ p8 103 → OK.

## 4. What these tests do not establish

- **Real broker behavior:** visibility delay, 404 semantics, terminal-status finality, partial fills,
  cancel timing, time-in-force behavior.
- **Profitability.**
- The new-day tests install reset state by hand; they don't exercise a real clock transition.
