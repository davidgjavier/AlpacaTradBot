# Proposal (NOT implemented): recovery for a genuinely never-sent liquidation attempt under Option 1

**The core fact:** the bot **cannot distinguish** "never sent" from "sent, accepted, not yet visible". None
of the following is proof that a hidden order cannot execute:
- a flat position;
- repeated 404s (by client id or order id);
- elapsed time;
- absence from open-order lists;
- an operator's assertion.

**The broker may never provide positive evidence that an order does not exist.** Recovery therefore has
to be designed around what the broker *can* evidence, and around bounding how long an unseen order could
be executable.

Three separate problems. **Solving one does not solve the others.**

## (a) Removing inventory exposure

- **Available today, outside the bot:** David flattens at the broker (a manual sell).
  - It removes held BTC. It does **not** resolve the attempt; the liquidation stays pending (Codex
    reproduction, package v2 group D).
  - If a hidden original later tries to execute, the outcome depends on broker behavior: presumably
    rejection for insufficient balance, since spot can't short. **That is an assumption, not a safety
    mechanism.** If BTC is acquired again later, a hidden original could sell *that* inventory.
- **Not proposed:** a bot-initiated replacement sell without positive evidence. That is exactly Option 2's
  demonstrated risk.

## (b) Resolving the original order's identity

**Positive evidence that can resolve it:**
- the order becomes visible by client id or id with a terminal status;
- a broker-verified `found`;
- an account-activity/fill record tied to the order id.

**Evidence the broker may not provide:** a definitive "this client id was never accepted". A 404 now
doesn't exclude later visibility, and **how long the broker can take to surface an accepted order is
unknown**.

**Proposed lever (for review): bound the executable lifetime of every liquidation attempt.**
- Today the liquidation sell uses `time_in_force=GTC`, so a hidden order could, in principle, stay
  executable indefinitely.
- Submitting liquidation sells as **IOC** (Alpaca crypto lists `gtc` and `ioc`; **unverified**) would
  mean an accepted order either executes immediately or is canceled by the broker.
- An attempt then becomes a candidate for "cannot execute any more" **after** its lifetime plus a margin.
  But **only if** the paper tests show that IOC crypto orders are honored and that a hidden accepted IOC
  cannot execute later.
- **Even then this is broker-semantics evidence, not proof.** It would still need David's approval as a
  recovery rule.
- **Tradeoffs:**
  - IOC market orders may fill partially and cancel the rest; that's L2 (sell the remainder on positive
    terminal evidence).
  - Liquidity gaps become more cancel/resubmit cycles.

**Resolving identity by elapsed time alone, without the TIF lever, is not proposed.**

## (c) Permitting future entries

Entries are **blocked while any attempt is unresolved** (implemented and tested), even across day rollover
and restart. Options, for David:
1. **Keep blocked until (b) resolves.** The safest option. It can mean **indefinite halt** of the bot for
   that symbol.
2. **Allow entries after (a), with the hidden-order risk accepted explicitly.** A late execution of the
   unseen sell could then reduce a *new* position unexpectedly, leaving position state, stops and P&L
   inconsistent. Not recommended without the TIF lever.
3. **Allow entries after (b) via the TIF lever**, once validated by paper tests and approved.

**Not proposed:** clearing pending records, clearing the breaker stamp, or treating a flat position as
resolution.

## Where recovery needs evidence the broker may not provide

- A definitive non-existence answer for a client id.
- A maximum visibility delay.
- Finality of terminal statuses (a late fill after "canceled").
- Account ownership of a client-fetched order (D7).

Where these can't be obtained, the safe state is **pending with entries blocked**. The cost of that is
liveness.
