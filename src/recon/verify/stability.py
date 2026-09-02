"""
Layer 1, MR1 -- permutation invariance, used as a RUNTIME REFUSAL GATE.

This is the load-bearing idea in the project.

If shuffling the input row order changes which payments get assigned to a bank credit,
then that assignment was decided by **iteration order rather than by the data**. That
fact is knowable at runtime, with no labels and no ground truth, which is exactly the
property every check here has to have.

So the engine's primary execution path is an ensemble, not a single pass:

    1. Run the matching core K times over independently shuffled inputs.
    2. stability == 1.0  -> the assignment is data-determined; keep it.
    3. stability <  1.0  -> REFUSE. Emit `order_dependent_assignment` listing every
                            distinct assignment observed and its frequency.

The shuffles are themselves derived from the run seed, so the randomised layer is
deterministic and a reported number can be reproduced exactly.

**An honest note on what this currently detects.** `match_once` sorts bank credits into
a total, data-derived order before processing them, and both tiers refuse rather than
choose when several candidates fit. Between them, those two properties make the current
matcher order-independent *by construction*, so stability comes out at 1.0 everywhere
and the gate never fires. That is the correct result, not a broken test -- and the gate
is not decoration: greedy claiming means the moment tier 3 starts enumerating subsets
(Block 6), which subset a credit takes can depend on enumeration order, and this is the
mechanism that catches it. The measurement is reported either way rather than assumed.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass

import config as cfg

from ..engine.match import match_once
from ..engine.results import MatchOutput, Refusal, RefusalCategory
from ..schemas import ReconInputs


@dataclass(frozen=True, slots=True)
class TxnStability:
    """How consistently one bank credit received the same assignment across passes."""

    bank_txn_id: str
    observed: tuple[tuple[frozenset[str], int], ...]  # (assignment, times seen)
    passes: int

    @property
    def modal(self) -> frozenset[str]:
        return max(self.observed, key=lambda kv: (kv[1], sorted(kv[0])))[0]

    @property
    def modal_count(self) -> int:
        return max(count for _, count in self.observed)

    @property
    def stability(self) -> float:
        """
        Fraction of passes producing the most common assignment.

        Note the denominator is the number of PASSES, not the number of passes in which
        this credit was assigned at all. A credit assigned in four passes and left
        unassigned in four others is unstable, not perfectly stable -- treating absence
        as agreement would hide exactly the kind of order-dependence this exists to
        catch.
        """
        return self.modal_count / self.passes if self.passes else 0.0

    @property
    def is_stable(self) -> bool:
        return self.stability >= 1.0


@dataclass(frozen=True, slots=True)
class Ensemble:
    passes: int
    seed: int
    per_txn: dict[str, TxnStability]
    base: MatchOutput

    def unstable(self) -> tuple[TxnStability, ...]:
        return tuple(
            s for s in self.per_txn.values() if not s.is_stable
        )

    def summary(self) -> dict[str, object]:
        vals = [s.stability for s in self.per_txn.values()]
        return {
            "passes": self.passes,
            "txns_observed": len(self.per_txn),
            "unstable": len(self.unstable()),
            "min_stability": min(vals) if vals else 1.0,
            "mean_stability": (sum(vals) / len(vals)) if vals else 1.0,
        }


def run_with_permutations(
    inputs: ReconInputs, k: int | None = None, seed: int | None = None, llm=None
) -> Ensemble:
    """
    Run the matching core over K independently shuffled orderings of all three sides.

    Pass 0 is the UNSHUFFLED input, so the ensemble's base output is comparable with a
    plain single-pass run; passes 1..K-1 are shuffled. Permutations derive from
    `seed + i`, making the whole ensemble reproducible.
    """
    passes = k or cfg.PERMUTATION_K
    base_seed = seed if seed is not None else inputs.seed

    counts: dict[str, Counter] = {}
    base: MatchOutput | None = None

    for i in range(passes):
        if i == 0:
            shuffled = inputs
        else:
            shuffled = inputs.shuffled(random.Random(base_seed + i))
        out = match_once(shuffled, llm=llm)
        if base is None:
            base = out
        for txn_id, payment_ids in out.assignment_map.items():
            counts.setdefault(txn_id, Counter())[payment_ids] += 1

    assert base is not None
    per_txn = {
        txn_id: TxnStability(
            bank_txn_id=txn_id,
            observed=tuple(counter.items()),
            passes=passes,
        )
        for txn_id, counter in counts.items()
    }
    return Ensemble(passes=passes, seed=base_seed, per_txn=per_txn, base=base)


def apply_gate(ensemble: Ensemble, credits_by_id: dict[str, int]) -> MatchOutput:
    """
    Turn the ensemble into a gated MatchOutput.

    Any assignment not stable across every pass is REMOVED and replaced by an
    `order_dependent_assignment` refusal carrying every distinct assignment observed,
    with its frequency and the rupees at risk.

    The gate is HARD, not a discount. An order-dependent assignment is not a
    low-confidence assignment -- it is evidence that the data did not determine the
    answer, and softening it into a confidence penalty would let it be auto-posted
    anyway at a slightly lower score.
    """
    base = ensemble.base
    kept = []
    new_refusals = list(base.refusals)

    for a in base.assignments:
        st = ensemble.per_txn.get(a.bank_txn_id)
        if st is None or st.is_stable:
            kept.append(
                a if st is None else _with_stability(a, st.stability)
            )
            continue

        variants = sorted(st.observed, key=lambda kv: (-kv[1], sorted(kv[0])))
        detail = "; ".join(
            f"{{{', '.join(sorted(ids))}}} in {n}/{st.passes} passes"
            for ids, n in variants
        )
        new_refusals.append(
            Refusal(
                bank_txn_id=a.bank_txn_id,
                category=RefusalCategory.ORDER_DEPENDENT,
                reason=(
                    f"assignment changed under input reordering across "
                    f"{st.passes} passes -- decided by iteration order, not by the "
                    f"data: {detail}"
                ),
                paise_at_risk=credits_by_id.get(a.bank_txn_id, 0),
                candidates=(),
            )
        )

    return MatchOutput(
        assignments=tuple(kept),
        refusals=tuple(new_refusals),
        no_candidate=base.no_candidate,
        unassigned_payment_ids=tuple(
            sorted(
                set(base.unassigned_payment_ids)
                | {
                    pid
                    for a in base.assignments
                    if a not in kept
                    for pid in a.payment_ids
                }
            )
        ),
        tier_counts=base.tier_counts,
    )


def _with_stability(assignment, stability: float):
    from dataclasses import replace

    return replace(assignment, permutation_stability=stability)


def match_gated(
    inputs: ReconInputs, k: int | None = None, llm=None
) -> tuple[MatchOutput, Ensemble]:
    """
    The engine's PRIMARY entry point: match under the permutation gate.

    `match_once` remains available and is what the ensemble replays internally, but
    nothing outside this module should call it directly for a reported run -- a single
    pass has not been tested for order-dependence, and its assignments are provisional.
    """
    ensemble = run_with_permutations(inputs, k=k, llm=llm)
    credits_by_id = {t.id: t.credit for t in inputs.bank_txns}
    return apply_gate(ensemble, credits_by_id), ensemble
