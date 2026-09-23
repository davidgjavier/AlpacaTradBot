# P8 wrapper — Round 2: ambiguous submissions and duplicate-order prevention

- **Branch:** `review/p8-sdk-wrapper`, worktree `/Users/davidj/AlpacaTradeBot_p8_worktree`
- **Scope:** `review/p8_sdk_transport/proposed/broker_io.py` and its tests. **Offline only.**
- **Isolation:** nothing wired into any bot, nothing deployed, no broker contact. Codex's Target-1
  accounting fix is untouched (it lives on `review/p0-1-breakeven-guard`).

| Commit | Content |
|---|---|
| `eddcdda` | Baseline: the wrapper as saved 2026-09-23 07:24 (`broker_io.py` sha256 `fd58a0a3…`, tests `9460be21…`), byte-identical |
| `e0bbf5f` | Round-2 regression tests only (12), reproducing the findings; wrapper unchanged |
| *(this commit)* | Fix, plus 3 self-review tests, 3 changed expectations in the existing suite, evidence, and this document |

## Findings — verdicts within the tested scope

"Fixed" means: reproduced on the baseline with the listed fixtures, and passing after the fix with
the same fixtures. It does **not** mean verified against Alpaca.

| # | Finding | Reproduced on baseline | Verdict |
|---|---|---|---|
| 1 | `resubmit()` had no atomic ownership check | Yes. Two concurrent callers gave **3 POSTs** (2 extra), and the duplicate 422 marked the intent REJECTED. A concurrent first `submit()` raised a raw UNIQUE-constraint error. A stale reconcile **overwrote ACCEPTED** | **Fixed (offline).** Only the caller whose INSERT created the intent may POST; a losing caller gets the existing state with 0 POSTs. All state changes are compare-and-set, so terminal states can't be overwritten. `resubmit()` never POSTs |
| 2 | A later 400/422 could mark an earlier, possibly accepted order REJECTED and stop reconciliation | Yes. A duplicate-id 422 (HYPOTHETICAL broker behavior) after a hidden accepted order gave REJECTED and removed the intent from the queue | **Fixed (offline).** REJECTED is allowed only from SUBMITTING with `submit_attempts == 1` (the single POST). Any other 4xx is ambiguous. The intent stays queued and resolved to the original order id once visible |
| 3 | Negative lookup plus one list page after 30 s does not prove absence | Yes. Delayed visibility (120 s), a truncated 500-order page, and even "full" negative evidence each authorized a **second POST** | **Fixed (offline).** Negative evidence never resolves an intent. The list query is used only for *positive* evidence. `NOT_FOUND_AFTER_WINDOW` is retired (legacy rows are read as UNRESOLVED). Automatic resubmission is **disabled** (`RESUBMISSION_ENABLED = False`) |
| 4 | Recovered ACCEPTED results omitted the broker order id | Yes. `order_id` was missing on the reconcile, already-resolved, existing-intent and restart paths | **Fixed (offline).** `SubmitResult.order_id` is populated on every path, from the broker response or the stored row |
| 5 | A persistence failure after submission hid an accepted order or an ambiguous outcome behind an exception | Yes: 4 cases (accepted then record fails; ambiguous then record fails; found in reconcile then record fails; intent write fails pre-POST) | **Fixed (offline).** After a POST has been attempted, **no exception escapes**. Results carry `persisted` and `persistence_error`; known identity is always returned. A pre-POST failure returns `NOT_SUBMITTED`. Self-review found **unwrapped store reads** that could still raise after a POST (3 new tests, 3/3 failing on the first fix). Now wrapped |

## Test results (same fixtures before and after)

| Suite | Baseline `eddcdda` | After (this commit) |
|---|---|---|
| `test_p8_round2.py` (15) | **14 fail**, 1 pass* | **15 pass** |
| `test_broker_io.py` (30, existing) | 30 pass | **30 pass**, with 3 expectations changed (below) |

\* The baseline pass is `test_rejected_post_then_store_reads_fail_still_no_exception`. It guards a store
read that the round-2 fix itself introduced; the baseline 403 branch did not read the store.

**Fixture corrections made before the fix.** Two round-2 fixtures initially passed on the baseline for
the wrong reason:
- The truncated page used non-UUID ids, which the SDK rejected, so the list read errored.
- The stale-reconcile race stayed inside the window, where the baseline skips identical writes.

Both were corrected and re-run on the baseline **before** any wrapper change (see
`round2_evidence/round2_BEFORE_eddcdda.txt`).

**Changed expectations in the existing 30** (safety assertions kept):

| Test | Old expectation | New expectation | Reason |
|---|---|---|---|
| `C.test_intent_write_failure_means_no_post` | raises `PersistenceError` | returns `NOT_SUBMITTED`, `persisted=False` | Finding 5. **0 POSTs** still asserted |
| `C.test_record_failure_after_accepted_post_is_recovered` | raises `PersistenceError` | returns `ACCEPTED` with `order_id`, `persisted=False` | Finding 5. Row still SUBMITTING; recovery still gives ACCEPTED with 1 POST |
| `D.test_crash_before_post_restart_then_window_list_then_single_resubmit` | resubmit after 30 s POSTs once | **no POST**; stays UNRESOLVED and queued; `submit_attempts` stays 1 | Finding 3 and the resubmission-disabled policy |

## Caller-integration requirements (for any future wiring; none is wired today)

Callers must read both `SubmitResult.state` **and** `SubmitResult.persisted`.

| `state` | Meaning | Required caller behavior |
|---|---|---|
| `ACCEPTED` | Broker has this order | Use `order_id`. If `persisted=False`, keep `order_id` in memory and in position state; the intent row stays queued and `recover_pending()` will record it |
| `REJECTED` | The single POST was definitively refused (400/401/403/422 on attempt 1) | Not placed. The caller may decide a new action, **with a new client id** |
| `UNRESOLVED` | Outcome unknown. **Not "not placed"** | Treat as possible exposure: persist the client id in position state; block new entries for the symbol; don't size or place replacement or opposing orders on the assumption it doesn't exist; call `recover_pending()` or `reconcile()` every cycle; alert a human if unresolved beyond a bound. **Never** submit the same logical action under a new client id |
| `NOT_SUBMITTED` | Nothing was sent; intent not durable (or store unreadable) | Safe to retry later; treat as a storage fault; alert |
| `CONFLICT` | Broker shows an order that contradicts a stored terminal state | Stop automated action on the symbol; human review |

- **`persisted=False`** on any state means the store may be behind reality. The row remains in the
  recovery queue. Don't treat the lack of a saved record as the lack of an order.
- `recover_pending()` must run at startup and every cycle. If the queue itself is unreadable, it
  returns a single `UNRESOLVED` result with client id `"*"`; treat that as "all exposure unknown".
- `resubmit()` is **read-only** in this round. An intent that truly never reached the broker now stays
  UNRESOLVED indefinitely. Clearing it requires an **explicit human decision**. That workflow isn't
  implemented; see the next task.

## Remaining risks (open)

- **Timeouts and deadlines (explicitly deferred):**
  - the read timeout is an inactivity timeout, not a wall-clock bound;
  - DNS and TLS aren't covered or tested;
  - `read()` can overrun its deadline by one attempt;
  - the Stage A ≈ 61 s figure inherits all of these.

  See `../../claude_supplement_2026-09-23/P8_PROPOSAL_KNOWN_LIMITATIONS.md` §3 (in the main checkout).
- **Paper plan (explicitly deferred):** the P5 + P6 inventory shortfall (0.0008 sold vs about 0.000798
  held) and the other sizing items in `PAPER_TEST_PLAN_v2.md` are unchanged.
- **Identity verification:** a lookup or list hit is adopted by `client_order_id` alone. Symbol, side,
  qty and prices aren't compared with the stored payload. Multiple orders with one id (if Alpaca
  accepts duplicates) aren't detected; the first hit is taken. List pagination isn't handled (it's now
  harmless for safety because absence is never inferred, but a found-by-list result can still be
  missed).
- **Liveness cost of the safer policy:** an intent that never reached the broker blocks entries
  forever without a human decision. Correct under uncertainty, but operationally heavy.
- **Concurrency coverage:** threads and separate store instances on one SQLite file were tested, plus
  the earlier two-process restart. Truly concurrent processes on different hosts were not tested.
  SQLite `BEGIN IMMEDIATE` is relied on.
- **Broker behavior still unobserved:** duplicate client-id handling, 404/40410000 for an unknown
  client id, visibility delay (paper test P3).
- **Accounting:** linking an intent to position state and a trade row (exactly-once) is Codex's work
  item.

## Next bounded task (proposal)

**Round 3: identity verification and human-resolution workflow. Offline, same branch.**
1. On any lookup or list hit, compare symbol, side, type, qty and limit/stop price with the stored
   payload. A mismatch returns `CONFLICT`. More than one match returns `CONFLICT`.
2. Add `abandon(cid, operator_note)`: an explicit, audited human action that moves an UNRESOLVED
   intent to `ABANDONED` only after a fresh positive-evidence check fails. It still never POSTs.
3. Paginate the list query for positive evidence, with bounded pages.

After that: timeout/deadline hardening (the deferred item), then the paper-plan revision.
