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

import config as cfg

from ..schemas import Payment, ReconInputs


def cfg_fs_lower() -> float:
    return cfg.FS_THRESHOLD_LOWER
from . import fees, fellegi_sunter as fs, tier1_reference, tier2_amount_date, tier3_subsetsum
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
    uniqueness: float = 1.0,
    fs_weight: float | None = None,
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
        # Tier 3 passes its measured margin -- how far the next-best subset sat.
        uniqueness_margin=uniqueness,
        fs_weight=fs_weight,
    )


MAX_ROUNDS = 6


def _verdict_for(txn, payments, by_id, index, claimed, invoices_by_no, u_est):
    """
    Run the three tiers against one credit, in descending order of evidence strength.

    A tier that finds nothing falls through to the next. A tier that finds AMBIGUITY
    stops and refuses -- a later, weaker tier cannot resolve what a stronger one has
    already shown to be underdetermined.
    """
    parsed = parse(txn.narration)

    cands, cat, reason = tier1_reference.match(
        txn, parsed, index, by_id, claimed, invoices_by_no
    )
    if cat is not None:
        return ("refuse", cands, cat, reason, 1.0, None)
    if cands:
        return ("assign", cands, None, "", 1.0, None)

    cands, cat, reason = tier2_amount_date.match(
        txn, payments, claimed, invoices_by_no
    )
    if cat is not None:
        return ("refuse", cands, cat, reason, 1.0, None)
    if cands:
        return ("assign", cands, None, "", 1.0, None)

    cands, cat, reason, uniq = tier3_subsetsum.match_with_margin(
        txn, payments, claimed, invoices_by_no
    )
    if cat is not None:
        return ("refuse", cands, cat, reason, 0.0, None)
    if cands:
        return ("assign", cands, None, "", uniq, None)

    return ("none", [], None, "", 0.0, None)


def match_once(inputs: ReconInputs) -> MatchOutput:
    """
    Match a batch, iterating to a FIXPOINT.

    Deterministic given `inputs`, and idempotent by construction: the loop repeats
    until a full round produces no new assignment, so rerunning the engine on its own
    residue can find nothing further.

    **Why iterate at all.** Claiming is greedy and credits are processed in a fixed
    order, so a credit examined early may see two viable decompositions and refuse --
    and then a later credit claims one of the payments involved, leaving the first
    credit with exactly one. Its refusal was correct on the information available at the
    time and is stale afterwards. A single pass therefore leaves work undone, and MR6
    caught precisely that: rerunning on the residue produced fresh assignments, meaning
    the engine's output depended on how many times it happened to be run.

    Resolving genuine ambiguity with information that arrives later is correct
    behaviour, not a shortcut. What would NOT be acceptable is resolving it by picking,
    and the tiers still refuse rather than choose within any single round.
    """
    payments = inputs.payments
    by_id = {p.id: p for p in payments}
    index = tier1_reference.ReferenceIndex(payments, inputs.invoices)
    invoices_by_no = {i.invoice_no: i for i in inputs.invoices}
    u_est = fs.estimate_u(payments, inputs.bank_txns)

    credits = sorted(
        (t for t in inputs.bank_txns if t.is_credit), key=tier2_amount_date.sort_key
    )

    claimed: set[str] = set()
    assignments: list[Assignment] = []
    tier_counts: dict[str, int] = {}
    settled: set[str] = set()
    refusals: list[Refusal] = []
    no_candidate: list[str] = []
    rounds = 0

    for _ in range(MAX_ROUNDS):
        rounds += 1
        progressed = False
        refusals, no_candidate = [], []

        for txn in credits:
            if txn.id in settled:
                continue
            verdict, cands, cat, reason, uniq, _ = _verdict_for(
                txn, payments, by_id, index, claimed, invoices_by_no, u_est
            )

            if verdict == "assign":
                cand = cands[0]
                # ---- Layer 3: Fellegi-Sunter two-threshold band ----
                ev = fs.evidence_for(
                    txn,
                    parse(txn.narration),
                    [by_id[pid] for pid in cand.payment_ids if pid in by_id],
                    u_est,
                    pool_size=max(2, len(tier2_amount_date.candidate_pool(txn, payments, claimed))),
                )
                if ev.contradicts:
                    # Names and references actively contradict the amount evidence.
                    # Two independent channels disagree, so neither is trusted alone.
                    refusals.append(
                        Refusal(
                            txn.id, RefusalCategory.AMOUNT_NAME_CONFLICT,
                            f"amounts reconcile (residual {cand.residual_paise:+d}p) but "
                            f"non-amount evidence contradicts it: Fellegi-Sunter field "
                            f"weight {ev.field_weight:+.2f} (total {ev.weight:+.2f}). "
                            + "; ".join(
                                f.detail for f in ev.fields if f.level is not None
                            ),
                            txn.credit, tuple(cands),
                        )
                    )
                    continue
                assignments.append(
                    _assignment_from(
                        txn.id, txn.credit, cand, by_id,
                        uniqueness=uniq, fs_weight=ev.weight,
                    )
                )
                claimed.update(cand.payment_ids)
                settled.add(txn.id)
                tier_counts[cand.tier] = tier_counts.get(cand.tier, 0) + 1
                progressed = True
            elif verdict == "refuse":
                refusals.append(
                    Refusal(txn.id, cat, reason, txn.credit, tuple(cands))
                )
            else:
                no_candidate.append(txn.id)

        if not progressed:
            break

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
