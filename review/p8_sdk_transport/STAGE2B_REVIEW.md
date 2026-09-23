# Stage 2b — Durable submission ownership, end-to-end submit budget, lock release disabled (OFFLINE)

- **Branch:** `review/p8-sdk-wrapper`
- **Commits:**
  - `7e8d3f2` — tests only
  - *fix commit* — see `git log`
- **Baseline:** `b228258` (Stage-2 fix `900d511`).
- **Codex reproduction used verbatim:**
  `…/work/p8_stage2_review/reviewer_checks.py` (sha256 `9fa2f8753f8efe38`). Codex's copies were
  byte-identical to `b228258`.

## Verdicts (tested offline scope only)

| # | Finding | Baseline | After |
|---|---|---|---|
| 1 | Ownership was gateway-local: a 2nd gateway (same process, client and DB) abandoned and released while A's POST was in flight; the order arrived later | Reproduced (ABANDONED, lock false) | **Fixed.** Durable `post_inflight=1` is set in the intent-creating transaction and cleared only by the POST worker when the request really ends. Visible to every gateway and process. Codex script: abandon UNRESOLVED, lock true |
| 1b | Other process; owner crash (`SIGKILL`) | Reproduced (abandon allowed) | **Fixed.** The marker survives the crash. No lease, expiry or owner disappearance counts as proof. A failed completion write also leaves the marker set |
| 2 | Submit budget not end-to-end | 0.05 s → 0.332 s; 0.6 s → 3.03 s; DB lock → 10.34 s | **Fixed.** One monotonic Budget covers the POST wait, DB writes (sqlite busy timeout capped to the remaining budget), reconcile reads and scan pages. Exhausted → UNRESOLVED, reconcile deferred. Codex script: 0.016 s |
| 3 | Lock release enabled operationally | Released by default | **Disabled by default** (`LOCK_RELEASE_ENABLED=False`); the lock is held. The mechanism is testable offline only via explicit opt-in |

An `ok:` POST outcome recorded by the worker also refuses abandon.

## Budget accounting

- Returns within `submit_deadline_s` plus overhead (tests allow 0.10 s; observed ≤ ~0.016 s).
- POST wait = remaining − 0.05 s final-write reserve − 0.01 s scheduling margin.
- Reads get `min(configured, remaining − reserves)`.
- DB busy timeout = `min(5 s, remaining − margin)`. A timed-out write → `persisted=False`, with the
  known outcome still returned.
- A budget below the reserves → 0 s POST wait; the POST is still sent; the result is UNRESOLVED.
- A timed-out request can still complete. Its worker clears the marker and records the outcome as
  blocking evidence only.

## Integration conflict found and resolved

The first version made the worker append a `POST_FINISHED` history event. That broke the Stage-2
invariant that a late response never mutates intent state or history. The worker now writes **only**
the ownership columns (no state, no `updated_ns`, no event). The existing expectation is kept, not
changed.

## Tests (identical fixtures)

| Suite | Before (`b228258`) | After |
|---|---|---|
| `test_p8_stage2b.py` (8; real subprocess and SIGKILL) | **8 fail** | **8 pass** |
| `test_p8_round4.py` (10) | 10 pass | 10 pass |
| Stage-1 (85) | 85 pass | 85 pass |

- **Total: 103/103.**
- **Stability:** 3 combined single-process runs; Stage-2b 5/5; reversed order; no orphan processes.
- **Changed fixtures (documented):** `make3`, `make3b` and round-3b `gateway()` opt into
  `lock_release_enabled=True`, because they test the release mechanism. The default-off behavior is
  asserted by `test_p8_stage2b.D`.
- **Changed assertions: none.**

## Remaining risks (open)

- **Liveness:** an owner crash mid-POST leaves `post_inflight=1` indefinitely. Abandon is refused and
  entries stay locked until a reviewed resolution exists (not designed; policy unapproved).
- **Worker completion write:** uses the store's default busy timeout. If the DB stays locked, it fails
  and the marker stays set (safe side).
- **Carried:**
  - shared `requests.Session` across threads (untested on real network/TLS);
  - DNS stalls untested;
  - deadline values unvalidated;
  - human-resolution policy UNAPPROVED;
  - CONFLICT and `ACCEPTED_UNVERIFIED` workflows;
  - normalization assumptions (P3);
  - intent ↔ `cbf1b9b` accounting;
  - paper-plan corrections.
- **Stages 3–5:** not started. Waiting for the saved integration brief.
