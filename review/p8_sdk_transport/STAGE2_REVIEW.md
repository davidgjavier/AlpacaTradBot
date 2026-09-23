# Stage 2 — Timeout/deadline hardening (P8 wrapper) — OFFLINE

- **Branch:** `review/p8-sdk-wrapper`
- **Commits:**
  - `7589d63` — tests only
  - *fix commit* — see `git log`
- **Baseline:** `127b6d0` (round-3b fix `a190fef`). Stage-1 fixes and their 85 regression tests are
  preserved and unchanged.
- **Isolation:** nothing wired into any bot, deployed or broker-connected.

## Design (as implemented)

Constraint honored: **timing out a thread join does not cancel the HTTP request**, which may still
complete later.

1. **`CallRunner`:** each broker call runs in a daemon worker; the *caller* waits at most a
   wall-clock budget. On timeout the caller gets `DEADLINE`. The worker keeps running, and its late
   result is **discarded**: only caller threads write intent state, so a late response can never
   mutate state out of order. Workers inherit the caller's thread name, for tracing.
2. **Bounded workers:** `MAX_INFLIGHT_CALLS = 8`, counting abandoned-but-running workers.
   Saturation fails closed:
   - reads → `UNAVAILABLE`;
   - submits → `NOT_SUBMITTED` with **nothing sent and no intent created**. The worker slot is
     reserved *before* the intent row.
3. **`read()`:** every attempt gets only the remaining wall-clock budget, and backoff never sleeps
   past it. A slow-drip response that the socket inactivity timeout can't catch is now bounded by the
   caller deadline.
4. **Submit:** a single POST on the reserved worker with `SUBMIT_DEADLINE_S` (proposal: 15 s).
   `DEADLINE` → **UNRESOLVED** (never "not sent"), then reconciled by the same client id.
5. **In-flight tracking (integration fix):** the gateway tracks client ids whose POST worker hasn't
   finished. **Abandon and release refuse while the id is in flight**, checked before and after the
   fresh broker check.

Socket timeouts, (connect, read), remain as a second layer: they eventually free workers except on
slow-drip.

## Results (identical fixtures)

| Suite | Before (`127b6d0`) | After |
|---|---|---|
| `test_p8_round4.py` (10, real wall-clock and loopback) | **7 fail** (6 failures, 1 error); 3 pass | **10 pass** |
| Stage-1 suites: 30 + 15 + 21 + 19 = 85 | 85 pass | **85 pass, unchanged** |

- **Total: 95/95.**
- **Stability:**
  - 5 combined single-process runs of all 93 tests (before the integration addition), all passing;
  - 3 combined runs of all 95;
  - Stage-2 suite 10/10 twice (before and after the integration fix);
  - reversed module order passes.
- **Changed expectations: none.**
- **Baseline passes (guards):**
  - backoff truncation (already correct);
  - late-response discard (trivial on a baseline that never returns early);
  - abandon allowed once the worker finished (control for the in-flight refusal).

**Baseline failures, measured:**
- a 2 s hang returned OK against a 0.4 s budget;
- slow-drip still blocked after 4 s;
- submit blocked 1.22 s against a 0.3 s deadline;
- a lookup hang blocked 2.03 s;
- a stalled POST blocked 1.02 s;
- no bounded runner existed;
- **abandon succeeded while the POST was still in flight.**

## Integration failures found (not assumed away)

1. **Thread identity.** Moving calls into workers would have broken per-caller attribution; a
   round-2 fixture keys on thread name. Resolved by name inheritance. A production concern too (logs
   and tracing).
2. **In-flight POST vs human resolution (new, real).** A deadline-abandoned POST can land *after* an
   operator's NEGATIVE_COMPLETE check. Reproduced (abandoned while in flight), then fixed (in-flight
   refusal), with a control test.
3. **Missing import** (`threading`): caught by the first combined run.

## Remaining risks (open)

- **Process-local knowledge only.** After a crash or restart, in-flight tracking is lost. A POST sent
  just before a crash can still land after an operator check. Only monitoring (late order → CONFLICT,
  re-lock) covers this.
- **Shared `requests.Session` across threads.** An abandoned POST worker and new calls can use the
  same SDK session concurrently. urllib3 pools are thread-safe; `requests.Session` isn't formally
  guaranteed. Untested against real network or TLS.
- **Worker leak under persistent slow-drip:** bounded by `MAX_INFLIGHT_CALLS`, then fail-closed. The
  system stays safe but stops trading until workers finish.
- **DNS resolution stalls:** bounded for the caller by the runner, but the worker may run long.
  Untested (offline).
- **Deadline values are proposals** (8 s read, 15 s submit, 8 workers). No latency data yet; P7-RO or
  P3 would inform them.
- **Carried:**
  - human-resolution policy UNAPPROVED;
  - CONFLICT and `ACCEPTED_UNVERIFIED` resolution workflows;
  - normalization assumptions;
  - intent ↔ accounting (`cbf1b9b`);
  - paper-plan corrections;
  - P7-RO and P3 pending.

## Next

Stages 3–5 (isolated integration, combined validation, paper-test preparation) were referenced by
the user's status update, but **no specification for them was found** in the message or the saved
files. Waiting for that spec before touching shared trading code.
