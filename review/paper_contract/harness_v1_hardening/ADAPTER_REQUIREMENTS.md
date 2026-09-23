# Paper harness v1.1 — implemented guards vs. future broker-adapter requirements

**Status:** offline only. There is **no broker adapter**, and none may be written or connected without
separate review and David's explicit approval (D3/D4, a dedicated paper account).

## Implemented in `paper_harness.py` v1.1 (tested offline with a fake transport)

| Guard | Test(s) |
|---|---|
| No submission unless the position is **confirmed and fresh**; stale after any submission or observed fill | R1 ×2, H3, H5 |
| qty / price / limit / notional finite, > 0, non-boolean | R2 (8 cases), original N4 |
| Deadline enforced **at the submission boundary**; separate bounded cleanup allowance | R3, H6 ×2 |
| Returned order must match client id, symbol and side; otherwise an anomaly, state unchanged | H1 (3 cases) |
| Cumulative fill finite, 0 ≤ filled ≤ qty, monotonic; otherwise an anomaly, state never zeroed | H2 (6 cases) |
| Journal header binds run/account/symbol; a mismatched replay is refused; **deadline start survives restart** | H4 ×2 |
| Single-writer lock (flock) for the harness lifetime | H4 |
| The attempt is journaled and fsynced **before** the transport call; a persistence failure refuses with no state change | H6 |
| Unknown-order invariant, budget and cleanup reserve, cleanup rules, graceful/forced termination | original suite (21) |

**Journaling is not behavior-neutral.** A journal-write failure **blocks** submission (fail closed). That is
a deliberate liveness cost.

## Future broker-adapter requirements (NOT implemented; must be satisfied before any activation)

1. **Durable identity.** Every request carries the harness-generated `client_order_id`, which is journaled
   before the call. The adapter never generates, reuses or retries an id.
2. **No submission retries** at any layer (HTTP adapter, SDK, proxy). Reads may retry within a bounded
   budget. This must be proved by configuration inspection **and** a test against the real client object
   (not a mock).
3. **Bounded requests.** Connect and read timeouts on every call. **A local timeout never cancels remote
   execution:** a timed-out submit is **UNKNOWN**, which the harness already handles by blocking everything.
4. **Account binding.** Before any call, and again before each submission:
   - verify the broker account (id/number and paper endpoint) against `account_key` from the journal
     header;
   - on mismatch, refuse and stop. `account_key`'s definition is D7.
5. **Single writer across processes and hosts.** flock covers one host and filesystem. A networked
   filesystem or a second host needs a different mechanism, not provided here.
6. **Restart deadlines.** The adapter must use the journal header's `start_wall`, never "now at restart".
   Clock jumps (NTP) can shorten or lengthen the effective window; a monotonic, persisted guard isn't
   possible across reboots.
7. **Order/fill field mapping.** Broker strings, e.g. `filled_qty` "0.0001" and symbol "BTCUSD" vs "BTC/USD",
   are converted by the adapter. The harness validates the result. Unmapped fields must surface as
   anomalies, not defaults.
8. **Credentials.** Supplied by David at run time. Never logged; redacted in evidence. Not read by Claude.

## Explicitly NOT claimed

- Real broker behavior (visibility delay, 404 structure, duplicate-cid rejection, cancel timing, finality,
  partial fills).
- Any bound on the real wall-clock duration of requests.
- Profitability.
