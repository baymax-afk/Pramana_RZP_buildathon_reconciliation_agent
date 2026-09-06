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
    # WHICH payments came closest, alongside how close. The gap alone told an operator a
    # credit was Rs 37.00 short of something and left them to find out what -- so a
    # refusal that said "no subset fits" carried no candidate at all, on six of fifteen
    # exceptions including the largest. Naming the subset turns the same refusal into
    # "these three payments come to Rs 37.00 less than this credit", which is a bank
    # charge or a short-payment to go and look for. See the 2026-09-03 audit, finding P1-3.
    best_miss_ids: tuple[str, ...] = ()
    # The closest subset the search could REACH, recorded whatever the distance.
    #
    # Separate from `best_miss` on purpose, and the separation is the whole point.
    # `best_miss` is EVIDENCE: it feeds `uniqueness_margin`, so it is deliberately
    # narrow -- only subsets near enough to count as rivals, within 2x tolerance. Widening
    # it to make refusals more informative would have tightened margins on assignments
    # that are not in fact contested, and could have turned correct assignments into
    # refusals. This field is REPORTING only: nothing reads it but the exception text.
    #
    # It exists because a `no_subset_fits` refusal was landing on the largest credit in
    # the batch carrying nothing at all. The remaining payment overshot by 498p on a
    # Rs 45,673 credit -- 4.98 rupees, a bank charge in all but name -- and the near-miss
    # guard suppressed it for being outside 2x a 100p tolerance. "Nothing accounts for
    # this credit" is not something an operator can act on; "this one payment is Rs 4.98
    # too big for it" is.
    nearest_ids: tuple[str, ...] = ()
    nearest_residual: int | None = None
    pool_size: int = 0
    capped: bool = False
    nodes: int = 0


def _effective_intervals(
    pool: list[Payment], invoices_by_no: dict[str, Invoice], declared_paise: int = 0
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
        iv = fees.expected_credit_interval([p], invoices_by_no, declared_paise)
        out.append((p.id, iv.lo, iv.hi, iv.certain))
    # Sorted by (lo, id): a total order derived from the DATA, so enumeration depends on
    # the set of candidates rather than the order they were handed to us.
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def _gap(credit: int, payment_ids, pool, invoices_by_no, declared_paise: int = 0) -> int:
    """The residual between a credit and a named subset, recomputed from the records."""
    chosen = [p for p in pool if p.id in set(payment_ids)]
    return fees.residual(
        credit, fees.expected_credit_interval(chosen, invoices_by_no, declared_paise)
    )


def search(
    target: int,
    pool: list[Payment],
    invoices_by_no: dict[str, Invoice],
    tolerance: int | None = None,
    max_k: int | None = None,
    max_solutions: int | None = None,
    declared_paise: int = 0,
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

    items = _effective_intervals(pool, invoices_by_no, declared_paise)
    n = len(items)

    # suffix_hi[i] = the largest additional amount items[i:] could contribute.
    suffix_hi = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_hi[i] = suffix_hi[i + 1] + max(0, items[i][2])

    solutions: list[Solution] = []
    best_miss: int | None = None
    best_miss_ids: tuple[str, ...] = ()
    nodes = 0
    capped = False

    def record_miss(resid: int, chosen: list[int]) -> None:
        nonlocal best_miss, best_miss_ids
        if best_miss is None or abs(resid) < abs(best_miss):
            best_miss = resid
            # Snapshotted: `chosen` is mutated by the search as it backtracks, so keeping
            # a reference would leave the "closest subset" describing wherever the walk
            # happened to finish rather than where it was closest.
            best_miss_ids = tuple(items[i][0] for i in chosen)

    nearest: int | None = None
    nearest_ids: tuple[str, ...] = ()

    def record_reach(resid: int, chosen: list[int]) -> None:
        """Reporting only -- never feeds `best_miss`, so no margin can move."""
        nonlocal nearest, nearest_ids
        if chosen and (nearest is None or abs(resid) < abs(nearest)):
            nearest = resid
            nearest_ids = tuple(items[i][0] for i in chosen)

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
            record_miss(resid, chosen)

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
                    record_miss(target - nlo, chosen + [i])
                record_reach(target - nlo, chosen + [i])
                break
            if hi + suffix_hi[i] < target - tol:
                # Record the SHORTFALL before pruning, for the same reason the overshoot
                # branch above records before breaking -- and this half was missing.
                #
                # The prune is correct: nothing at or after `i` can reach the target. But
                # "nothing accounts for this credit", carrying nothing, is a fact an
                # operator can do nothing with, and it was landing on the largest
                # exception in the batch. What they need is the shortfall: "everything
                # left in this window comes to Rs X, which is Rs Y short" is a bank
                # charge or a missing settlement to go and find.
                #
                # The subset recorded is the most reachable one within the k budget --
                # `chosen` plus the largest remaining items, taken from the tail because
                # `items` is sorted ascending. It can only FILL IN a `best_miss` that
                # was None: `record_miss` keeps the tightest residual, and a shortfall
                # this large never displaces a nearer miss, so no uniqueness margin
                # moves. Verified: every assignment and every margin in the batch is
                # byte-identical with and without this branch.
                room = kmax - len(chosen)
                if room > 0:
                    tail = list(range(max(i, n - room), n))
                    reach = hi + sum(items[j][2] for j in tail)
                    record_reach(target - reach, chosen + tail)
                break
            chosen.append(i)
            dfs(i + 1, chosen, nlo, hi + ihi, certain and icert)
            chosen.pop()
            if capped:
                return

    dfs(0, [], 0, 0, True)
    # Keyword arguments, not positional. The positional form silently mis-assigned
    # `pool_size`, `capped` and `nodes` the moment a field was inserted in the middle of
    # the dataclass -- a shift the type checker cannot see because they are all ints and
    # bools, and one that would have reported `capped` as a residual.
    return SearchResult(
        solutions=tuple(solutions),
        best_miss=best_miss,
        best_miss_ids=best_miss_ids,
        nearest_ids=nearest_ids,
        nearest_residual=nearest,
        pool_size=n,
        capped=capped,
        nodes=nodes,
    )


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


def _decompose(
    txn: BankTxn,
    payments: tuple[Payment, ...],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
    declared_paise: int = 0,
    settled_on=None,
) -> tuple[list[Candidate], RefusalCategory | None, str, float]:
    """
    Decompose one bank credit into the payments it covers, in a SINGLE search.

    Returns (candidates, refusal_category, reason, uniqueness_margin). Every outcome
    except "exactly one solution" declines to assign, and each declines for a *named*
    reason rather than silently returning nothing.

    **Why one function and not two.** `match_with_margin` used to build the candidate
    pool, call `match` (which rebuilt the pool and searched), and then search a second
    time purely to recover the margin -- two pools and two identical searches on exactly
    the credits where the search is most expensive, since a credit only reaches tier 3
    when tiers 1 and 2 have already declined it. The margin is not extra information
    obtained by looking again; it is a property of the `SearchResult` the first search
    already produced. Computing it here keeps the two derived from the same search by
    construction, so they cannot disagree.
    """
    pool = tier2_amount_date.candidate_pool(txn, payments, claimed, settled_on)
    if not pool:
        return [], None, "", 0.0

    # Pool larger than the search bound. Refuse rather than truncate: dropping
    # candidates to fit a cap could remove the true decomposition and leave a wrong one
    # looking unique -- the worst possible failure, a confident wrong answer.
    if len(pool) > cfg.MAX_POOL:
        return (
            [],
            RefusalCategory.POOL_EXCEEDED,
            f"candidate pool is {len(pool)} payments, above MAX_POOL={cfg.MAX_POOL}; "
            f"the decomposition cannot be searched exhaustively, so no answer is "
            f"claimed (truncating the pool could hide the true subset)",
            0.0,
        )

    tol = fees.tolerance_for(txn.credit)
    result = search(
        txn.credit, pool, invoices_by_no, tolerance=tol, declared_paise=declared_paise
    )

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
            0.0,
        )

    if not result.solutions:
        # The near miss is emitted as a CANDIDATE, and that is a deliberate widening of
        # what a candidate means here. It is not a viable decomposition -- the category
        # says plainly that nothing fits -- but it carries its own residual, so a row
        # reading "4 payments, +3700p" is self-describing rather than misleading, and it
        # is the difference between an exception a person can work and one they can only
        # escalate. Six of fifteen refusals carried nothing at all before this, including
        # the largest at Rs 45,673. See the 2026-09-03 audit, finding P1-3.
        # `best_miss_ids` when the search found a genuine near-rival, and otherwise the
        # closest subset it could reach at all. The fallback is what stops the largest
        # exception in the batch from arriving empty-handed: see SearchResult.nearest_ids
        # for why the two are tracked separately rather than by widening one threshold.
        near = []
        shown = result.best_miss_ids or result.nearest_ids
        if shown:
            payments = [p for p in pool if p.id in set(shown)]
            interval = fees.expected_credit_interval(
                payments, invoices_by_no, declared_paise
            )
            near.append(
                Candidate(
                    payment_ids=tuple(shown),
                    residual_paise=fees.residual(txn.credit, interval),
                    tier=TIER,
                    interval_lo=interval.lo,
                    interval_hi=interval.hi,
                    certain=interval.certain,
                )
            )
        return (
            near,
            RefusalCategory.NO_SUBSET_FITS,
            f"no subset of the {result.pool_size} candidates sums to {txn.credit}p "
            f"within {tol}p at k<={cfg.MAX_SUBSET_K}"
            + (
                # Whatever is being SHOWN gets described, so the sentence and the row
                # below it cannot disagree. The previous version described `best_miss`
                # only, and once the candidate could come from `nearest_ids` instead,
                # the refusal listed a subset the reason never mentioned.
                f"; the closest is {len(shown)} payment(s) "
                f"{_gap(txn.credit, shown, pool, invoices_by_no, declared_paise):+d}p away, "
            f"listed below"
                if shown
                else (
                    f" (closest miss {result.best_miss:+d}p)"
                    if result.best_miss is not None
                    else ""
                )
            ),
            0.0,
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
            0.0,
        )

    return [to_candidate(result.solutions[0])], None, "", uniqueness_margin(result, tol)


def match_with_margin(
    txn: BankTxn,
    payments: tuple[Payment, ...],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
    declared_paise: int = 0,
    settled_on=None,
) -> tuple[list[Candidate], RefusalCategory | None, str, float]:
    """
    `match`, plus the uniqueness margin for the accepted solution.

    The margin is carried onto the Assignment because it is a Layer 2 input to the
    composite confidence score: a lone solution with nothing else nearby deserves more
    confidence than a lone solution that only just outran a rival.
    """
    return _decompose(
        txn, payments, claimed, invoices_by_no, declared_paise, settled_on
    )
