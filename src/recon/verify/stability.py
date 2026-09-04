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

import pickle
import random
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace

import config as cfg

from ..engine import reversals
from ..engine.match import match_once
from ..engine.results import MatchOutput, Refusal, RefusalCategory
from ..schemas import BankTxn, Payment, ReconInputs


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
    # The batch's raw records, carried so `apply_gate` can recompute the reversal ledger
    # against what actually survived the gate. Defaulted because tests construct an
    # Ensemble directly to exercise the gate's logic; when absent, the gate keeps the
    # base run's reversals rather than silently reporting none.
    bank_txns: tuple[BankTxn, ...] = ()
    payments_by_id: dict[str, Payment] = field(default_factory=dict)

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


# --------------------------------------------------------------------------
# Parallel execution of the ensemble.
#
# The K passes are independent by construction -- `match_once` is a pure function of
# its inputs -- so running them concurrently cannot change any answer, only how long it
# takes. That matters because the ensemble is the engine's PRIMARY execution path, not
# a test: every reported run pays K times the single-pass cost. Measured on 4 cores,
# K=8: 324ms sequential -> 117ms parallel, 2.78x, with byte-identical assignments.
#
# Determinism is preserved by construction rather than by hope: each pass is identified
# by its index, the shuffle derives from `base_seed + i`, and results are collected by
# index rather than by completion order. Nothing observes which worker finished first.
# --------------------------------------------------------------------------

_WORKER_INPUTS: ReconInputs | None = None
_WORKER_LLM = None


def _worker_init(inputs: ReconInputs, llm) -> None:
    global _WORKER_INPUTS, _WORKER_LLM
    _WORKER_INPUTS, _WORKER_LLM = inputs, llm


def _worker_pass(args: tuple[int, int]) -> MatchOutput:
    i, base_seed = args
    assert _WORKER_INPUTS is not None
    shuffled = (
        _WORKER_INPUTS
        if i == 0
        else _WORKER_INPUTS.shuffled(random.Random(base_seed + i))
    )
    return match_once(shuffled, llm=_WORKER_LLM)


def _is_picklable(obj) -> bool:
    """
    Whether this LLM tier can cross a process boundary.

    `RecordedTier` and `NullTier` are plain data and pickle fine. `ClaudeTier` holds an
    open HTTP client and does not. Rather than special-casing tier classes -- which
    would rot the moment a fourth one is added -- the question is asked directly, and a
    tier that cannot travel simply runs the ensemble sequentially. Correctness never
    depends on the answer; only speed does.
    """
    if obj is None:
        return True
    try:
        pickle.dumps(obj)
        return True
    except Exception:
        return False


def _run_passes(
    inputs: ReconInputs, passes: int, base_seed: int, llm
) -> list[MatchOutput]:
    """Run the K passes, in parallel where that is possible, in order either way."""
    sequential = [
        (
            inputs
            if i == 0
            else inputs.shuffled(random.Random(base_seed + i))
        )
        for i in range(passes)
    ]

    if (
        not cfg.PERMUTATION_PARALLEL
        or passes < 2
        or not _is_picklable(llm)
        or not _is_picklable(inputs)
    ):
        return [match_once(s, llm=llm) for s in sequential]

    try:
        with ProcessPoolExecutor(
            max_workers=min(passes, cfg.PERMUTATION_MAX_WORKERS),
            initializer=_worker_init,
            initargs=(inputs, llm),
        ) as pool:
            # `map` yields in argument order, so pass i is always result i.
            return list(pool.map(_worker_pass, [(i, base_seed) for i in range(passes)]))
    except Exception:
        # A sandbox that forbids subprocesses, an exhausted process table, a worker
        # killed by the OOM reaper. None of that is a reason to fail a reconciliation
        # run: the sequential path produces the identical answer.
        return [match_once(s, llm=llm) for s in sequential]


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
    outs = _run_passes(inputs, passes, base_seed, llm)
    base = outs[0]

    for out in outs:
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
    return Ensemble(
        passes=passes, seed=base_seed, per_txn=per_txn, base=base,
        bank_txns=inputs.bank_txns,
        payments_by_id={p.id: p for p in inputs.payments},
    )


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

    # Credits that were assigned in SOME pass but not in pass 0 never appear in
    # base.assignments, so iterating that list alone silently exempts them from the
    # gate -- and those are precisely the unstable ones. A credit assigned in 7 of 8
    # orderings and dropped in the 8th is the clearest possible order-dependence, and
    # if the 8th happened to be pass 0 the gate never looked at it.
    #
    # They cannot be ASSIGNED here either: pass 0 declined them, and this function
    # gates pass 0's output rather than re-deciding it. So each is recorded as an
    # order-dependent refusal, which is what it is.
    gated_ids = {a.bank_txn_id for a in base.assignments}
    already_refused = {r.bank_txn_id for r in base.refusals}
    for txn_id, st in sorted(ensemble.per_txn.items()):
        if txn_id in gated_ids or txn_id in already_refused or st.is_stable:
            continue
        variants = sorted(st.observed, key=lambda kv: (-kv[1], sorted(kv[0])))
        detail = "; ".join(
            f"{{{', '.join(sorted(ids))}}} in {n}/{st.passes} passes"
            for ids, n in variants
        )
        new_refusals.append(
            Refusal(
                bank_txn_id=txn_id,
                category=RefusalCategory.ORDER_DEPENDENT,
                reason=(
                    f"assigned under some input orderings and not others "
                    f"({st.modal_count}/{st.passes} passes) -- decided by iteration "
                    f"order, not by the data: {detail}"
                ),
                paise_at_risk=credits_by_id.get(txn_id, 0),
                candidates=(),
            )
        )

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

    # ---- settlement groups go through the same gate -------------------------
    #
    # They must, and the reason is a defect this project has already shipped once: the
    # gate rebuilds a MatchOutput field by field, so anything added to that type and not
    # added here is silently dropped from the REPORTED run while continuing to look
    # correct in `match_once`. A group left out would be exempt from the permutation
    # gate -- posted without ever having been tested for order-dependence -- which is
    # the one property the gate exists to guarantee.
    #
    # A group is stable only if EVERY member credit is stable. A grouping that holds
    # under some input orderings and not others was decided by traversal, and the
    # correct verdict is the same as for a single credit: refuse the whole group, and
    # say so per credit, because that is the unit an operator sees on the statement.
    kept_groups = []
    for g in base.groups:
        stats = [ensemble.per_txn.get(t) for t in g.bank_txn_ids]
        unstable = [
            (t, st) for t, st in zip(g.bank_txn_ids, stats)
            if st is not None and not st.is_stable
        ]
        if not unstable:
            observed = [st.stability for st in stats if st is not None]
            kept_groups.append(
                replace(g, permutation_stability=min(observed) if observed else 1.0)
            )
            continue
        for txn_id, st in unstable:
            new_refusals.append(
                Refusal(
                    bank_txn_id=txn_id,
                    category=RefusalCategory.ORDER_DEPENDENT,
                    reason=(
                        f"settled as part of the group "
                        f"{'+'.join(g.bank_txn_ids)} under some input orderings and not "
                        f"others ({st.modal_count}/{st.passes} passes) -- the grouping "
                        f"was decided by iteration order, not by the data"
                    ),
                    paise_at_risk=credits_by_id.get(txn_id, 0),
                    candidates=(),
                )
            )
        # The stable members of a dropped group are not assignments either: the group
        # was the claim, and half a group is not a smaller claim.
        for txn_id, st in zip(g.bank_txn_ids, stats):
            if (txn_id, st) in unstable:
                continue
            new_refusals.append(
                Refusal(
                    bank_txn_id=txn_id,
                    category=RefusalCategory.ORDER_DEPENDENT,
                    reason=(
                        f"its settlement group {'+'.join(g.bank_txn_ids)} did not "
                        f"survive the permutation gate, and a part of a group is not a "
                        f"smaller claim -- the whole grouping is withdrawn"
                    ),
                    paise_at_risk=credits_by_id.get(txn_id, 0),
                    candidates=(),
                )
            )

    dropped_group_payments = {
        pid for g in base.groups if g not in kept_groups for pid in g.payment_ids
    }

    # Reversals are recomputed against what SURVIVED the gate, not carried over. A
    # reversal is a claw-back against a posted settlement; if the gate withdrew that
    # settlement, the debit is no longer explained by it and saying otherwise would
    # have the books reversing a match the engine no longer makes.
    if ensemble.bank_txns:
        gated_reversals, gated_unexplained = reversals.resolve(
            ensemble.bank_txns, tuple(kept), tuple(kept_groups), ensemble.payments_by_id
        )
    else:
        gated_reversals = list(base.reversals)
        gated_unexplained = list(base.unexplained_debits)

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
                | dropped_group_payments
            )
        ),
        tier_counts=base.tier_counts,
        groups=tuple(kept_groups),
        reversals=tuple(gated_reversals),
        unexplained_debits=tuple(gated_unexplained),
        group_search_truncated=base.group_search_truncated,
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
