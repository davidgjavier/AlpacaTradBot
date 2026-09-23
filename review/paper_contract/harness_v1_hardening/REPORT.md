# Paper harness hardening v1.1 (Codex review of 4ee799d) — offline, synthetic only

- **Baseline:** `4ee799d` (clean). Tests first: `de8968d`. The Option 1 candidate (`947df4cb…`), frozen draft
  and bot are unchanged.
- **No broker calls, credentials, deploys, restarts, settings changes or recovery-writer activation.**

## Codex-reproduced defects: reproduced here, then fixed

| # | Defect on `08a3668d` | Before | After |
|---|---|---|---|
| R1 | A buy with an **unknown position** (None, or after a failed read) reached the transport | 2/2 fail (submitted) | refused, 0 transport calls |
| R2 | **NaN / zero / negative / boolean price, and NaN / boolean / negative limit** bypassed the caps | 7/8 fail (submitted). The infinite price was already refused (genuine guard). | 8/8 refused, 0 calls |
| R3 | **Deadline only checked between `run()` steps**; a direct call at t=1900 > 1800 submitted | fail (submitted) | refused at the submission boundary |

## Hardening of the static concerns (new behavior; failed or errored on the old harness by design)

- **H1** Returned-order identity (client id, symbol, side).
- **H2** Cumulative fill validation (NaN, inf, negative, boolean, non-numeric, > qty).
- **H3** Boolean position: this was a **genuine behavioral failure before** (`True` read as 1.0).
- **H4** Journal bound to run/account/symbol, the deadline start preserved across restart, and a
  single-writer lock.
- **H5** A stale position after an intervening fill.
- **H6** A bounded cleanup allowance, and fail-closed journaling.

**Unknown states are always preserved.** They are never replaced with zero, inferred rejection, or
permission to retry.

## Results (all SYNTHETIC; fake transport)

| Suite | Before (`08a3668d`) | After (`a6492e25`) |
|---|---|---|
| Hardening (14 tests; subtests reported individually in the evidence) | 11 failures + 16 errors | **14/14 pass** |
| Original harness suite (21) | 21/21 | **21/21**, after setup-only changes |
| Stability | — | 3 runs, ResourceWarnings as errors |
| Committed-bot tests/ (155) | — | 81 pass + 4 expected failures + 70 skips (unaffected) |

## Changed expectations (setup only; **all 59 assertion lines identical**, verified by diff)

- **Fake transport:** returned orders now include `symbol` (real broker orders do). Without it, the new
  identity guard correctly treats the orders as unverifiable.
- **`refresh()` before each submission** (26 statements, 2 lambdas, 3 step functions). Required by the new
  fresh-position rule.
- **`account_key`** is now a required constructor argument.
- **`close()`** before a restart re-opens the same journal (single-writer lock).
- Full diff: `test_setup_changes.diff`. Harness diff: `paper_harness_v1_to_v1.1.diff`.

## Reporting corrections (from Codex's review)

- **"Committed suite 155 OK"** means **81 passes, 4 expected failures and 70 skips**. It is not 155
  independent safety checks.
- **"Duplicate entry up to 2× sizing"** was an **example** of one duplicated entry, **not a bound**.
  Repeated hidden-order cycles could compound further. No upper bound is proven.
- **Journaling is not behavior-neutral.** It changes persistence-failure behavior (fail closed), now tested
  (H6).

## Remaining blockers

- **A broker adapter doesn't exist** and must meet `ADAPTER_REQUIREMENTS.md`: no submit retries at any
  layer, bounded requests, account verification, cross-host single writer, and field mapping.
- **Pending decisions:** R1 · D9 · D7 (the account-key definition used here) · D3/D4 paper-test approval
  and a dedicated account.
- **Broker behavior is unobserved.** Profitability is not addressed.
