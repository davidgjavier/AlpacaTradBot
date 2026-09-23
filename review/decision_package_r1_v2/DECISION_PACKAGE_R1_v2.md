# Decision package R1 — v2 (for Codex/Gemini review, then David)

**Supersedes v1** (`review/decision_package_r1_v1/`, commit `94713ac`). **v2 changes are documentation
only** (§7).

**Frozen draft:** commit `c3744a9`, tag `draft-r1v2.1-frozen`. Hashes are unchanged and re-verified
(`frozen_sources_sha256.txt`):
- `crypto_trading_bot_R1v2.py` `b0a19726…`
- `d10_snapshot_check.py` `84a4f674…`

**The writer stays disabled (unbuilt) under every option.** Nothing here deploys anything, changes
trading behavior, or introduces a recovery policy.

## Two revisions, named explicitly

- **Integration revision:** `AlpacaTradeBot_integration_worktree`, branch `review/stage3-integration`,
  committed `crypto_trading_bot.py` `f48d7e71…`. This is what option 3 describes, and what the fake-broker
  tests exercise.
- **Live checkout:** `/Users/davidj/AlpacaTradeBot` at `6b43065`, `crypto_trading_bot.py` `08d4c028…`. It
  **does not contain** the Stage 5 liquidation code at all.
- **Nothing in this package verifies the behavior of the running bot.**

## 1. The three options

| | **1. Remove `not_placed`** (wait for positive reconciliation) | **2. Retain human-authorized `not_placed`** | **3. Leave R1 unadopted** (integration revision as-is) |
|---|---|---|---|
| **Status** | **Proposed only. Not implemented, and not tested as a complete variant.** The frozen draft still contains `not_placed`. | The frozen draft as-is. | The integration revision as-is. |
| What resolves an escalated attempt | Only positive evidence: broker visibility (terminal order) or a verified `found` (client id, symbol, side). | As option 1, **plus** a bound `not_placed` record (exactly one new attempt). | No escalation. A client-id 404 is treated as "never sent" and **resubmitted automatically**. |
| Duplicate-sell risk on accepted-but-hidden orders | The frozen draft's **core tests** show no new sell without positive evidence on the paths they cover (group A). **The complete option-1 variant is untested**, so no unconditional "no risk" claim. | **Demonstrated:** a correct-looking `not_placed` gives 2 submissions and a false completion (§3, group C). Requires a human action. | **Demonstrated in the integration revision:** an automatic second submission, with no human involved (visibility tests; 4 `expectedFailure`s). |
| A genuinely never-sent attempt | **Stays pending**, entries blocked, **with no exit in this workflow** (see §4: even a manual flatten does not resolve it). | An operator assertion can authorize one new attempt. **That does not by itself end exposure** (§4). | Resubmitted automatically on the next cycle. |
| Exposure while unresolved | Residual unsold, with no resting stop, **no bound on duration or loss** (D8). A manual flatten can remove the *inventory*; the workflow stays blocked. | Same until a successful reconciled execution; an operator error can cause a duplicate. | Short in the never-sent case, but the automatic duplicate risk applies. |
| Human dependency | Investigation; any exit from a never-sent state would need a **separate, not yet designed** policy. | Operator judgment is safety-critical; `by`/`evidence` are not authentication. | None. |
| Replaces the committed test `test_intent_persisted_but_never_sent_is_resubmitted_once` | Yes, with "escalate, never resubmit" (not written). | Yes (`review/drafts/proposed_replacement_for_5c_never_sent.py`). | No. |

## 2. Tests, separated (frozen draft `c3744a9`; fake broker; network blocked)

| Group | Meaning | Count | Result | Evidence |
|---|---|---|---|---|
| **A. Core safety invariants** | Specific invariants of the frozen draft (outcome matrix, bounds/restart, metadata, `found` binding + unrelated-order counterexample, D10, visibility). They support options 1 and 2 **only for the paths they cover**. | 48 | 48 pass | `A_core_safety.txt` |
| **B. `not_placed` path** | Option 2 only | 4 | 4 pass | `B_not_placed_path_option2_only.txt` |
| **C. Intentional risk** | **Asserts undesired behavior on purpose** (2 submissions, pending=false). Not an acceptance test. | 1 | reproduces the risk | `C_documented_risk.txt` |
| **D. Manual-flat scenario (new evidence)** | Never-sent escalated attempt + broker position set to 0 (models a manual flatten) | probe | **pending stays true; no breaker completion** | `codex_manual_flat_*`, `manual_flat_rerun_*` |

**These are reorganized evidence, not 53 new tests.** Group D is a probe, not an added test. **No code
or tests changed in v2.**

## 3. The demonstrated risk (option 2)

The original sell is accepted but not visible. A correctly bound `not_placed` passes the contradiction
checks (client-id 404, absent from open orders, position unchanged). A second sell is submitted and fills,
and the liquidation completes while the first sell is still outstanding. **Negative lookups do not prove
non-placement.**

## 4. Recovery, exposure, validation, rollback (corrected)

**Manual flatten does NOT resolve an unknown order (v1 was wrong).** v1 claimed that under option 1,
manual broker action is an exit, "after which the bot reconciles from positions". **That is false for the
frozen draft.**
- **Codex's reproduction:** a never-sent escalated attempt, broker position set to 0, another cycle →
  `broker_qty 0.0`, `pending_after_manual_flat true`, `breaker_completion_marks []`.
- **Re-run here:** the same result, and still pending after three more cycles spanning +24 h. Escalation
  is still `NOT_FOUND_BY_CID`; no breaker mark.
- **Why:** the unresolved-identity check deliberately runs **before** positional completion, so an
  unknown order is never treated as resolved by a zero position.
- A manual flatten can remove inventory exposure in this simulation, but the liquidation workflow stays
  pending (entries blocked).
- **Getting out of that state would require a separate recovery policy, which this package does not
  propose.** Clearing records as a workaround is not proposed either.

| | Option 1 | Option 2 | Option 3 |
|---|---|---|---|
| **Recovery** | Broker visibility or a verified `found` only. A never-sent attempt has **no exit** in this workflow; a manual flatten removes inventory but leaves it pending. | As option 1, plus `not_placed`. **Operator action alone does not guarantee an exit.** It only authorizes one new attempt. Exposure ends only after that attempt is submitted, **executes, and reconciles**. The attempt can itself fail, be rejected, or become hidden and escalate again. | None needed by design; the automatic resubmit carries the duplicate risk. |
| **Validation before adoption** | Implement the variant (reject `not_placed`; remove `authorized_attempt`); write and pass its tests (group A + an inverted group C + a manual-flat expectation) **and** decide the never-sent exit policy; full committed suites; paper contract tests (D3/D4). | Groups A + B; C recorded as an accepted risk; an operator runbook and writer review (identity/authentication, stale records, CAS); the same suites and paper tests. | — |
| **Rollback** | Revert to the prior commit. **First check for pending escalated records**: old code would resubmit them automatically. The additive fields are ignored by old code. | Same, plus: applied `not_placed` / `authorized_attempt` fields are ignored by old code. | — |
| **Residual risks** | Indefinite pending with no exit; D8 exposure; non-atomic gate; no CAS; clock jumps; the `found` contract is untested against a broker; **variant untested**. | Option 1's (except "variant untested"), **plus** the demonstrated duplicate on operator error, and no guaranteed exit even after a correct authorization. | The automatic duplicate on delayed visibility, and completion with an unresolved sell (integration revision). |

## 5. Interactions

- **D8** (alert/protection while unresolved) is most acute under option 1.
- **D10** uses the D10 check.
- **D7** defines the account key.
- **D3/D4** paper tests are required before any deployment.
- **A never-sent exit policy** is needed for option 1 (and as a fallback for option 2). It is not designed
  here.

## 6. Questions for Codex/Gemini review

1. Does anything in group A depend on the `not_placed` path?
2. What minimal, reviewable policy could exit a never-sent attempt **after** a manual flatten without
   treating a zero position as resolving an unknown order?
3. Under option 2, what evidence would make an authorized attempt's success verifiable, rather than
   assumed?
4. Is leaving the integration revision's automatic resubmit acceptable while nothing is deployed?

## 7. Changes from v1 (documentation only)

- **Corrected** the option-1 "manual action, then reconcile from positions" claim, with reproduction
  evidence (group D).
- **Option 1** is marked proposed/unimplemented/untested as a complete variant; the unconditional no-risk
  wording is removed.
- **Option 2:** operator action does not guarantee an exit.
- **Option 3** names the integration revision explicitly; running-bot behavior is not verified; the live
  checkout doesn't contain Stage 5 code.
- **The A/B/C classification is preserved.** Group C is still explicitly intentional-risk. Group D
  (evidence) is added.
