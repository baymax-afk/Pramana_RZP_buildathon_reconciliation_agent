"""
The matching core: one deterministic pass over a batch.

`match_once` is the unit the permutation ensemble replays. It is a pure function of
`ReconInputs` -- no paths, no clock, no global state, no ground truth -- which is what
makes MR1's comparison across shuffled orderings meaningful. Anything the engine
learns, it learns from the three sides it was handed.

**One deliberate impurity, isolated here:** the matcher walks bank credits in a fixed,
data-derived order (`tier2.sort_key`) rather than in input order. That makes a single
pass reproducible. It does NOT make the result order-independent -- greedy claiming
means an earlier credit can take a payment a later one also wanted, and which credit
gets there first depends on the ordering. Detecting exactly that is the runtime
permutation gate's job in Block 5. Until it exists, single-pass results are provisional
and are labelled as such.
"""

from __future__ import annotations

from ..schemas import Payment, ReconInputs
from . import fees, tier1_reference, tier2_amount_date
from .normalize import parse
from .results import Assignment, Candidate, MatchOutput, Refusal, RefusalCategory


def _invoices_for(payment_ids: tuple[str, ...], by_id: dict[str, Payment]) -> tuple[str, ...]:
    out = []
    for pid in payment_ids:
        p = by_id.get(pid)
        if p:
            inv = p.notes.get("invoice_no")
            if inv:
                out.append(inv)
    return tuple(out)


def _assignment_from(
    txn_id: str,
    credit: int,
    cand: Candidate,
    by_id: dict[str, Payment],
) -> Assignment:
    interval = fees.NetInterval(cand.interval_lo, cand.interval_hi, cand.certain)
    return Assignment(
        bank_txn_id=txn_id,
        payment_ids=cand.payment_ids,
        invoice_nos=_invoices_for(cand.payment_ids, by_id),
        tier=cand.tier,
        residual_paise=cand.residual_paise,
        residual_tightness=fees.residual_tightness(credit, interval),
        certain_fee=cand.certain,
        # Tier 1 needed no search, so nothing competed with it; tier 2 reached here
        # only by being the sole fit in its window. Either way the margin is maximal.
        uniqueness_margin=1.0,
    )


def match_once(inputs: ReconInputs) -> MatchOutput:
    """
    Run tiers 1 and 2 across every bank credit. Deterministic given `inputs`.

    Tier 3 (bounded subset-sum with uniqueness testing) is not wired in yet; credits
    needing a many-to-one decomposition currently fall out as `no_candidate` rather
    than being guessed at. That is the correct interim behaviour -- the engine declines
    to assign what it cannot yet justify, and the metrics harness will show the gap
    honestly as a coverage number rather than hiding it.
    """
    payments = inputs.payments
    by_id = {p.id: p for p in payments}
    index = tier1_reference.ReferenceIndex(payments, inputs.invoices)
    invoices_by_no = {i.invoice_no: i for i in inputs.invoices}

    claimed: set[str] = set()
    assignments: list[Assignment] = []
    refusals: list[Refusal] = []
    no_candidate: list[str] = []
    tier_counts: dict[str, int] = {}

    credits = sorted(
        (t for t in inputs.bank_txns if t.is_credit), key=tier2_amount_date.sort_key
    )

    for txn in credits:
        parsed = parse(txn.narration)

        # ---- tier 1: exact reference ----
        cands, refusal_cat, reason = tier1_reference.match(
            txn, parsed, index, by_id, claimed, invoices_by_no
        )
        if refusal_cat is not None:
            refusals.append(
                Refusal(txn.id, refusal_cat, reason, txn.credit, tuple(cands))
            )
            continue
        if cands:
            cand = cands[0]
            assignments.append(_assignment_from(txn.id, txn.credit, cand, by_id))
            claimed.update(cand.payment_ids)
            tier_counts[cand.tier] = tier_counts.get(cand.tier, 0) + 1
            continue

        # ---- tier 2: amount + date window ----
        cands, refusal_cat, reason = tier2_amount_date.match(
            txn, payments, claimed, invoices_by_no
        )
        if refusal_cat is not None:
            refusals.append(
                Refusal(txn.id, refusal_cat, reason, txn.credit, tuple(cands))
            )
            continue
        if cands:
            cand = cands[0]
            assignments.append(_assignment_from(txn.id, txn.credit, cand, by_id))
            claimed.update(cand.payment_ids)
            tier_counts[cand.tier] = tier_counts.get(cand.tier, 0) + 1
            continue

        # ---- nothing fits: tier 3 territory ----
        no_candidate.append(txn.id)

    unassigned = tuple(
        sorted(p.id for p in payments if p.captured and p.id not in claimed)
    )
    return MatchOutput(
        assignments=tuple(assignments),
        refusals=tuple(refusals),
        no_candidate=tuple(no_candidate),
        unassigned_payment_ids=unassigned,
        tier_counts=tier_counts,
    )
