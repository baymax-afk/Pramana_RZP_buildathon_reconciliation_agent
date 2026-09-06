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
    # Layer 2b. A credit fits more than one settlement group, or a group's payments are
    # wanted by a rival group. Same doctrine as MULTIPLE_CANDIDATES one level up: the
    # constraint identified several groupings, so it identified none.
    AMBIGUOUS_GROUPING = "ambiguous_grouping"            # Layer 2b, group uniqueness
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
    find. See the 2026-09-03 audit, finding P1-3.
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
class SettlementGroup:
    """
    One payment set settled across SEVERAL bank credits -- the claim unit that
    `split_settlement` needs and a single `Assignment` cannot express.

    **Why this is a new type rather than a flag on `Assignment`.** Every invariant the
    engine rests on is stated per credit: MR4 checks that one credit equals the settled
    interval of the payments assigned to it, and MR5 refuses to see one payment claimed
    by two credits and calls it double-posting. Both are correct, and a part-settlement
    violates both by construction -- credit 1 is only half the payment, and credits 1
    and 2 name the same payment. Smuggling a group into `Assignment` would have made the
    engine's own conservation checks start failing on its own correct output, and the
    obvious next step -- relaxing them -- would have destroyed the thing they protect.

    So the invariants were not relaxed; they were RESTATED at the group level.
    Conservation holds over the group's total credit against the group's total settled
    interval, and each credit and each payment still belongs to exactly one verdict. A
    single-credit assignment is the degenerate case, and it keeps its own type because
    rewriting 500 tests' worth of scoring for a relation that occurs twice per batch
    would be the wrong trade the week before a deadline.

    **`ARCHITECTURE.md` predicted this would need fractional claims. It does not.**
    The claim unit had to stop being "one credit", but it did not have to become
    "(payment, fraction)": a part-settlement is a GROUP relation, and the group balances
    exactly. Fractions are only needed to post half a payment, which the same document
    already argues is a wrong answer rather than a partial one. The simpler model was
    available the whole time, behind an assumption nobody had tested.
    """

    bank_txn_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    invoice_nos: tuple[str, ...]
    credit_paise: int
    residual_paise: int
    residual_tightness: float
    certain_fee: bool
    tier: str = "layer2b_group"
    uniqueness_margin: float | None = None
    permutation_stability: float = 1.0
    confidence: float | None = None

    @property
    def size(self) -> int:
        return len(self.bank_txn_ids)


@dataclass(frozen=True, slots=True)
class Reversal:
    """
    A DEBIT that claws back money a credit already brought in.

    The engine read `t for t in inputs.bank_txns if t.is_credit` for the life of the
    project, so every debit -- chargeback, reversal, bank fee, payout -- was not
    matched, not refused and not counted. The gap went unnoticed because the generated
    statement contained no debits at all, so the engine had never been shown the half of
    a bank statement it ignores by construction.

    **A reversal does not delete the assignment it reverses, and that is deliberate.**
    The settlement happened and the claw-back happened; both are facts, and erasing the
    first would leave the books describing a batch that never occurred. MR5's accounting
    stays intact -- the credit keeps its single verdict, the payment keeps its single
    claim -- and the reversal is a second, later entry against the same money. What
    changes is the NET position, which is why the reconciled total is reported gross and
    net rather than silently as one number.

    `settled_by` is the credit being reversed, not the payment: a chargeback is raised
    against a settlement line, and tying it to the credit is what lets conservation be
    checked across the two events.
    """

    bank_txn_id: str
    settled_by: str
    payment_ids: tuple[str, ...]
    debit_paise: int
    reason: str
    evidence: tuple[str, ...] = ()
    # True when the debit claws back only PART of the settlement -- one or more of the
    # payments inside a batch, rather than the whole credit.
    #
    # Worth its own flag rather than being derived from the amounts, because the two are
    # different facts for an operator: a full reversal means the settlement is undone,
    # a partial one means the rest of that batch still stands and only these receivables
    # reopened. `payment_ids` says which.
    partial: bool = False


class DebitCategory(str, Enum):
    """
    Why a debit could not be tied to a settlement, in the same spirit as
    `RefusalCategory`: "unexplained" is not actionable, "reverses a settlement from an
    earlier statement" is.

    **These were one bucket, and collapsing them was costing an operator the answer.**
    Every debit the engine could not resolve carried the same sentence -- *"money left
    the account and this engine cannot say against what"* -- which is honest and nearly
    useless. It is true of a bank fee, of a claw-back on last month's settlement, and of
    a chargeback against a credit sitting in this batch's own exception list. Those are
    three different next steps: ignore it, go to the prior period, or work the linked
    exception and this one resolves with it.
    """

    # The reference names a settlement this batch does not contain. A claw-back on an
    # earlier statement -- real, unreconcilable HERE, and the operator's next step is a
    # different period rather than a different search.
    OUT_OF_BATCH = "reverses_a_settlement_outside_this_batch"
    # The reference names a credit in this batch that the engine REFUSED to post. The
    # debit is not independently resolvable and must not be used to justify the match
    # the engine declined -- but the two items are linked, and saying so gives an
    # ordering: clear the exception and this resolves with it.
    SETTLEMENT_REFUSED = "reverses_a_settlement_this_engine_refused"
    # Several posted settlements answer to the same reference and amount, or several
    # subsets of one settlement settled for this amount. Same doctrine as Layer 2: the
    # evidence identified more than one answer, so it identified none.
    AMBIGUOUS = "ambiguous_reversal"
    # No reference in the debit resolves to anything in the batch. A bank fee, a payout,
    # a transfer -- money leaving for a reason that is not a reversal at all.
    NO_SETTLEMENT_NAMED = "no_settlement_named"


@dataclass(frozen=True, slots=True)
class UnexplainedDebit:
    """
    A debit the engine could not tie to a settlement it had posted.

    Reported rather than dropped, and now CLASSIFIED rather than lumped. `category` says
    which of the four situations this is and `candidates` carries whatever was
    considered, on the same principle as `Refusal`. `depends_on` names the exception this
    debit is waiting behind, when there is one -- the only place in this engine where one
    item's resolution is stated to unblock another.
    """

    bank_txn_id: str
    debit_paise: int
    reason: str
    candidates: tuple[str, ...] = ()
    category: DebitCategory = DebitCategory.NO_SETTLEMENT_NAMED
    depends_on: str = ""

    @property
    def rupees(self) -> float:
        return self.debit_paise / 100.0


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
    groups: tuple[SettlementGroup, ...] = ()
    reversals: tuple[Reversal, ...] = ()
    unexplained_debits: tuple[UnexplainedDebit, ...] = ()
    # True when group resolution hit a search bound and therefore granted nothing.
    # A disclosure about the SEARCH, not a verdict about any credit -- which is why it
    # lives here and not in the refusal list. See config.MAX_GROUP_RESIDUE.
    group_search_truncated: bool = False

    @property
    def assignment_map(self) -> dict[str, frozenset[str]]:
        """
        bank_txn_id -> the set of payments assigned to it.

        Order-independent by construction, which is what makes MR1's comparison across
        shuffled passes meaningful rather than an artefact of list ordering.

        **Group members appear here too, and they must.** MR1 compares this map across
        K shuffled passes and refuses anything that moves; a settlement group left out
        of the map would be exempt from the permutation gate -- decided by iteration
        order with nothing checking. Each credit in a group maps to the group's payment
        set, which is exactly the claim being made about it.
        """
        m = {a.bank_txn_id: frozenset(a.payment_ids) for a in self.assignments}
        for g in self.groups:
            for txn_id in g.bank_txn_ids:
                m[txn_id] = frozenset(g.payment_ids)
        return m

    @property
    def grouped_txn_ids(self) -> frozenset[str]:
        return frozenset(t for g in self.groups for t in g.bank_txn_ids)

    @property
    def credit_verdicts(self) -> tuple[str, ...]:
        """
        Every credit-side verdict, WITH duplicates, so double-verdicts are detectable.

        A credit now leaves the matcher with one of FOUR outcomes -- assigned, settled
        inside a settlement group, refused, or no candidate -- and this property is the
        single statement of that. It exists because the three-way version of the same
        sentence had been written out by hand in MR5 and in four separate tests, and
        when the fourth outcome arrived every one of them silently began asserting that
        four correctly-settled credits had received no verdict at all. One place to
        change is the difference between an invariant and a copied incantation.
        """
        return tuple(
            [a.bank_txn_id for a in self.assignments]
            + sorted(self.grouped_txn_ids)
            + [r.bank_txn_id for r in self.refusals]
            + list(self.no_candidate)
        )

    @property
    def debit_verdicts(self) -> tuple[str, ...]:
        """The same, for the debit half: reversed, or reported unexplained."""
        return tuple(
            [r.bank_txn_id for r in self.reversals]
            + [u.bank_txn_id for u in self.unexplained_debits]
        )

    @property
    def claimed_payment_ids(self) -> tuple[str, ...]:
        """Every payment claimed by any verdict, single or grouped, with duplicates."""
        return tuple(
            [pid for a in self.assignments for pid in a.payment_ids]
            + [pid for g in self.groups for pid in g.payment_ids]
        )

    @property
    def reversed_paise(self) -> int:
        return sum(r.debit_paise for r in self.reversals)

    def summary(self) -> dict[str, int]:
        return {
            "assigned": len(self.assignments),
            "grouped_credits": len(self.grouped_txn_ids),
            "groups": len(self.groups),
            "refused": len(self.refusals),
            "no_candidate": len(self.no_candidate),
            "unassigned_payments": len(self.unassigned_payment_ids),
            "reversals": len(self.reversals),
            "unexplained_debits": len(self.unexplained_debits),
        }
