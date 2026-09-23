# R1 v2 — liquidation attempt reconciliation under delayed broker visibility

**Status:** DRAFT for independent (Gemini) review. **Not adopted.** Committed bot unchanged.

**Version:** R1 **v2.1** (Stage 5f hardening after Codex's review of 8ac5cba; supersedes v2 and v1). See §9 for what changed.

**Artifacts:**
- `review/drafts/crypto_trading_bot_R1v2.py` and `R1v2.patch`: diff against the committed bot.
- `review/drafts/d10_snapshot_check.py`: D10 snapshot check.
- Tests: `tests/test_draft_r1v2.py` (17) and `tests/test_stage5d_visibility.py` (5).
- `review/drafts/proposed_replacement_for_5c_never_sent.py`.

**How to run (fake broker, offline):**
```
DRAFT_R1V2=1 BOT_SOURCE=review/drafts/crypto_trading_bot_R1v2.py python3 -m unittest discover -s tests -t tests -p 'test_draft_r1v2.py' -v
DRAFT_R1=1  BOT_SOURCE=review/drafts/crypto_trading_bot_R1v2.py python3 -m unittest discover -s tests -t tests -p 'test_stage5d_*.py' -v
DRAFT_R1V2=1 BOT_SOURCE=review/drafts/crypto_trading_bot_R1v2.py python3 review/drafts/proposed_replacement_for_5c_never_sent.py
```

## 1. Problem (verified on the committed bot)

A liquidation sell is accepted, its response is lost, and it is temporarily invisible (client-id 404, absent
from open orders). The committed bot then **submits a second sell under a new client id**, and with a flat
position it **completes while its own sell is unresolved**. Evidence:
`review/stage5_evidence/visibility_committed_bot_UNMASKED.txt`, 5/5 fail.

## 2. Outcome matrix for our own attempt (what each state does)

| Lookup result | R1 v2 behavior | Escalates? |
|---|---|---|
| OPEN (visible, working) | wait; no resubmit; if the position reads flat, cancel it and re-check | **Never via R1.** A long-working order is a D8 alerting concern. |
| FILLED / TERMINAL | resolved; any escalation moved to `escalation_history` as `resolved_by=broker_visibility` | — |
| UNKNOWN (lookup failed) | UNRESOLVED: no resubmit, no completion | yes, at the bound |
| NOT_FOUND by order id | UNRESOLVED | yes, at the bound |
| NOT_FOUND by client id | UNRESOLVED (a 404 is never proof of non-placement) | yes, at the bound |
| attempt > 0, no order id and no client id | cannot reconcile | **immediately** (`NO_IDENTITY`) |

- All UNRESOLVED kinds share one counter; per-kind counts are recorded (`inflight.kinds`).
- **Missing `inflight` metadata** (records written by the committed or v1 code): reconstructed with
  `since = first observation` and `origin = reconstructed`. That can only lengthen the wait, never shorten
  it.

## 3. Bound semantics

- **Escalate once, when `checks ≥ 3` AND `elapsed ≥ 900 s`.** Both are inclusive; the values are drafts.
- Tested exactly:
  - 2 checks at 900 s → no escalation;
  - 3 checks at 899.999 s → no escalation;
  - 3 checks at 900 s → escalation.
- **These are escalation thresholds, not safety proofs.** Observed paper delays can inform the choice, but
  they cannot establish a universal maximum visibility delay. R1 v2 therefore **never resubmits
  automatically**, at any elapsed time.
- **Escalation persists** across restarts: tested unchanged after a restart, with no resubmit and no
  entry.
- It is cleared **only** by broker visibility (terminal) or a validated operator resolution. Never by
  elapsed time.

## 4. Operator recovery (specification; the bot enforces validation)

1. **Collect broker evidence** outside the bot: the order history searched **by client id** across all
   statuses since the attempt time, and the account fills/activities for the same window.
2. **Record exactly one resolution** in `liquidation_state.operator_resolution`, through a reviewed write
   (the tool is not built):
   - `{"kind": "found", "order_id": <id>, "by": <name>}` → the bot reconciles that order normally.
   - `{"kind": "not_placed", "cid": <the pending cid>, "evidence": <reference>, "by": <name>}` → the
     attempt moves to `abandoned_attempts`, and **exactly one new attempt** is authorized. The bot sizes it
     from its post-reconciliation strict read.
3. **The bot validates.** Missing fields or a cid mismatch → `rejected` is recorded, **nothing is sold**,
   and the escalation stays.
4. **Forbidden:** deleting or clearing liquidation records, clearing the breaker stamp, automatic
   replacement, or a resolution written by an agent without David's evidence.
5. **Tested:** `found`, valid `not_placed` (one new attempt, then complete), invalid → rejected with no
   sale.

## 5. D10 snapshot validation (draft `d10_snapshot_check.py`; read-only)

An offline script **cannot establish current broker truth**. The check validates a David-supplied snapshot:

| Check | Effect |
|---|---|
| Environment (paper/live), account key (definition is D7), symbol | BLOCK on mismatch |
| Freshness: age ≤ max (draft 600 s), not in the future, taken after the breaker day began | BLOCK |
| Fill history starts at or before the breaker day | BLOCK if incomplete |
| Position qty present | BLOCK if missing |
| Open orders: status known and actually open | BLOCK otherwise |
| Open orders: identity matched to the DB stop, TP, liquidation id or cid | FLAG if unrecognized |
| Buy fills since the breaker day | FLAG: re-entry (option B would liquidate it) |
| A DB attempt appearing in neither open orders nor fills | FLAG, explicitly "not proof it was never placed" |

- **Live gate at apply time:** the bot's strict qty and open-order ids must equal the snapshot, else it
  refuses. Unknown live reads also mean refuse.
- The breaker stamp is **never** cleared.
- Tested: 9 BLOCK reasons, the flags, a clean case, and the live gate.

## 6. Conflict with an existing test (preserved)

- `tests/test_stage5c_latch_and_crash.py::test_intent_persisted_but_never_sent_is_resubmitted_once`
  encodes an automatic resubmit. It **stays unchanged** and fails only under the draft (27 pass + this 1).
- **Proposed replacement** (`review/drafts/proposed_replacement_for_5c_never_sent.py`, passes on the
  draft): never-sent → escalates, no automatic resubmit → a valid `not_placed` resolution → exactly one
  new attempt.

## 7. Tradeoffs and remaining gaps (for the reviewer)

- **Liveness cost:** a genuinely never-sent attempt now needs operator action. Until then the residual is
  unsold, unprotected by R1, and entries are blocked. This is D8 exposure: **no bound on duration or
  loss**.
- **An operator error is still possible:** a wrong `not_placed` produces a second sell. The evidence
  requirement reduces but cannot eliminate it.
- **An OPEN order that never executes** is not escalated by R1 (D8 alerting is needed).
- **The resolution-writing tool is not built.** Tests write the record directly.
- **The D10 account key depends on D7.** The snapshot format assumes David can export these fields.
- **The injectable clock (`_r1_now`)** uses wall time. Clock jumps (NTP) can shorten or lengthen the
  elapsed time; the check count bound mitigates this but doesn't remove it.
- **R2** (route through the P8 broker I/O layer with its NEGATIVE_COMPLETE semantics) remains the
  longer-term alternative.

## 8. Questions for independent review

1. Is any path in §2 able to submit a second sell without operator authorization?
2. Should UNKNOWN (transport failure) share the NOT_FOUND bound, or escalate differently?
3. Is the `not_placed` evidence requirement sufficient, and what exactly must the evidence contain?
4. Are the D10 BLOCK conditions complete (e.g. partially filled open orders, fees, dust)?


## 9. v2.1 changes (Stage 5f) — after Codex's review of 8ac5cba

**Reproduced on the v2 draft by the tests:**

| Finding | Type | v2 | v2.1 |
|---|---|---|---|
| `found` pointing at an unrelated terminal order, original sell hidden | **END-TO-END failure** | **second sell submitted** | no sell, no completion |
| D10 accepts NaN, ±inf, negative, boolean qty | validation weakness | accepted | BLOCK |
| D10 accepts missing `open_orders`/`fills`, naive timestamps, malformed orders/fills, no completeness evidence | validation weakness | accepted | BLOCK |
| `live_gate` checks only qty + id set | validation weakness | wrong account/env/symbol, changed order details, stale reads all accepted | refused |
| Unbound, stale, replayed, boolean-attempt resolutions; rejected records left active | validation weakness | accepted / left active | rejected, consumed to history |

**Operator resolution, schema 2** (bound, consumed exactly once):
```
{"schema": 2, "kind": "found"|"not_placed", "attempt": <int == pending attempt>,
 "cid": <== pending client id>, "escalation_id": <== CURRENT escalation id>, "nonce": <unused>,
 "evidence": <non-empty>, "by": <non-empty>, "order_id": <found only>}
```
- Accepted only while the attempt is **escalated**. Every record moves to `resolution_history` as applied
  or rejected, and the active slot is cleared, so it is never re-evaluated.
- **Nonces are single-use.**
- A `not_placed` authorizes **exactly the next attempt number** (`authorized_attempt`). That
  authorization is consumed when the next intent is written, so it can't be reused by a later attempt
  (tested).
- **`found`:** the broker order fetched by id must have `client_order_id` == the pending cid, the same
  symbol, and side sell. **Assumption:** an order fetchable through this account's client belongs to this
  account; the order object carries no account id.
- **`not_placed`:** checked **freshly** at apply time. The client-id lookup must be a structured
  NOT_FOUND, the cid absent from open orders, and the position not reduced since the attempt. If any
  lookup is UNKNOWN, the record isn't consumed; it's retried next cycle.
- **Crash after applying:** exactly one new attempt (tested; the crash is asserted to fire).
- **Authentication:** `by`/`evidence` are **not** authentication. Records are assertions; the bot
  verifies what the broker can confirm. `not_placed` remains a **trust decision**, because absence can't
  be proven. **The writer tool does not exist and must not be built** until identity/authentication and
  stale-record protection are separately reviewed.

**D10 schema 2:**
- strict types, with finite non-boolean quantities;
- tz-aware timestamps;
- required lists, with every order and fill validated, including symbol;
- an explicit completeness attestation (`has_more: false`, integer pages, fill coverage from ≤ breaker-day
  start to ≥ `taken_at`), with every fill inside that window.

The completeness block is an **attestation**: it is checked for presence and consistency, not proof that
pagination really happened.

**`live_gate(snapshot, live, expected)`:**
- re-validates the snapshot, and requires the live read to be attested complete;
- account, environment and symbol must match **both** the expected configuration and the snapshot;
- the live read must be no older than the snapshot, not in the future, and ≤ 60 s old;
- quantities must be valid and equal, and **every authoritative order field** must be identical.

**Remaining gap, stated:** the gate and the action that follows are **not atomic**.

**Remaining risks and untested possibilities:**
- Wrong-but-plausible operator input: a `not_placed` for an attempt the broker simply hasn't surfaced
  yet, which passes all fresh checks, still produces a second sell. **Untested possibility; inherent in
  the trust decision.**
- A position reduced by an unrelated process makes `not_placed` fail closed (liveness cost).
- Account binding of `found` relies on the client being bound to one account. D7 defines the account
  key.
- An attested-but-false completeness block would pass.
- Clock jumps affect escalation timing.
- Unchanged: no bound on exposure duration or loss while waiting (D8).
