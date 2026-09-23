# Recovery proposal v2 — genuinely never-sent liquidation attempt (Option 1 candidate). NOT implemented.

**Supersedes v1** (`candidate_option1_v1/2_RECOVERY_PROPOSAL_NEVER_SENT.md`). **Correction:** v1 proposed an
IOC "lever" after which an attempt could become a candidate for "cannot execute any more" after its lifetime plus
a margin. **That inference is withdrawn.**
- The time since the client *attempted* a submission doesn't bound when an unknown request was **accepted,
  processed or finalized** at the broker.
- Observed IOC outcomes (e.g. "canceled within N ms" in paper tests) describe orders the client *saw*. They
  say nothing certain about an order it never saw.
- A no-change observation window can't prove finality for an unseen order.
- **No resubmission deadline is inferred from IOC behavior, elapsed time, or any observation window.**

## The candidate's rule is unchanged

**Positive reconciliation is required.** An attempt is resolved only by the broker showing it **terminal**
(filled / canceled / rejected / expired), or by a broker-verified `found`.

**A found working order is NOT terminal.** `found` only binds the attempt to an order id. That order must
still reconcile, and while it is OPEN or partially filled the liquidation waits. Never a replacement sell.

## Three problems, kept separate

| | What it means | Available now | What would resolve it | Evidence the broker may never provide |
|---|---|---|---|---|
| **(a) Inventory exposure** | BTC still held | David flattens manually at the broker (outside the bot). The bot never replaces an unresolved sell. | Positions confirmed at zero | — (positions are observable) |
| **(b) Order identity** | Whether the unresolved attempt exists, can execute, or has executed | Wait for positive evidence; investigate by client id, order id and account activity | Broker shows the attempt terminal, or a verified `found` that later reconciles terminal | **A definitive "never accepted"**; a maximum visibility delay; finality guarantees for unseen orders |
| **(c) Permission for new entries** | Whether the bot may open positions again | Entries blocked while any attempt is unresolved (implemented and tested, across restart and rollover) | (b) resolved, or a separately approved policy | — |

- Solving (a) does **not** solve (b) or (c) (Codex's manual-flat reproduction). A later execution of the unseen
  order could sell inventory acquired afterwards.
- **Never treated as proof:** a flat position, repeated 404s, elapsed time, absence from open-order lists,
  observed IOC behavior, or an operator assertion.

## Unresolved tradeoff: indefinite pending

If the broker never surfaces the attempt, **(b) never resolves**, and under the candidate **(c) stays
blocked indefinitely** for that symbol. That is the stated liveness cost of Option 1. It is **not** solved
here and needs David's decision among:
1. accept a possibly indefinite halt, with human investigation;
2. a separately reviewed policy that permits entries under a **named, accepted** hidden-order risk (not
   recommended; it would need its own review);
3. an **independently justified broker contract** (e.g. documented Alpaca guarantees on client-id
   visibility or order-lifetime finality), with a reviewed policy built on it.

**Option 3 is not something paper tests can establish; they can only fail to contradict it.** Changing the
liquidation order's time-in-force (GTC today) may still be worth reviewing *to reduce exposure duration for
visible orders*, but it is **not** a basis for resolving unseen ones.
