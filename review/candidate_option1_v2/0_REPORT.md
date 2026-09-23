# Option 1 candidate — review folder v2 (offline; NOT adopted; nothing deployed)

- **Baseline verified:** HEAD `37b346f`, clean; frozen draft `c3744a9` and the candidate unchanged; live
  `6b43065` untouched (its 22 pre-existing uncommitted files left alone).
- **No candidate code change this round.** Candidate `947df4cb…`, frozen draft `b0a19726…`, committed bot
  `f48d7e71…`, db `b60823e0…`. Hashes: `sources_sha256.txt`.

## Concise diff (this round)

| Added | Purpose |
|---|---|
| `codex_regressions/test_option1_candidate.py` (`b3a609a9…`) | Codex's six regressions, **verbatim** |
| `tests/test_codex_option1_regressions.py` (`26067ac1…`) | Gated wrapper (`CANDIDATE_OPT1=1`); adds only the gate |
| `review/paper_contract/harness_v1/paper_harness.py` (`08a3668d…`) + tests (`c4c872d1…`) | **Offline** paper harness: no default transport, no bot/credential/network imports |
| `1_RECOVERY_PROPOSAL_v2.md`, `2_PAPER_TEST_PLAN_v2.md`, `3_ORDER_IDENTITY_MAP.md` | Corrected proposal and plan; new identity map |

## Test results (all SYNTHETIC: fake broker / fake transport; no broker observations)

| Suite | Result |
|---|---|
| Codex's six regressions: frozen draft / candidate | **5 fail + 1 control pass / 6 pass**. Reproduces Codex exactly, including the legacy `authorized_attempt` bypass (`2 != 1` on the frozen draft). |
| All relevant suites vs candidate (97) | **91 safety pass** (85 earlier + 6 Codex) · 4 changed expectations fail as predicted · 1 risk demo no longer reproduces · 1 known conflict · **0 skipped · 0 unexpected** |
| Committed bot (unchanged) | tests/ 155: 81 pass + 4 expected failures + 70 gated skips · p8 103 → OK |
| Paper harness (offline) | **21/21**: 19 new-feature tests + 2 defect characterizations of plan v1's cleanup rule |

**Harness first attempt** (`harness_tests_first_attempt.txt`): 17/20. All three failures were my **test
fixture** errors, not harness defects:
- two "lost" orders were still findable, so the harness correctly resolved them by positive
  reconciliation;
- one fixture sell was $30 notional, which the harness correctly refused over the $25 cap.

The fixtures were corrected, and a positive-reconciliation counterpart test was added.

**Changed expectations:** none new this round. The 4 Option-2 `not_placed` behaviors and the risk demo are as
in v1. The known conflict (automatic resubmit after never-sent) is unchanged and preserved.

## Unresolved risks

- **Indefinite pending** under Option 1 when the broker never surfaces an attempt (inventory, identity and
  entries kept separate; recovery proposal v2).
- **Order-identity gap** for entries, market exits and stops (16 sites without a client id). **Duplicate
  entry** (exposure up to 2× sizing) is the most severe.
- **Broker assumptions untested:** 404 structure, visibility delay, duplicate-client-id rejection,
  cancel timing, terminal finality, partial fills (may not be reproducible on paper).
- **Harness limits:** a SIGKILL during a submission is covered only by journal replay. The `CANCEL_UNKNOWN`
  and anomaly states need human reconciliation (by design).

## Evidence categories

- **Synthetic vs broker:** everything in this folder is synthetic. There are **no** broker observations.
  Paper tests weren't run; no credentials or calls.
- **Execution correctness vs profitability:** this work addresses execution correctness only. **Nothing
  here says anything about an economic edge.**
