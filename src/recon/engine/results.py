"""
Engine output types.

Every bank transaction leaves the matcher with exactly one of three verdicts:

    ASSIGN       -- a decomposition was found and it survived every check
    REFUSE       -- candidates existed, but the evidence did not identify one
    NO_CANDIDATE -- nothing in the pool could plausibly account for this credit

The three-way split is deliberate and load-bearing. Collapsing REFUSE into
NO_CANDIDATE would hide the single most informative thing the engine does; collapsing
it into ASSIGN would mean guessing. Reporting precision without also reporting refusal
rate lets either be gamed, which is why `docs/METRICS.md` requires the whole triple.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    ASSIGN = "assign"
    REFUSE = "refuse"
    NO_CANDIDATE = "no_candidate"


class RefusalCategory(str, Enum):
    """
    Why the engine declined. Each maps to a specific verification layer, so the
    exception list says which mechanism objected rather than merely that something did.
    """

    ORDER_DEPENDENT = "order_dependent_assignment"      # Layer 1, MR1 runtime gate
    MULTIPLE_CANDIDATES = "multiple_candidates"          # Layer 2, uniqueness
    SOLUTION_CAP_REACHED = "solution_cap_reached"        # Layer 2, >= MAX_SOLUTIONS
    # These two were ONE category, and the collapse was visible to operators. Every
    # instance at the reported seed fired on a pool of 1, 1, 1, 2, 4 or 4 -- nothing had
    # exceeded MAX_POOL=20 or k=6 -- while the exception told a human there were "too
    # many candidates to search exhaustively". The largest single exception in the batch
    # read that way about a credit with ONE candidate.
    #
    # They are also different facts about the world, and a different next step. A pool
    # above the bound means the engine declined to look; no subset fitting means it
    # looked exhaustively and the money genuinely is not accounted for by anything in
    # the window. The first is a limit of the search, the second is a finding.
    POOL_EXCEEDED = "pool_exceeded"                      # declined to search
    NO_SUBSET_FITS = "no_subset_fits"                    # searched; nothing accounts for it
    # FS_BELOW_THRESHOLD and FS_REVIEW_BAND lived here and were NEVER RAISED. Layer 3
    # gates on `Evidence.contradicts` alone, and measuring what the two-threshold band
    # would have done settles why:
    #
    #   wiring it would have refused 78 of 126 assignments on the primary batch and
    #   63 of 104 on the holdout -- every one of them CORRECT, and zero wrong ones saved.
    #
    # That is not a threshold needing adjustment, it is the wrong test for this engine.
    # 4.0/7.0 are Splink's record-linkage conventions, where name and reference ARE the
    # evidence. Here the amount channel is primary and Fellegi-Sunter corroborates it, so
    # a correct match routinely sits below +4 bits on names alone -- 39.7% of correct
    # assignments score `non_match` on non-amount evidence and are right anyway.
    #
    # `contradicts` already encodes the correct rule, and its docstring records the same
    # lesson learned the expensive way: treating silence as dissent refused 86 of 137
    # credits on the first attempt.
    NARRATION_COUNT_CONFLICT = "narration_count_conflict"  # credit says N, match says M
    CONTESTED_PAYMENT = "contested_payment"              # two credits, equal evidence
    AMOUNT_NAME_CONFLICT = "amount_name_conflict"        # layers disagree
    UNEXPLAINED_RESIDUAL = "unexplained_residual"        # layers disagree


@dataclass(frozen=True, slots=True)
class Candidate:
    """
    One decomposition the engine weighed -- usually viable, and on one refusal not.

    Refusals carry every candidate they saw, not just the best one. An exception that
    says "two subsets fit, here they both are, Rs 800 at risk" is actionable; one that
    says "ambiguous" is not.

    **`no_subset_fits` deliberately carries a candidate that does NOT fit**, and the
    widening is worth stating here because this is where a reader looks. That refusal
    means the search ran to completion and nothing summed within tolerance, so its
    candidate is the CLOSEST subset rather than a viable one. It is safe to report
    because the category already says nothing fits and because `residual_paise` travels
    with it: a row reading "4 payments, +3700p" describes itself. Without it, six of
    fifteen exceptions carried nothing at all -- including the largest, at Rs 45,673 --
    and "no combination accounts for this credit" is a fact an operator can do nothing
    with, where "these four come to Rs 37.00 less than it" is a bank charge to go and
    find. See REVIEW.md P1-3.
    """

    payment_ids: tuple[str, ...]
    residual_paise: int
    tier: str
    interval_lo: int
    interval_hi: int
    certain: bool
    fs_weight: float | None = None

    @property
    def size(self) -> int:
        return len(self.payment_ids)


@dataclass(frozen=True, slots=True)
class Assignment:
    bank_txn_id: str
    payment_ids: tuple[str, ...]
    invoice_nos: tuple[str, ...]
    tier: str
    residual_paise: int
    residual_tightness: float
    certain_fee: bool
    permutation_stability: float = 1.0
    uniqueness_margin: float | None = None
    fs_weight: float | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class Refusal:
    bank_txn_id: str
    category: RefusalCategory
    reason: str
    paise_at_risk: int
    candidates: tuple[Candidate, ...] = ()

    @property
    def rupees_at_risk(self) -> float:
        return self.paise_at_risk / 100.0


@dataclass(frozen=True, slots=True)
class MatchOutput:
    """
    One complete pass of the matching core.

    The permutation ensemble runs this K times over shuffled inputs and compares the
    assignments; anything not identical across all K was decided by iteration order
    rather than by the data, and is refused. So this type must be comparable
    independently of input ordering -- hence `assignment_map`.
    """

    assignments: tuple[Assignment, ...]
    refusals: tuple[Refusal, ...]
    no_candidate: tuple[str, ...]
    unassigned_payment_ids: tuple[str, ...]
    tier_counts: dict[str, int] = field(default_factory=dict)

    @property
    def assignment_map(self) -> dict[str, frozenset[str]]:
        """
        bank_txn_id -> the set of payments assigned to it.

        Order-independent by construction, which is what makes MR1's comparison across
        shuffled passes meaningful rather than an artefact of list ordering.
        """
        return {a.bank_txn_id: frozenset(a.payment_ids) for a in self.assignments}

    def summary(self) -> dict[str, int]:
        return {
            "assigned": len(self.assignments),
            "refused": len(self.refusals),
            "no_candidate": len(self.no_candidate),
            "unassigned_payments": len(self.unassigned_payment_ids),
        }
