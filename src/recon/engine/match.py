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

from dataclasses import dataclass

import config as cfg

from ..schemas import Payment, ReconInputs
from . import (
    confidence as conf,
    fees,
    fellegi_sunter as fs,
    tier1_reference,
    tier2_amount_date,
    tier3_subsetsum,
)
from .normalize import parse_with_llm
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
        confidence=conf.score(
            residual_tightness=fees.residual_tightness(credit, interval),
            uniqueness_margin=uniqueness,
            fs_weight=fs_weight,
        ).confidence,
    )




def _verdict_for(txn, payments, by_id, index, claimed, invoices_by_no, u_est, llm=None):
    """
    Run the three tiers against one credit, in descending order of evidence strength.

    A tier that finds nothing falls through to the next. A tier that finds AMBIGUITY
    stops and refuses -- a later, weaker tier cannot resolve what a stronger one has
    already shown to be underdetermined.
    """
    # The LLM, when enabled, fills only narration FIELDS the regex tier could not
    # read. It cannot reach a matching decision from here -- see llm/interface.py
    # for why that is a type-level guarantee rather than a convention.
    parsed = parse_with_llm(txn.narration, llm)

    def count_conflict(cands) -> bool:
        """
        Does the bank's own narration contradict the size of this match?

        A settlement narration states how many transactions it covers -- "RAZORPAY
        SETTLEMENT setl_... 2 TXNS". The engine has always PARSED that count and never
        used it, which let a credit covering two payments be posted to one whenever a
        single payment's net happened to equal the batch total.

        That is not hypothetical. At seed 11111 a netted refund came to exactly the
        second payment's net, so the batch total equalled the first payment alone.
        Tier 2 found an exact one-to-one fit with residual 0, assigned it, and never
        reached tier 3 -- where enumeration would have found BOTH decompositions and
        Layer 2 would have refused. The tier ordering short-circuited the uniqueness
        test, and the result was a confident wrong answer at confidence 0.96.

        The count is a genuinely independent, label-free channel: it comes from the
        bank's text, not from the amounts, so it can contradict an arithmetic fit
        without being derived from one. Using it is the same move as Layer 3 -- when
        two independent channels disagree, neither is trusted alone.
        """
        return bool(cands) and parsed.txn_count is not None and (
            len(cands[0].payment_ids) != parsed.txn_count
        )

    def conflict_reason(cands) -> str:
        return (
            f"the bank's narration states this settlement covers "
            f"{parsed.txn_count} transaction(s), but the amount evidence fits "
            f"{len(cands[0].payment_ids)}: "
            + ", ".join(cands[0].payment_ids)
            + ". Two independent channels disagree, so neither is trusted alone"
        )

    cands, cat, reason = tier1_reference.match(
        txn, parsed, index, by_id, claimed, invoices_by_no
    )
    if cat is not None:
        return ("refuse", cands, cat, reason, 1.0, parsed)
    if cands and not count_conflict(cands):
        return ("assign", cands, None, "", 1.0, parsed)
    tier1_conflict = cands if count_conflict(cands) else []

    cands, cat, reason = tier2_amount_date.match(
        txn, payments, claimed, invoices_by_no
    )
    if cat is not None:
        return ("refuse", cands, cat, reason, 1.0, parsed)
    if cands and not count_conflict(cands):
        return ("assign", cands, None, "", 1.0, parsed)
    tier2_conflict = cands if count_conflict(cands) else []

    cands, cat, reason, uniq = tier3_subsetsum.match_with_margin(
        txn, payments, claimed, invoices_by_no
    )
    if cat is not None:
        return ("refuse", cands, cat, reason, 0.0, parsed)
    if cands and not count_conflict(cands):
        return ("assign", cands, None, "", uniq, parsed)

    # Tier 3 could not produce a decomposition of the stated size either. Whatever a
    # single-payment tier found earlier is reported as the refusal's candidate, because
    # an exception that names what it declined to post is actionable and one that says
    # only "conflict" is not.
    blocked = cands or tier2_conflict or tier1_conflict
    if blocked:
        return (
            "refuse", blocked, RefusalCategory.NARRATION_COUNT_CONFLICT,
            conflict_reason(blocked), 0.0, parsed,
        )

    return ("none", [], None, "", 0.0, parsed)


# Tier precedence IS the engine's declared evidence hierarchy -- tier 1 is an exact
# reference agreement, tier 2 a unique amount/date fit, tier 3 a searched decomposition.
# Naming it here keeps "which evidence is stronger" a single fact rather than something
# re-derived at each comparison.
_TIER_RANK = {
    tier1_reference.TIER: 2,
    tier2_amount_date.TIER: 1,
    tier3_subsetsum.TIER: 0,
}


@dataclass(frozen=True, slots=True)
class _Proposal:
    """One credit's bid for a set of payments, before any of it is granted."""

    txn_id: str
    credit: int
    cand: Candidate
    uniqueness: float
    # None means "there was nothing at all to weigh" -- explicitly NOT the same as a
    # weight of zero, which means the evidence balanced out. See fellegi_sunter.Evidence.
    fs_weight: float | None

    @property
    def evidence_key(self) -> tuple[int, float]:
        """
        How strong this bid is, for comparison against a rival bid.

        Two components, both already part of the engine's stated evidence model: the
        tier that produced it, and the Fellegi-Sunter weight of its non-amount evidence.
        The FS weight is rounded because it is a float sum -- without that, two bids
        that are equal in every respect that matters could differ in the last bit and
        produce a "strict" winner that is really a coin toss.

        Deliberately NOT included: residual tightness, subset size, recency, or
        anything else that would break a tie. A tie here means the evidence does not
        separate two claims on the same money, and the correct output is a refusal.

        **An absent weight ranks below every real one, and never as zero.** `None` means
        the Fellegi-Sunter layer had nothing to weigh -- no usable name, no usable
        reference -- which the evidence model treats as categorically different from
        evidence that cancelled to zero. Mapping it to 0.0 would let a credit with *no*
        supporting evidence outrank one carrying real but slightly negative evidence, and
        would let it win contested money outright. Ranking it at -inf means it can only
        lose a contest or tie with another evidence-free bid, and a tie refuses both.

        (This was a live crash, not a hypothetical: `round(None, 6)` raised TypeError.
        It surfaced only when the density sweep was re-run at ppw=12 -- neither the
        primary seed nor the test suite contained a credit whose FS layer found nothing
        to weigh.)
        """
        weight = float("-inf") if self.fs_weight is None else round(self.fs_weight, 6)
        return (_TIER_RANK.get(self.cand.tier, -1), weight)


def _fmt_weight(w: float | None) -> str:
    """Render an FS weight for an operator, distinguishing absent from zero."""
    return "no non-amount evidence" if w is None else f"{w:+.2f}"


def _resolve_contested(
    proposals: list[_Proposal],
) -> tuple[list[_Proposal], list[tuple[_Proposal, list[_Proposal]]]]:
    """
    Award each contested payment on evidence, and refuse where evidence cannot separate.

    **Why this replaces greedy claiming.** The loop used to walk credits in sorted order
    and let each take what it wanted, so when two credits both had a viable claim on one
    payment, the winner was whichever the sort happened to reach first. That is a
    decision made by iteration order, on money, and the permutation gate could only
    detect it after the fact. Detecting a design weakness is weaker than not having it:
    the gate is now a safety net rather than a load-bearing part of the answer.

    A proposal is granted when it is uncontested, or when its evidence STRICTLY beats
    every rival bidding for any payment it wants. Equal evidence is not a tie to be
    broken -- it is the engine saying two credits have an equally good claim on the same
    money, which is the same underdetermination Layer 2 refuses on, arriving through a
    different door. Both are refused and both are handed to a human.

    Returns (granted, contested) where each contested entry pairs a losing proposal with
    the rivals that beat or tied it, so the refusal can name them.
    """
    wanted: dict[str, list[_Proposal]] = {}
    for prop in proposals:
        for pid in prop.cand.payment_ids:
            wanted.setdefault(pid, []).append(prop)

    granted: list[_Proposal] = []
    contested: list[tuple[_Proposal, list[_Proposal]]] = []

    for prop in proposals:
        rivals = {
            other.txn_id: other
            for pid in prop.cand.payment_ids
            for other in wanted[pid]
            if other.txn_id != prop.txn_id
        }
        if not rivals:
            granted.append(prop)
            continue
        mine = prop.evidence_key
        # max() over rival keys and a STRICT comparison: both are symmetric in the
        # rivals, so the outcome does not depend on the order the rivals were found.
        strongest = max(r.evidence_key for r in rivals.values())
        if mine > strongest:
            granted.append(prop)
        else:
            blockers = sorted(
                (r for r in rivals.values() if r.evidence_key >= mine),
                key=lambda r: r.txn_id,
            )
            contested.append((prop, blockers))

    return granted, contested


def match_once(inputs: ReconInputs, llm=None) -> MatchOutput:
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

    # ---- Fellegi-Sunter prior, fixed before any claiming ----
    #
    # lambda = 1/pool_size is the prior that a random (credit, payment) pair inside the
    # blocking window matches, and BLOCKING is what makes it tractable. The blocking key
    # is the date window -- nothing else. Measuring the pool *as currently claimed* let
    # the greedy loop leak into the probabilistic layer: two identical credits got
    # different priors depending on which happened to be processed first, and the same
    # credit got a different prior on round 2 than on round 1, because earlier
    # assignments had drained its window. A prior that moves while the evidence does not
    # is not a prior.
    #
    # Computed once, with nothing claimed, so it is a property of the batch's date
    # structure rather than of the traversal.
    unclaimed: set[str] = set()
    blocking_pool_size = {
        txn.id: max(
            2, len(tier2_amount_date.candidate_pool(txn, payments, unclaimed))
        )
        for txn in credits
    }

    claimed: set[str] = set()
    assignments: list[Assignment] = []
    tier_counts: dict[str, int] = {}
    settled: set[str] = set()
    refusals: list[Refusal] = []
    no_candidate: list[str] = []
    rounds = 0

    for _ in range(cfg.MAX_ROUNDS):
        rounds += 1
        refusals, no_candidate = [], []
        proposals: list[_Proposal] = []

        # ---- PROPOSE ----------------------------------------------------------
        # Every unsettled credit bids against the SAME claimed set. Nothing is granted
        # inside this loop, so no credit's bid can shrink a later credit's pool -- which
        # is precisely how sort order used to leak into the answer.
        for txn in credits:
            if txn.id in settled:
                continue
            # The parse comes back with the verdict. It used to be discarded here and
            # recomputed below for `fs.evidence_for` -- and with a live ClaudeTier that
            # is a second API call per assigned credit, multiplied by up to MAX_ROUNDS
            # fixpoint rounds and again by the K=8 permutation passes. Nothing in the
            # path memoises, so the cost was real rather than theoretical.
            # REVIEW_2026-09-02 R11.
            verdict, cands, cat, reason, uniq, parsed = _verdict_for(
                txn, payments, by_id, index, claimed, invoices_by_no, u_est, llm
            )

            if verdict == "assign":
                cand = cands[0]
                # ---- Layer 3: Fellegi-Sunter two-threshold band ----
                ev = fs.evidence_for(
                    txn,
                    parsed,
                    [by_id[pid] for pid in cand.payment_ids if pid in by_id],
                    u_est,
                    pool_size=blocking_pool_size[txn.id],
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
                proposals.append(
                    _Proposal(txn.id, txn.credit, cand, uniq, ev.weight)
                )
            elif verdict == "refuse":
                refusals.append(
                    Refusal(txn.id, cat, reason, txn.credit, tuple(cands))
                )
            else:
                no_candidate.append(txn.id)

        # ---- RESOLVE ----------------------------------------------------------
        granted, contested = _resolve_contested(proposals)

        for prop in granted:
            assignments.append(
                _assignment_from(
                    prop.txn_id, prop.credit, prop.cand, by_id,
                    uniqueness=prop.uniqueness, fs_weight=prop.fs_weight,
                )
            )
            claimed.update(prop.cand.payment_ids)
            settled.add(prop.txn_id)
            tier_counts[prop.cand.tier] = tier_counts.get(prop.cand.tier, 0) + 1

        for prop, blockers in contested:
            shared = sorted(
                set(prop.cand.payment_ids).intersection(
                    *(set(b.cand.payment_ids) for b in blockers)
                )
                or set(prop.cand.payment_ids)
            )
            refusals.append(
                Refusal(
                    prop.txn_id, RefusalCategory.CONTESTED_PAYMENT,
                    f"{len(blockers) + 1} credits have an equally good or better claim "
                    f"on {', '.join(shared)}: this credit at Fellegi-Sunter weight "
                    f"{_fmt_weight(prop.fs_weight)} (tier {prop.cand.tier}) against "
                    + "; ".join(
                        f"{b.txn_id} at {_fmt_weight(b.fs_weight)} "
                        f"(tier {b.cand.tier})"
                        for b in blockers
                    )
                    + ". The evidence does not separate them, so neither is posted",
                    prop.credit, (prop.cand,),
                )
            )

        # A round that granted nothing cannot change what the next round would see.
        if not granted:
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
