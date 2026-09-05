"""
Which specialist gets which exception -- and, just as load-bearing, which gets none.

**Routing is the point of having specialists at all.** One investigator over every
refusal spent its budget asking a payer register about arithmetic, and said so honestly
each time ("the engine refused on no_subset_fits, which is not a question about who
paid"). That is a correct answer and a wasted call. A router turns the refusal category
-- which the engine already produces, and which already names the layer that objected --
into the question worth asking.

**Two of the categories in the brief do not exist in this engine, and inventing them
would have been the easy mistake.**

`partial` is a ground-truth `Relation` and a `Reversal.partial` flag; it is not something
`match_once` can emit. A customer who short-pays surfaces as `no_subset_fits` -- the
search ran to completion and nothing summed to the credit, with the closest subset a few
hundred paise short. `bank_txn_0056` in the reported batch is exactly this shape at
`-498p`. So the routing key is `no_subset_fits`, and the investigator sent to it is the
one that can ask whether a credit note, a TDS deduction or a short payment explains the
gap.

`duplicate_reference` is a generator defect label (`duplicate_utr`), not a refusal.
Duplicate UTRs reach the engine as `multiple_candidates` or `contested_payment` -- ties,
both of them, and breaking a tie is the FIRST of the five things `docs/AGENTIC.md` says an
agent must never do. So duplicate-reference detection ships as a read (`get_bank_line`
returns the lines sharing a reference) that a specialist may consult while working some
other question, and never as a routing key. An agent can see the duplicate; there is no
path by which seeing it resolves the tie.

**The never-investigate set is not an optimisation.** Each member is a case where refusing
is the right answer and an agent's involvement could only make it worse:

    multiple_candidates      two subsets fit. The engine has identified a set, not an
                             answer, and an agent asked which is more likely will always
                             produce one -- a guess wearing the costume of analysis.
    ambiguous_grouping       the same thing one level up, at Layer 2b.
    contested_payment        two credits with equal evidence want the same payment.
    solution_cap_reached     so many combinations fit that the amount is evidence for
                             none of them.
    order_dependent_assignment
                             the permutation gate caught a match that depended on read
                             order. That is a defect in the matcher, not a missing
                             document, and it goes to an engineer.

`pool_exceeded` sits just outside that set and is the interesting boundary. The engine
declined to search because the window was too crowded, so there IS a question worth
asking -- but it is a question about the settlement window and the data in it, never
"which of these should we post". The bank specialist may assert a confirmed settlement
date, which re-anchors the window without widening it; it may not assert anything that
forces a match.
"""

from __future__ import annotations

# Refusal category -> the specialist roles to try, in order. First one to assert wins;
# a role that declines hands on to the next.
ROUTE: dict[str, tuple[str, ...]] = {
    # The amounts reconcile and the NAME does not. A counterparty-identity question:
    # who is this payer, and are they authorised to settle for the customer on the
    # invoice? That is the invoice specialist's, because the answer lives in the ledger
    # and the payer register beside it.
    "amount_name_conflict": ("invoice",),
    # The counterparty is right and the money is not. Two places the gap can be: netted
    # against the payment (a refund the ledger missed) or taken by the bank (a charge
    # outside the fee model). Payment first, because a refund is the commoner cause and
    # is checkable against a field the record already carries.
    "unexplained_residual": ("payment", "bank"),
    # The search ran to completion and nothing fits. This is where a short payment, a
    # credit note or an unrecorded TDS deduction lives -- all invoice-side -- and, if
    # none of those, a refund.
    "no_subset_fits": ("invoice", "payment"),
    # The engine declined to search. Data scope and settlement window only.
    "pool_exceeded": ("bank",),
}

# Categories no agent may be routed to, and why. Kept as a mapping rather than a set so
# the reason travels with the rule and a trace can quote it.
NEVER: dict[str, str] = {
    "multiple_candidates": (
        "two or more subsets fit this credit. The engine identified a set, not an "
        "answer; asking an agent which is more likely produces a guess wearing the "
        "costume of analysis"
    ),
    "ambiguous_grouping": (
        "the credit balances inside more than one settlement group. Same doctrine as "
        "multiple_candidates, one level up"
    ),
    "contested_payment": (
        "two credits want the same payment on equal evidence. Breaking that tie is the "
        "first thing an agent must never do"
    ),
    "solution_cap_reached": (
        "so many combinations fit that the amount is evidence for none of them"
    ),
    "order_dependent_assignment": (
        "the permutation gate caught a match that changed with read order. That is a "
        "defect in the matcher, not a missing document"
    ),
    "narration_count_conflict": (
        "the bank's own narration contradicts the size of the match. Two independent "
        "channels disagree, which is a refusal on the evidence rather than a gap in it"
    ),
    "no_candidate": (
        "nothing in the settlement window could account for this credit at all. There "
        "is no candidate to gather evidence about"
    ),
}


def roles_for(category: str) -> tuple[str, ...]:
    """
    Which specialists may work this category, in order. Empty means none may.

    An unknown category returns empty rather than raising. `report/routing.py` raises on
    one, and correctly -- an exception that reaches no DESK reaches nobody. This is the
    opposite case: an exception that reaches no INVESTIGATOR still reaches its desk, so
    the safe default is to leave it for a human rather than to fail the run.
    """
    return ROUTE.get(category, ())


def why_not(category: str) -> str:
    """The reason this category is not investigated, for the trace. '' if it is."""
    return NEVER.get(category, "")


def CATEGORIES_FOR(role: str) -> frozenset[str]:
    """
    Every category the table may send to this role.

    The specialists' own `handles` sets are built from this, so a role cannot declare
    competence the router would never exercise -- and a category added to `ROUTE` without
    a specialist that accepts it fails a test rather than silently routing to nothing.
    """
    return frozenset(cat for cat, roles in ROUTE.items() if role in roles)
