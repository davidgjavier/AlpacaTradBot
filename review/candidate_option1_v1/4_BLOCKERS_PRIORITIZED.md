# Remaining blockers for returning to PAPER operation (prioritized)

**Synthetic tests establish neither real broker behavior nor an economic edge.** The live checkout
(`6b43065`) currently contains **none** of the Stage 3–5 fixes.

## A. Execution correctness

1. **Order identity for non-liquidation orders.** Entry buys, exit market sells and protective/trailing
   stops have no client order id.
   - A lost response or delayed visibility can give **duplicate entries** (exposure beyond sizing) or
     duplicate exits.
   - Needs the P8 intent/identity layer (built and tested offline; integration gated on D7–D10) or an
     equivalent extension.
2. **R1 policy decision** (Option 1 candidate / Option 2 / Option 3), **plus** the never-sent recovery
   policy (proposal §2, e.g. the TIF lever).
3. **D8:** behavior while a position is unreadable, and protection of an unresolved residual. There is
   currently no bound on duration or loss.
4. **D3/D4 paper contract tests** (plan §3): 404 structure, visibility delay, duplicate client id, cancel
   timing, IOC/TIF finality, partial fills. These inform R1/D8 and the recovery policy.
5. **D10 cutover handling** of legacy "breaker marked + residual" state (snapshot-based check drafted).
6. **D7** account key: needed by D10 and by the `found` account binding.
7. **Remaining original-audit FAILs on the integration revision (25).**
   - Execution-relevant: protection during a historical-data outage; BTC residual instrument precision;
     negative residual treated as flat; the concurrent state writer; failed stop replacement losing the
     prior floor; pending crypto entry duplicate (related to #1).
   - Equity-bot items (NVDA latch, EOD retry, cancel/fill race): outside this branch.
8. **Concurrency:** no compare-and-swap on `liquidation_state`/`position_state`; a single bot process is
   assumed but not enforced.
9. **Merge and deployment path.** The integration branch isn't merged, and no deployment plan or rollback
   rehearsal exists (rollback: check for pending escalated records first).

## B. Strategy profitability (separate question; nothing here addresses it)

1. **D5 paper-plan revision:** sizing; the P5/P6 shortfall noted earlier.
2. **No evidence of an edge.** The 480 synthetic scenarios were stress tests, not backtests. Fees and
   slippage estimates are unvalidated on fills.
3. **Scalp strategy** remains paused; re-enabling is a separate decision.

## Decisions needed from David (in order)

1. **R1:** adopt the Option 1 candidate (this experiment) for further review · Option 2 · Option 3.
2. **Never-sent recovery:** accept "pending + entries blocked" as the safe state (possible indefinite
   halt), or pursue the IOC/TIF lever pending paper tests.
3. **D3/D4:** approve a dedicated paper account and executing the paper plan (credentials handled by
   David).
4. **D8, D10, D7.**
5. **Scope of order identity (#A1):** integrate P8 (with D7–D10), or extend client ids to all orders.
