# P8 wrapper — Round 3: unreadable history, identity, duplicates, pagination, human resolution

- **Branch:** `review/p8-sdk-wrapper` · Offline only; nothing wired into any bot or deployed.
- **Commits:**
  - `a15ade0` — tests only
  - `fdb6505` — fixture correction and baseline re-run
  - *fix commit* — see `git log`
- **Baseline for "before":** `bb2a2f8` (end of round 2).

## Verdicts (tested scope only; nothing verified against Alpaca)

| Item | Reproduced on baseline | Verdict |
|---|---|---|
| **Codex defect:** `submit()` with the same client id while the store is unreadable returned `NOT_SUBMITTED`, `exposure_may_exist=False`, although the broker held the accepted order | Yes | **Fixed.** Unreadable prior history never yields `NOT_SUBMITTED`. The gateway keeps the client id, doesn't POST, and uses a read-only broker lookup: an identity match gives `ACCEPTED`; a mismatch gives `CONFLICT`; otherwise `UNRESOLVED`. All results carry `persisted=False`. `NOT_SUBMITTED` now requires *readable, empty* history plus a failed insert (control test) |
| Identity validation, with normalization and qty-vs-notional | Yes: side, qty, limit-price and notional mismatches were all accepted | **Fixed.** `identity_mismatches()` compares client id, symbol (`BTC/USD` = `BTCUSD`), side, type, TIF, and limit/stop by numeric value (`"100000.00"` = `100000.0`). A notional intent matches on notional and ignores qty; a notional intent with no broker notional is `CONFLICT`. A qty intent matches on qty, and any broker notional is `CONFLICT`. Also applied to the POST response |
| Conflicting identity or several broker orders for one client id | Yes: accepted silently | **Fixed.** Any mismatch or more than one broker order gives `CONFLICT` with all order ids recorded. It stays monitored and locks entries |
| Bounded pagination (positive evidence only) | Yes: an order on page 3 was never found | **Fixed.** Cursor pagination (`after`, ascending, 500 per page, **max 3 pages**, 1 µs overlap with dedup by id). Truncation, a failed page, no progress, or the bound all mean `complete=False`, and are **never** treated as absence. `uniqueness_verified` is reported |
| Human-resolution mechanism | n/a (didn't exist) | **Implemented offline (mechanism only):** `acknowledge`, `abandon`, `release_entry_lock`, `entries_locked`, and an append-only `intent_events` history. The policy is proposed below, for review |
| Automatic resubmission | — | Still **disabled** |

## Test results (identical fixtures before and after)

| Suite | Before (`bb2a2f8`) | After |
|---|---|---|
| `test_p8_round3.py` (21) | **15 fail** (9 failures, 6 errors from the missing human-resolution API); 6 pass | **21 pass** |
| `test_p8_round2.py` (15) | 15 pass | **15 pass**, 1 expectation changed |
| `test_broker_io.py` (30) | 30 pass | **30 pass**, unchanged |

- **The 6 baseline passes are guards:**
  - the `NOT_SUBMITTED` control;
  - normalized and notional orders must still match (prevents over-strict validation);
  - exhausted or failed pages are not absence (already true since round 2);
  - resubmission stays disabled.
- **Changed expectation (1):** `test_p8_round2.F5b.test_pre_post_read_failure_is_not_submitted` expected
  `NOT_SUBMITTED` for unreadable history. That was Codex's defect. It now expects `UNRESOLVED`, exposure
  possible, `persisted=False`, and still **0 POSTs**.
- **Fixture correction (before the fix, baseline re-run):** the page-failure fixture failed only one
  HTTP call, which the wrapper's bounded read retry would simply retry. It now fails every attempt for
  that page. The baseline result was unchanged: 15/21 fail.
- Evidence: `round3_evidence/`.

## State model (round 3)

| State | Monitored | Entries locked | Leaves by |
|---|---|---|---|
| SUBMITTING | yes | yes | POST outcome or reconcile |
| UNRESOLVED | yes | yes | positive matching evidence → ACCEPTED; mismatch or >1 order → CONFLICT; operator → ABANDONED |
| CONFLICT | yes | yes | **human only** (no automated exit implemented) |
| ABANDONED | **yes** (late-order detection) | **yes, until `release_entry_lock`** | a late order → CONFLICT (re-locks) |
| ACCEPTED / REJECTED | no | no | terminal for this layer |

Every transition and operator action appends to `intent_events`. Nothing is deleted.

## Caller requirements (additions to round 2)

- Gate new entries on `entries_locked(symbol)`, not on local state. It **fails closed**: an unreadable
  store locks.
- `CONFLICT`: stop automated trading on the symbol; alert; don't act on either order automatically.
- `ABANDONED`: exposure may still exist; still locked unless explicitly released; late orders re-lock.
- `ACCEPTED` with `uniqueness_verified` False or None means the bounded scan couldn't confirm there's
  only one order. Treat it as accepted, but surface it in monitoring.
- Any result from unreadable history: keep the client id; never retry with a new id; no replacement
  order.
- Run `recover_pending()` every cycle. It now includes `CONFLICT` and `ABANDONED`.

## Proposed operational policy for human resolution (FOR REVIEW — not adopted)

1. **Acknowledge** means "I have seen it". It changes nothing else. Use it on any alert to stop repeat
   paging if alerting supports that. Monitoring and the entry lock continue.
2. **Abandon** only when **all** of the following hold. The mechanism enforces only (a) and (b); (c)–(e)
   are policy:
   - (a) the intent is UNRESOLVED (not CONFLICT);
   - (b) a fresh automated check found no broker order;
   - (c) the operator checked the Alpaca dashboard order history **and** account activities (fills) for
     the symbol since the intent's creation, and recorded what was checked in the note;
   - (d) at least N hours have passed since `last_submit_ns` (proposal: 24 h, to exceed any plausible
     visibility delay);
   - (e) no position change is unexplained by other recorded orders.
3. **Release entry lock** is a separate, later decision, never in the same step as abandon. Proposal:
   - require a second check at least M hours after abandon (proposal: 1 h);
   - record the dashboard evidence in the note;
   - for the live account, require a second person or explicit written user approval.
4. **CONFLICT** is never cleared by these actions. It needs a separate reviewed resolution (not
   implemented): reconcile the broker orders and fills manually, and decide flatten/keep outside the bot.
5. **Monitoring never stops** for ABANDONED or CONFLICT intents. A retention and archival policy (for
   example after 30 days with a verified flat position and no open orders) needs a separate decision.
6. **Automatic resubmission stays disabled.** Any replacement order is a new decision, with a new
   client id, made by a human after abandon **and** release.

## Remaining risks (open)

- **Timeout and deadline hardening** (inactivity vs wall-clock, DNS/TLS, `read()` overrun): **OPEN**.
- **Paper-plan corrections** (sizing, the P5/P6 shortfall): **OPEN**. A separate read-only P7 subset is
  proposed in `P7_READONLY_SUBSET_PROPOSAL.md`; P3 (submits orders) stays separate. Neither is approved.
- **Scan bound:** a duplicate beyond 3 pages (~1,500 orders since creation) is undetected; the order is
  reported `ACCEPTED` with `uniqueness_verified=False`. Load: up to 3 list calls per monitored intent
  per cycle.
- **Normalization assumptions (HYPOTHETICAL until P3):** how Alpaca reports symbol, decimals,
  qty/notional and type/TIF for crypto orders. Over-strict matching would produce false CONFLICTs; the
  tests only cover the forms modeled.
- **Human-action concurrency:** CAS makes abandon fail if a concurrent reconcile changed the state, but
  no multi-thread test covers operator actions. A SUBMITTING intent can't be abandoned until a
  reconcile has moved it to UNRESOLVED.
- **CONFLICT resolution workflow:** not implemented.
- **Exactly-once accounting:** Codex fix `cbf1b9b` (separate repo, not deployed); integration with
  intent ids is not designed yet.
- **Unobserved broker behavior:** duplicate client ids, 404 semantics, visibility delay.

## Next bounded task (proposal)

**Round 4 (offline): timeout/deadline hardening.**
- Wall-clock per-call deadline (worker thread with a hard join timeout, or an overall budget).
- `read()` never exceeds its deadline.
- Tests for slow-drip responses and stalled connects on loopback.

Then the paper-plan revision, including the sizing fix and the assets-derived increments from P7-RO.
