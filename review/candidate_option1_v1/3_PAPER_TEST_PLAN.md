# Minimal broker-connected PAPER test plan (NOT executed; requires David's separate approval: D3/D4)

**Purpose:** test the broker assumptions the offline suites can't. Paper results characterize the paper
environment only; **they don't establish live behavior or a maximum delay.**

## Isolation

- **Separate script** in `review/paper_contract/` (to be written only after approval). It doesn't import
  the bot and has no launchd entry. It runs in the foreground by David, and Ctrl-C stops it.
- The trading bot services and their settings are **not** touched, and scalps stay paused.
- **A dedicated paper account (D3), not the bot's.**
- **Credentials:** David supplies them via the environment at run time. Claude doesn't read or store keys,
  and the script redacts keys in all logs.

## Account verification (abort if any fails)

- The base URL is the **paper** endpoint.
- `GET /account`:
  - status ACTIVE, crypto enabled;
  - `account_number` equals the value David typed at launch;
  - `id` hashed prefix matches the D7 key.
- **Preflight:** no open orders and no BTC position; otherwise abort. Don't clean up someone else's state.

## Limits and stop conditions

- **Symbol:** BTC/USD only.
- **Limits:** ≤ **$25** notional per order; ≤ **$100** aggregate buys; ≤ **20** orders total; ≤ **30 min**
  wall clock.
- **Stop immediately on:**
  - any fill not predicted by the test step;
  - any position larger than the test's own buys;
  - account/URL mismatch;
  - an error outside the expected set;
  - a limit breach;
  - failure to confirm cleanup.

## Tests (each records raw request/response JSON, monotonic and wall timestamps)

| ID | Assumption tested | Method (all orders use a unique `client_order_id` prefix `pt-<run>-`) |
|---|---|---|
| P1 | Structured not-found | `GET order` for a random uuid; `GET by client id` for a random cid; `GET position` when flat. Capture HTTP status, `code` (expect 40410000?), and body. |
| P2 | Client-id lookup + visibility delay | Submit a **limit buy far below market** (won't fill). Immediately poll by cid, by id, and the open-orders list at 0/50/100/200/500 ms/1/2/5 s. Record first-visible time per method. Repeat ×5. |
| P3 | Duplicate client id | Re-submit with the same cid as P2's resting order. Is it rejected, and with what code? **If the broker enforces uniqueness, that is the strongest available duplicate guard.** |
| P4 | Cancel timing | Cancel P2 orders. Record the status sequence (pending_cancel → canceled) and timing via id and cid. |
| P5 | Terminal-status finality / IOC semantics | Buy a small qty (market, ≤ $25), then an **IOC limit sell far above market**. Expect immediate canceled/expired, with filled_qty 0. Re-check status and position over 60 s for any late change. Is `time_in_force=ioc` accepted for crypto? |
| P6 | Partial fills (best effort) | An IOC limit sell at/near the bid for a qty larger than top-of-book, if paper simulates depth. **Paper may not produce partials; if not, record that the assumption remains untested.** |
| P7 | Market sell reconciliation | Market-sell the P5 inventory. Poll order and position until consistent; record the lag. |
| P8 | Lost-response simulation (client side) | Submit with a client-side timeout shorter than the response. Reconcile by cid only (no resubmit). Record whether and when it becomes visible. |

## Cleanup (always, also on stop)

- Cancel every open order with prefix `pt-<run>-`, and confirm via the list.
- Market-sell any BTC bought by this run.
- Confirm flat and no open orders.
- **If cleanup can't be confirmed:** stop, preserve all evidence, and alert David. Don't retry blindly.

## Evidence

`review/paper_contract/<run>/`, containing:
- redacted raw JSON per call;
- a timeline CSV;
- a per-test verdict (observed / not observed / inconclusive);
- the account verification record;
- the cleanup confirmation;
- script sha256.

Reviewed by Codex before any conclusion is drawn.
