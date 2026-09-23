# Decision package R1 — v1 (for Codex/Gemini review, then David)

**Frozen draft:** commit `c3744a9`, tag `draft-r1v2.1-frozen`. No draft code changes after this point.

**Hashes** (`frozen_sources_sha256.txt`):
- `crypto_trading_bot_R1v2.py` `b0a19726…`
- `d10_snapshot_check.py` `84a4f674…`
- `R1v2.patch` `b9622fa2…`
- committed `crypto_trading_bot.py` `f48d7e71…`
- committed `db.py` `b60823e0…`

**Scope:** liquidation-attempt reconciliation under delayed broker visibility (R1). D8, D10 and D3/D4 are
separate decisions, but they interact (§5). **The writer stays disabled (unbuilt) under every option.**
Nothing here deploys anything or changes trading behavior.

## 1. The three options

| | **1. Remove `not_placed`** (wait for positive reconciliation) | **2. Retain human-authorized `not_placed`** | **3. Leave R1 unadopted** (committed bot as-is) |
|---|---|---|---|
| What resolves an escalated attempt | Only positive evidence: broker visibility (terminal order), or a verified `found` (client id, symbol, side match). | As option 1, **plus** a bound `not_placed` record (exactly one new attempt). | Nothing is escalated. A client-id 404 is treated as "never sent" and **resubmitted automatically**. |
| Duplicate-sell risk on accepted-but-hidden orders | **None from this path.** No new sell without positive evidence (tested). | **Demonstrated:** a correct-looking `not_placed` gives 2 submissions and a false completion (§3, test C). Requires a human action. | **Demonstrated, automatic, no human involved:** committed bot, visibility tests (4 `expectedFailure`s); 2 submissions. |
| A genuinely never-sent attempt | Pending **until broker evidence appears; possibly indefinitely.** Entries blocked. | Recoverable by an operator assertion. | Resubmitted automatically on the next cycle (fast). |
| Exposure while unresolved | Residual unsold, with no resting stop, **no bound on duration or loss** (D8). Possibly indefinite. | Same, until an operator acts; then ends, with duplicate risk if wrong. | Short in the never-sent case, but the duplicate risk above applies. |
| Human dependency | Investigation only; no authorization path. | Operator judgment is safety-critical; `by`/`evidence` are not authentication. | None. |
| Code needed beyond the frozen draft | Small: reject `kind=not_placed` unconditionally; remove the `authorized_attempt` path. Invert test C (§4). | None (frozen draft). | None. |
| Replaces the committed test `test_intent_persisted_but_never_sent_is_resubmitted_once` | Yes, with "escalate, never resubmit". | Yes, with "escalate; one attempt after a `not_placed`" (`review/drafts/proposed_replacement_for_5c_never_sent.py`). | No; the test stays. |

**Common to options 1 and 2** (the frozen draft's core):
- no resubmit and no completion while unresolved;
- persistent escalation at ≥ 3 checks **and** ≥ 900 s (draft values; escalation thresholds, **not safety
  proofs**);
- `found` verified against broker identity;
- a validated D10 snapshot check.

## 2. Tests, separated (frozen draft `c3744a9`; fake broker; network blocked)

| Group | Meaning | Count | Result | Evidence |
|---|---|---|---|---|
| **A. Core safety invariants** | Must hold under **options 1 and 2**: outcome matrix, bounds/restart, missing metadata, `found` binding + end-to-end unrelated-order counterexample, D10 validation/chronology/gate, visibility | 48 | **48 pass** | `A_core_safety.txt` |
| **B. `not_placed` path correctness** | Meaningful **only under option 2**: exactly-once authorization, crash after apply, replay rejection, rejected-then-valid | 4 | **4 pass** | `B_not_placed_path_option2_only.txt` |
| **C. Documented risk** | **Asserts undesired behavior on purpose:** `not_placed` on a hidden accepted order gives 2 submissions and pending=false | 1 | reproduces the risk | `C_documented_risk.txt` |

- **Option 1:** group B is removed; group C is **inverted** into a safety test (1 submission, still
  pending, `not_placed` rejected).
- **Option 2:** A + B must pass; C remains a **known accepted risk**, not an acceptance criterion.
- **Option 3:** none of the draft tests apply. The committed bot's visibility tests stay
  `expectedFailure`.

## 3. The demonstrated risk (option 2)

The original sell is accepted by the broker but not visible. An operator writes a correctly bound
`not_placed` record. The bot's contradiction checks all pass: client-id 404, absent from open orders,
position unchanged. It submits a second sell, the second fills, and the liquidation is marked complete
while the first sell is still outstanding. **Negative lookups do not prove non-placement.** A stronger
evidence rule (the full broker order history for the cid plus account activities) narrows the risk but
remains an assertion.

## 4. Implications

| | Option 1 | Option 2 | Option 3 |
|---|---|---|---|
| **Recovery procedure** | Investigate with broker evidence; if the order is found, write `found` (future reviewed tool); otherwise **wait**. Manual action at the broker (outside the bot) is the only way out of a truly never-sent attempt, **after which** the bot reconciles from positions. | As option 1, or a `not_placed` record (future tool, separately reviewed). | None. |
| **Validation before adoption** | Implement the rejection; groups A + inverted C pass; full committed suites; paper contract tests (D3/D4) of 404 semantics and **observed** delays (these inform thresholds; they don't prove a maximum). | Groups A + B pass; C documented as an accepted risk; the same suites and paper tests; an operator runbook + writer review (identity, authentication, stale-record/CAS protection). | — |
| **Rollback** | Revert to the prior commit. The additive DB fields are ignored by old code. **Check for pending escalated records first**: old code would resubmit them automatically. | Same, plus: records with an applied `not_placed` / `authorized_attempt` would be ignored by old code. | — |
| **Residual risks** | Indefinite pending (liveness); D8 exposure; non-atomic gate; no CAS; clock jumps; the `found` contract is untested against a real broker. | All of option 1's, **plus** the demonstrated duplicate on operator error. | The **automatic** duplicate on delayed visibility (committed-bot gap); the completion-with-unresolved-sell gap. |

## 5. Interactions

- **D8** (alert/protection while unresolved) matters most under option 1, where pending may be long.
- **D10** (legacy cutover) uses the D10 snapshot check under any adoption.
- **D7** defines the account key used by D10 and assumed by `found`.
- **D3/D4** paper tests are required before any deployment, under any option.

## 6. Questions for Codex/Gemini review

1. Is any path in the frozen draft able to submit a second sell **without** an operator record
   (group A's claim)?
2. Under option 1, is "manual broker action + positional reconciliation" a sound exit? What does the
   bot do after a manual sell?
3. Is the group split correct, i.e. does anything in group A depend on the `not_placed` path?
4. For option 3: is leaving the committed automatic-resubmit behavior acceptable in the interim, given
   that nothing is deployed?

**Decision for David:** option 1 · option 2 · option 3. Each is a policy choice; none is implemented by
this package.
