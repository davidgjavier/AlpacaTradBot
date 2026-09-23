# P8 wrapper — Round 3b: evidence quality in human resolution; persistent uniqueness uncertainty

- **Branch:** `review/p8-sdk-wrapper` · Offline only; nothing wired into any bot, deployed, or broker-connected.
- **Commits:**
  - `27ed154` — tests only, including Codex's reproduction
  - *fix commit* — see `git log`
- **Baseline for "before":** `83ef87c` (round-3 fix `344dcb2`).
- **Codex reproduction used verbatim:**
  `…/work/p8_round3_review/reviewer_reproduction.py` (sha256 prefix `4de046470390e4db`).
  Codex's copy of the wrapper and tests was byte-identical to `83ef87c`.

## Verdicts (tested scope only)

| Item | Reproduced on baseline | Verdict |
|---|---|---|
| **Codex case:** all broker reads fail, yet abandon → ABANDONED and release → lock false | Yes | **Fixed.** Every reconcile reports `evidence`: `POSITIVE`, `NEGATIVE_COMPLETE` (structured 404 **and** a complete bounded scan with no match), or `INCOMPLETE`. Abandon and release force the scan and require `NEGATIVE_COMPLETE` **and** a persisted fresh check. Verbatim script after the fix: abandon and release both refused (UNRESOLVED), **lock still held** |
| Failed, truncated, bounded or stuck pagination during human checks | Yes (4 cases) | **Fixed.** Each is `INCOMPLETE`, so the action is refused. History, monitoring and the lock are preserved |
| Fresh check not persistable; operator write failure | Fresh-check case reproduced; write-failure cases already safe | **Fixed / guarded.** A fresh check with `persisted=False` refuses the action. A failed operator write keeps state, lock and history |
| ACCEPTED with `uniqueness_verified=False` left monitoring and cleared the lock | Yes (4 cases, including restart) | **Fixed.** New state **`ACCEPTED_UNVERIFIED`**: the order id is known, but a complete scan hasn't confirmed uniqueness. It's monitored, locks entries, is re-scanned every cycle, and survives restarts. A later complete scan gives `ACCEPTED`; a found duplicate or mismatch gives `CONFLICT`. It can't be abandoned |
| Operator actions racing reconciliation or a new order | Already safe on baseline (compare-and-set) | **Guarded by tests.** Abandon racing a concurrent discovery doesn't overwrite; release racing a late-order CONFLICT doesn't release |

**Important limit, stated plainly:** `NEGATIVE_COMPLETE` is a *successful negative observation*, not proof
that exposure is absent. It is only a precondition that lets an operator decision be recorded. The
proposed human-resolution policy (ROUND3_REVIEW.md) remains **UNAPPROVED**. Its 24 h / 1 h waits are
**not implemented** and are not treated as evidence.

## Tests (identical fixtures before and after)

| Suite | Before (`83ef87c`) | After |
|---|---|---|
| `test_p8_round3b.py` (19) | **13 fail**, 6 pass | **19 pass** |
| `test_p8_round3.py` (21) | 21 pass | 21 pass |
| `test_p8_round2.py` (15) | 15 pass | 15 pass |
| `test_broker_io.py` (30) | 30 pass | 30 pass |

- **Total after:** 85/85.
- **Changed expectations: none** this round.
- **The 6 baseline passes are guards:**
  - the successful-negative control (abandon, then release, still works);
  - release write failure;
  - abandon write failure;
  - abandon racing a discovery;
  - release racing a late order;
  - a later complete scan resolves to ACCEPTED. This one is trivial on the baseline, which never held
    the order in the first place.
- Evidence: `round3b_evidence/`.

## State model delta

| State | Monitored | Entries locked | Exits |
|---|---|---|---|
| **ACCEPTED_UNVERIFIED** (new) | yes | yes | complete scan with one match → ACCEPTED; >1 or mismatch → CONFLICT. No operator exit (not abandonable) |

Abandon (from UNRESOLVED) and release (from ABANDONED) additionally require the fresh check's
`evidence == NEGATIVE_COMPLETE` and `persisted == True`.

## Caller requirements (delta)

- Treat `ACCEPTED_UNVERIFIED` as "order exists" for position logic, **and** keep entries locked (via
  `entries_locked()`).
- Read `SubmitResult.evidence`. Only `NEGATIVE_COMPLETE` is a successful negative observation, and even
  that isn't proof of absence.

## Remaining risks (open)

- **Inherent broker race:** between a `NEGATIVE_COMPLETE` check and the operator write, an order can
  appear at the broker without any local caller having recorded it. Abandon or release then succeeds.
  Only continued monitoring (late order → CONFLICT, re-lock) mitigates this. No local mechanism can
  close it.
- **Liveness:** on a busy account where the 3-page bound is always hit, `ACCEPTED_UNVERIFIED` never
  becomes `ACCEPTED`, and there's no human path for it (by design this round). Needs a reviewed
  resolution workflow, like CONFLICT.
- **Load:** a forced scan on every human action; a re-scan every cycle for `ACCEPTED_UNVERIFIED`.
- **Carried open:**
  - normalization assumptions (unverified until P3);
  - CONFLICT resolution workflow;
  - intent ↔ accounting integration with Codex `cbf1b9b`;
  - broker behavior unobserved;
  - human-resolution policy **unapproved**.
- **Timeout hardening (round 4): OPEN.** Design constraint recorded for that round: *timing out a
  thread join does not cancel the in-flight HTTP request.* The request can still complete late, so a
  submit abandoned at the deadline must be treated as UNRESOLVED (never "not sent"), and the late
  response must not mutate state out of order.
- **Paper-plan corrections: OPEN.** P7-RO (read-only) and P3 (submits orders) remain separate, pending
  decisions.

## Next bounded task (proposal)

Round 4 (offline): timeout/deadline hardening under the constraint above. Cover wall-clock budgets,
late completion after deadline, `read()` never exceeding its budget, and loopback slow-drip and
stalled-connect tests.
