"""
Tier 3 -- bounded subset-sum decomposition, and Layer 2's uniqueness test.

A settlement batch credits one amount for many payments, so recovering which payments
it covers is a subset-sum problem. Two things make this tier different from a
conventional solver, and both matter more than the search itself.

**It enumerates ALL solutions, never the first.** This is the entire point of Layer 2.
Finding *an* answer is easy; knowing whether it is *the* answer is the unsolved part.
If exactly one subset satisfies the constraint that is strong evidence. If two do, the
constraint has not identified an answer -- it has identified two -- and the engine
refuses and hands both to a human.

This is where the state of the art stops. The J.P. Morgan formalisation of financial
reconciliation as a Subset Sum Matching Problem (arXiv 2508.19218) gives three
algorithms -- MILP, cached search, and dynamic programming -- and all of them
**terminate on the first valid match**. None addresses non-unique solutions. That gap
is what this module fills, and it is not a small one: deciding whether a knapsack
instance has a unique optimum is Delta-2-P-complete, strictly harder than finding an
optimum. Verifying uniqueness is provably harder than producing an answer.

**Amounts are intervals, not numbers.** The engine knows a payment's fee only within a
band unless Razorpay priced it, so every partial sum carries a lo and a hi and the
target must fall inside the summed interval within tolerance. Interval arithmetic
throughout, never a point estimate with a fudge factor.

**On order-independence.** The pool is sorted internally before the search, so the
result depends on the SET of candidates, not the order they arrived in. That is
deliberate: it makes tier 3 order-independent by construction rather than by luck, and
the permutation gate confirms it rather than having to repair it.
"""

from __future__ import annotations

from dataclasses import dataclass

import config as cfg

from ..schemas import BankTxn, Invoice, Payment
from . import fees, tier2_amount_date
from .results import Candidate, RefusalCategory

TIER = "tier3_subsetsum"


@dataclass(frozen=True, slots=True)
class Solution:
    payment_ids: tuple[str, ...]
    lo: int
    hi: int
    residual: int
    certain: bool


@dataclass(frozen=True, slots=True)
class SearchResult:
    """
    Everything the search learned, including what it *nearly* found.

    `best_miss` is the tightest subset that fell OUTSIDE tolerance. It is what makes a
    uniqueness margin meaningful: a single solution with the next-best candidate far
    away is strong evidence, while a single solution with another subset a few paise
    outside tolerance is a coin toss that happened to land on one side of a threshold.
    Reporting the first as confidently as the second would be exactly the overclaim this
    project exists to argue against.
    """

    solutions: tuple[Solution, ...]
    best_miss: int | None
    pool_size: int
    capped: bool
    nodes: int


def _effective_intervals(
    pool: list[Payment], invoices_by_no: dict[str, Invoice]
) -> list[tuple[str, int, int, bool]]:
    """
    Per-payment settled-amount intervals, already net of KNOWN ledger deductions.

    TDS is folded in here rather than subtracted from the credit, because which TDS
    applies depends on which payments are in the subset -- it cannot be pre-deducted
    from the target. Folding it per payment keeps the search a plain interval subset-sum
    over a single quantity.
    """
    out = []
    for p in pool:
        iv = fees.expected_credit_interval([p], invoices_by_no)
        out.append((p.id, iv.lo, iv.hi, iv.certain))
    # Sorted by (lo, id): a total order derived from the DATA, so enumeration depends on
    # the set of candidates rather than the order they were handed to us.
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def search(
    target: int,
    pool: list[Payment],
    invoices_by_no: dict[str, Invoice],
    tolerance: int | None = None,
    max_k: int | None = None,
    max_solutions: int | None = None,
) -> SearchResult:
    """
    Find every subset of `pool` (size 1..max_k) whose summed interval covers `target`
    within tolerance.

    Depth-first with two prunes, both exact rather than heuristic -- a heuristic prune
    could silently discard a second solution and turn a genuine ambiguity into a
    confident wrong answer:

      * **Overshoot.** All amounts are positive, so once the running lower bound exceeds
        the target plus tolerance, no extension can come back down.
      * **Unreachable.** Suffix sums bound the most the remaining candidates could add;
        if the running upper bound plus that maximum still falls short, stop.
    """
    tol = tolerance if tolerance is not None else fees.tolerance_for(target)
    kmax = max_k or cfg.MAX_SUBSET_K
    cap = max_solutions or cfg.MAX_SOLUTIONS

    items = _effective_intervals(pool, invoices_by_no)
    n = len(items)

    # suffix_hi[i] = the largest additional amount items[i:] could contribute.
    suffix_hi = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_hi[i] = suffix_hi[i + 1] + max(0, items[i][2])

    solutions: list[Solution] = []
    best_miss: int | None = None
    nodes = 0
    capped = False

    def record_miss(resid: int) -> None:
        nonlocal best_miss
        if best_miss is None or abs(resid) < abs(best_miss):
            best_miss = resid

    def dfs(start: int, chosen: list[int], lo: int, hi: int, certain: bool) -> None:
        nonlocal nodes, capped
        if capped:
            return
        nodes += 1

        if chosen:
            if lo <= target <= hi:
                resid = 0
            elif target < lo:
                resid = target - lo
            else:
                resid = target - hi
            if abs(resid) <= tol:
                solutions.append(
                    Solution(
                        payment_ids=tuple(sorted(items[i][0] for i in chosen)),
                        lo=lo, hi=hi, residual=resid, certain=certain,
                    )
                )
                if len(solutions) >= cap:
                    capped = True
                    return
                # A superset of a solution overshoots, so stop descending here.
                return
            record_miss(resid)

        if len(chosen) >= kmax:
            return
        for i in range(start, n):
            _, ilo, ihi, icert = items[i]
            nlo = lo + ilo
            if nlo > target + tol:
                # Items are sorted ascending by lo, so every later item is at least as
                # large: no sibling at this level can fit either.
                #
                # Record the overshoot BEFORE pruning. The prune is correct -- nothing
                # below here can fit -- but the subset we are about to discard may be
                # the closest thing to a rival the search will ever see. Breaking first
                # meant a decomposition sitting 5p outside tolerance was never compared
                # against, `best_miss` kept a far worse value, and uniqueness_margin
                # reported 1.0: perfect isolation, on a credit that had a near-twin.
                # Only near overshoots are recorded; a wildly oversized subset is not
                # evidence about anything.
                if nlo - target <= 2 * tol:
                    record_miss(target - nlo)
                break
            if hi + suffix_hi[i] < target - tol:
                break
            chosen.append(i)
            dfs(i + 1, chosen, nlo, hi + ihi, certain and icert)
            chosen.pop()
            if capped:
                return

    dfs(0, [], 0, 0, True)
    return SearchResult(tuple(solutions), best_miss, n, capped, nodes)


def uniqueness_margin(result: SearchResult, tolerance: int) -> float:
    """
    How isolated the single solution is, in [0, 1].

    1.0 means the next-best subset is at least a full tolerance BEYOND the tolerance
    boundary -- nothing else came close. 0.0 means another subset sits right on the
    boundary, and only the threshold separates a unique answer from a tie.

    When no near-miss exists at all, the margin is 1.0: the search explored the pool and
    found exactly one arrangement of the money that works.

    **The distance is measured from the tolerance EDGE, not from the winning residual.**
    Measuring from the residual divided a rival's absolute distance by the tolerance, so
    anything further than one tolerance away scored 1.0 -- a rival 3 paise outside the
    boundary and a rival 3 rupees outside both reported "perfectly isolated". The first
    is a coin toss that happened to land on one side of a threshold; the second is a
    genuinely unique answer. Compressing them into the same number is exactly the
    overclaim Layer 2 exists to prevent, and it survived the near-miss recording fix
    because it is a separate defect in the same expression.
    """
    if len(result.solutions) != 1 or tolerance <= 0:
        return 0.0
    if result.best_miss is None:
        return 1.0
    # How far OUTSIDE tolerance the nearest rival sits.
    excess = abs(result.best_miss) - tolerance
    return max(0.0, min(1.0, excess / tolerance))


def match(
    txn: BankTxn,
    payments: tuple[Payment, ...],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
) -> tuple[list[Candidate], RefusalCategory | None, str]:
    """
    Decompose one bank credit into the payments it covers.

    Returns (candidates, refusal_category, reason). Every outcome except "exactly one
    solution" declines to assign, and each declines for a *named* reason rather than
    silently returning nothing.
    """
    pool = tier2_amount_date.candidate_pool(txn, payments, claimed)
    if not pool:
        return [], None, ""

    # Pool larger than the search bound. Refuse rather than truncate: dropping
    # candidates to fit a cap could remove the true decomposition and leave a wrong one
    # looking unique -- the worst possible failure, a confident wrong answer.
    if len(pool) > cfg.MAX_POOL:
        return (
            [],
            RefusalCategory.OUT_OF_BOUNDS,
            f"candidate pool is {len(pool)} payments, above MAX_POOL={cfg.MAX_POOL}; "
            f"the decomposition cannot be searched exhaustively, so no answer is "
            f"claimed (truncating the pool could hide the true subset)",
        )

    tol = fees.tolerance_for(txn.credit)
    result = search(txn.credit, pool, invoices_by_no, tolerance=tol)

    def to_candidate(s: Solution) -> Candidate:
        return Candidate(
            payment_ids=s.payment_ids, residual_paise=s.residual, tier=TIER,
            interval_lo=s.lo, interval_hi=s.hi, certain=s.certain,
        )

    if result.capped:
        return (
            [to_candidate(s) for s in result.solutions],
            RefusalCategory.SOLUTION_CAP_REACHED,
            f"at least {len(result.solutions)} distinct decompositions satisfy this "
            f"credit within {tol}p; the constraint has not identified an answer",
        )

    if not result.solutions:
        return (
            [],
            RefusalCategory.OUT_OF_BOUNDS,
            f"no subset of the {result.pool_size} candidates sums to {txn.credit}p "
            f"within {tol}p at k<={cfg.MAX_SUBSET_K}"
            + (f" (closest miss {result.best_miss:+d}p)" if result.best_miss is not None else ""),
        )

    if len(result.solutions) > 1:
        # THE Layer 2 refusal. Two or more subsets each account for this credit exactly
        # as well as the others. Rank them for a human's convenience, but do not treat
        # rank as evidence -- the engine has no basis to choose.
        ranked = sorted(
            result.solutions, key=lambda s: (abs(s.residual), s.payment_ids)
        )
        return (
            [to_candidate(s) for s in ranked],
            RefusalCategory.MULTIPLE_CANDIDATES,
            f"{len(ranked)} distinct subsets each satisfy this credit within {tol}p: "
            + " | ".join(
                "{" + ", ".join(s.payment_ids) + "}" for s in ranked[:4]
            )
            + "; amount evidence cannot identify one",
        )

    return [to_candidate(result.solutions[0])], None, ""


def match_with_margin(
    txn: BankTxn,
    payments: tuple[Payment, ...],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
) -> tuple[list[Candidate], RefusalCategory | None, str, float]:
    """
    `match`, plus the uniqueness margin for the accepted solution.

    The margin is carried onto the Assignment because it is a Layer 2 input to the
    composite confidence score: a lone solution with nothing else nearby deserves more
    confidence than a lone solution that only just outran a rival.
    """
    pool = tier2_amount_date.candidate_pool(txn, payments, claimed)
    cands, cat, reason = match(txn, payments, claimed, invoices_by_no)
    if cat is not None or not cands:
        return cands, cat, reason, 0.0
    tol = fees.tolerance_for(txn.credit)
    result = search(txn.credit, pool, invoices_by_no, tolerance=tol)
    return cands, None, "", uniqueness_margin(result, tol)
