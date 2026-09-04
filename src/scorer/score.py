"""
Scoring the engine's output against ground truth.

Every metric here is defined in `docs/METRICS.md` with its numerator and denominator,
and the definitions are repeated in the docstrings below so the code and the document
cannot drift apart silently.

**Why the headline is four numbers rather than one.** Once an engine may refuse,
precision alone is trivially gamed: refuse everything and precision is 1.0 over an
empty set. Coverage alone is gamed in the other direction: assign everything and
coverage is 1.0 while precision collapses. Neither means anything without the other,
and neither means anything without knowing how often refusing was the *right* call. So
match rate, match precision, refusal rate and refusal correctness are always reported
together.

**Refusals are scored in two ways, and the distinction is the point.** A refusal where
ground truth also says "refuse" is correct. A refusal where ground truth says "assign"
is a MISS -- the engine failed to do work it should have done -- but it is not an
ERROR: no money was posted anywhere. A wrong assignment moves money to the wrong place;
a conservative refusal leaves it on a human's desk. Collapsing those two into one
number would treat caution and error as equivalent, which is exactly the confusion this
project exists to argue against.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from recon.engine.results import MatchOutput
from recon.schemas import TruthLink


# --------------------------------------------------------------------------
# Loading ground truth -- the one place in the codebase that may do this
# --------------------------------------------------------------------------
def load_truth(truth_path: Path) -> tuple[dict, tuple[TruthLink, ...]]:
    raw = json.loads(truth_path.read_text(encoding="utf-8"))
    links = tuple(
        TruthLink(
            bank_txn_id=l["bank_txn_id"],
            payment_ids=tuple(l["payment_ids"]),
            invoice_nos=tuple(l["invoice_nos"]),
            defect_labels=tuple(l["defect_labels"]),
            relation=l["relation"],
            expected_verdict=l["expected_verdict"],
        )
        for l in raw["links"]
    )
    return raw, links


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ShortfallTxn:
    """
    One credit ground truth says should have been assigned, and was not.

    The count alone (`short_of_ceiling`) says how far the engine is from what the data
    permits. It does not say WHICH money, and "we are 5 payments short" is a weaker
    sentence than "we are 5 payments short and here they are, with the reason each one
    was refused". Naming them is what turns the gap from a caveat into a claim -- the
    engine knows exactly what it does not know, and can point at it.

    `engine_verdict` is what the engine actually did, so a reader can see the miss was a
    REFUSAL and not a wrong post. Every entry here cost coverage; none cost precision.
    """

    bank_txn_id: str
    payment_ids: tuple[str, ...]
    defect_labels: tuple[str, ...]
    relation: str
    engine_verdict: str
    paise: int


@dataclass(frozen=True, slots=True)
class Scorecard:
    # Coverage
    total_payments: int
    captured_payments: int
    payments_assigned: int
    match_rate: float

    # Correctness of what was assigned
    total_assignments: int
    correct_assignments: int
    match_precision: float
    wrong_assignments: tuple[str, ...]
    # `precision_ci_lower` qualifies `match_precision` and lives with the other
    # defaulted fields below, because dataclass ordering requires it.

    # Refusal behaviour
    credits_with_candidates: int
    total_refusals: int
    refusal_rate: float
    correct_refusals: int
    conservative_refusals: int
    refusal_correctness: float

    # What was never reached at all
    no_candidate: int
    no_candidate_by_relation: dict[str, int]


    # Detail
    exceptions_by_category: dict[str, int]
    paise_at_risk_by_category: dict[str, int]
    precision_by_tier: dict[str, tuple[int, int]]
    recall_by_relation: dict[str, tuple[int, int]]
    # Per DEFECT category, which is the finer and more useful cut: `relation` says how
    # many payments a credit covers, `defect` says what makes it hard.
    outcome_by_defect: dict[str, tuple[int, int, int, int]]
    ambiguity_case_verdict: str
    # The reachable ceiling: how much of the batch ground truth says CAN be matched.
    # Match rate alone invites comparison against 100%, and 100% is not available -- a
    # payment that never settled has no bank credit to match, and one the model cannot
    # represent (a split settlement) is refused correctly. Reporting the gap to the
    # ceiling instead says how much of the shortfall is the engine's, which is the
    # number worth arguing about.
    # Exact two-sided 95% Clopper-Pearson lower bound on `match_precision`. Reported
    # because 1.0000 on 126 assignments and 1.0000 on 126,000 are the same number and
    # not the same claim -- and this project cites the 99.9% automated-matching standard
    # while supporting about 97.1%. An external reviewer did that arithmetic before we
    # did, which is the reason it is now in the report rather than in their notes.
    precision_ci_lower: float = 0.0
    reachable_payments: int = 0
    ceiling: float = 0.0
    short_of_ceiling: int = 0
    shortfall_by_defect: dict[str, int] = field(default_factory=dict)
    # The same shortfall, NAMED. A count is a caveat; a list you can click through
    # to the engine's own reason for each one is a claim.
    short_of_ceiling_txns: tuple[ShortfallTxn, ...] = ()
    materiality: object | None = None
    confidence_deciles: tuple[tuple[float, int, float], ...] = ()
    confidence_calibrated: bool = False
    throughput_records_per_s: float | None = None
    # Bank lines the engine structurally never reads. Not a score -- a disclosure.
    #
    # This was every debit on the statement until Layer 2c existed. It is now the lines
    # that remain outside the engine's reach after debits were brought in, and the field
    # is KEPT rather than deleted: a disclosure that goes to zero is worth reporting as
    # zero, and re-deriving it every run is what would catch a future blind spot.
    unexamined_lines: int = 0
    unexamined_paise: int = 0
    # ---- the reversal ledger, reported as its own triple ----
    #
    # Deliberately not folded into match_precision. A reversal is a verdict about a
    # DEBIT; adding debits to the credit denominators would move the headline match rate
    # by counting lines that are not matches. Kept separate so it can be read on its own
    # terms, or ignored, without either number contaminating the other.
    reversals_found: int = 0
    reversals_expected: int = 0
    reversals_correct: int = 0
    reversals_wrong: tuple[str, ...] = ()
    reversals_missed: tuple[str, ...] = ()
    unexplained_debits: int = 0
    unexplained_debit_paise: int = 0
    reversed_paise: int = 0
    # ---- settlement groups (Layer 2b) ----
    settlement_groups: int = 0
    grouped_credits: int = 0
    grouped_payments: int = 0
    grouped_paise: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def clopper_pearson_lower(successes: int, trials: int, alpha: float = 0.05) -> float:
    """
    Exact lower bound of a two-sided `1 - alpha` Clopper-Pearson interval.

    **Why this is here at all.** This project reports `match precision 1.0000` beside
    `ARCHITECTURE.md`'s citation of the industry 99.9% standard for fully automated
    matching, and until now said nothing about how little 126 observations can support. An
    external reviewer did the arithmetic instead: zero errors in 127 gives a 95% lower
    bound of **97.14%**, not 99.9%. They were right, and the number belongs in the report
    rather than in a reviewer's notes.

    **Exact, not normal-approximate.** A Wald interval on a proportion of 1.0 has zero
    width -- it would print `1.0000 +/- 0.0000` and say the opposite of the truth. Clopper-
    Pearson inverts the binomial test and stays correct at the boundary, which is the only
    place this project's numbers ever sit.

    **Stdlib only.** The engine has no dependencies and this must not add one, so the
    bound is found by bisecting the exact binomial tail rather than by calling a Beta
    quantile. At n in the hundreds that is instant and there is no approximation to
    disclose.

    For all-successes the closed form is `(alpha/2) ** (1/n)`, and the bisection is
    asserted against it in `tests/test_confidence_interval.py` -- a check on the search,
    not a shortcut around it.
    """
    if trials <= 0:
        return 0.0
    if successes <= 0:
        return 0.0
    if successes > trials:
        raise ValueError(f"{successes} successes in {trials} trials")

    target = alpha / 2.0

    def tail_at_least(p: float) -> float:
        """P(X >= successes | trials, p) -- the probability of a result this good."""
        if p <= 0.0:
            return 0.0
        if p >= 1.0:
            return 1.0
        return sum(
            math.comb(trials, k) * (p**k) * ((1.0 - p) ** (trials - k))
            for k in range(successes, trials + 1)
        )

    lo, hi = 0.0, successes / trials
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if tail_at_least(mid) < target:
            lo = mid
        else:
            hi = mid
    return lo


def _safe(n: int, d: int) -> float:
    return n / d if d else 0.0


def score(
    out: MatchOutput,
    links: tuple[TruthLink, ...],
    total_payments: int,
    captured_payments: int,
    ambiguity_bank_txn_id: str = "",
    throughput: float | None = None,
    credits_by_id: dict[str, int] | None = None,
    seed: int = 0,
    unexamined: tuple[int, int] = (0, 0),
) -> Scorecard:
    """
    Score one engine run.

    `out` came from the engine, which never saw `links`. That separation is the whole
    point, and it is enforced structurally: the engine's input type carries no paths,
    and an audit hook raises if anything under `recon.engine` opens the truth directory.
    """
    truth_by_txn = {l.bank_txn_id: l for l in links if l.bank_txn_id}

    # ---- assignments ----
    #
    # Settlement-group members are scored HERE, alongside single-credit assignments, and
    # each member counts once. A group of two credits settling one payment is two
    # verdicts about two bank lines -- an operator sees two rows on the statement and
    # each is either right or wrong -- so counting the group as one assignment would
    # understate both the numerator and the denominator, and counting the payment twice
    # would double-count coverage. Ground truth agrees: the generator writes one link
    # per credit, both naming the same payment set.
    correct: list[str] = []
    wrong: list[str] = []
    precision_by_tier: dict[str, list[int]] = {}

    scored: list[tuple[str, tuple[str, ...], str]] = [
        (a.bank_txn_id, a.payment_ids, a.tier) for a in out.assignments
    ]
    scored += [
        (txn_id, g.payment_ids, g.tier)
        for g in out.groups
        for txn_id in g.bank_txn_ids
    ]
    for txn_id, payment_ids, tier in scored:
        link = truth_by_txn.get(txn_id)
        ok = bool(
            link
            and link.expected_verdict == "assign"
            and set(link.payment_ids) == set(payment_ids)
        )
        (correct if ok else wrong).append(txn_id)
        slot = precision_by_tier.setdefault(tier, [0, 0])
        slot[1] += 1
        if ok:
            slot[0] += 1

    # ---- reversals: the debit half ----
    #
    # Scored separately and never folded into `match_precision`. A reversal is a verdict
    # about a DEBIT, and putting debits into the credit denominators would move the
    # headline match rate by adding lines that are not matches. Reported as its own
    # triple so it can be read, or ignored, on its own terms.
    reversals_correct = 0
    reversals_wrong: list[str] = []
    for r in out.reversals:
        link = truth_by_txn.get(r.bank_txn_id)
        if (
            link
            and link.expected_verdict == "reverse"
            and set(link.payment_ids) == set(r.payment_ids)
        ):
            reversals_correct += 1
        else:
            reversals_wrong.append(r.bank_txn_id)
    reversals_expected = sum(1 for l in links if l.expected_verdict == "reverse")
    unexplained_debit_ids = {u.bank_txn_id for u in out.unexplained_debits}
    reversals_missed = tuple(
        sorted(
            l.bank_txn_id
            for l in links
            if l.expected_verdict == "reverse" and l.bank_txn_id in unexplained_debit_ids
        )
    )

    # ---- refusals ----
    # A refusal ground truth AGREES with is correct. A refusal ground truth wanted
    # assigned is a miss, not an error -- no money moved. They are counted separately.
    correct_refusals = 0
    conservative_refusals = 0
    for r in out.refusals:
        link = truth_by_txn.get(r.bank_txn_id)
        if link and link.expected_verdict == "refuse":
            correct_refusals += 1
        else:
            conservative_refusals += 1

    # ---- denominators ----
    # "Credits with candidates" excludes those where nothing plausibly fit. Declining
    # where nothing fits is an empty result, not a refusal, and folding it in would
    # flatter the refusal rate.
    credits_with_candidates = (
        len(out.assignments) + len(out.grouped_txn_ids) + len(out.refusals)
    )
    # Payments claimed by a group are counted ONCE, not once per member credit. This is
    # the coverage numerator, and a split settlement moves one payment however many
    # lines it arrived on.
    payments_assigned = len(
        {pid for a in out.assignments for pid in a.payment_ids}
        | {pid for g in out.groups for pid in g.payment_ids}
    )

    # ---- exceptions ----
    by_cat: dict[str, int] = {}
    risk_by_cat: dict[str, int] = {}
    for r in out.refusals:
        by_cat[r.category.value] = by_cat.get(r.category.value, 0) + 1
        risk_by_cat[r.category.value] = risk_by_cat.get(r.category.value, 0) + r.paise_at_risk

    # ---- recall per relation: what kinds of case does the engine actually reach? ----
    #
    # Counted against CORRECTLY assigned transactions, not merely assigned ones. Using
    # "was it assigned at all" credits the engine with recall for posting a credit to
    # entirely the wrong payments, which inflates the number precisely when the engine
    # is doing the most damage. It happens to agree while precision is 1.0, and would
    # diverge silently the moment it is not.
    assigned_txns = set(correct)
    any_assigned_txns = {t for t, _, _ in scored}
    recall: dict[str, list[int]] = {}
    for link in links:
        if not link.bank_txn_id or link.expected_verdict != "assign":
            continue
        slot = recall.setdefault(link.relation, [0, 0])
        slot[1] += 1
        if link.bank_txn_id in assigned_txns:
            slot[0] += 1

    # ---- outcome per DEFECT category ----
    #
    # `relation` only distinguishes one-to-one from many-to-one from partial, which
    # says how many payments a credit covers and nothing about what makes it hard. The
    # defect labels are the real taxonomy, and a credit usually carries several: an
    # `mdr_fee` + `settlement_drift` + `third_party_payer` credit is counted under all
    # three, because each is a separate claim about what the engine can cope with.
    #
    # Four counts, not one, and the split is the point. A defect the engine REFUSES is
    # not a failure when ground truth also expects a refusal -- `bank_charge` is
    # unmatchable by construction and declining it is the correct output. Collapsing
    # "missed" and "correctly refused" into a single recall figure would score the
    # engine down for being right.
    reversed_txns = {
        r.bank_txn_id
        for r in out.reversals
        if (l := truth_by_txn.get(r.bank_txn_id))
        and l.expected_verdict == "reverse"
        and set(l.payment_ids) == set(r.payment_ids)
    }
    any_reversed_txns = {r.bank_txn_id for r in out.reversals}
    per_defect: dict[str, list[int]] = {}
    for link in links:
        if not link.bank_txn_id:
            continue
        correctly_assigned = link.bank_txn_id in assigned_txns
        was_assigned = link.bank_txn_id in any_assigned_txns
        for label in link.defect_labels:
            slot = per_defect.setdefault(label, [0, 0, 0, 0])
            if link.expected_verdict == "assign":
                slot[0 if correctly_assigned else 1] += 1
            elif link.expected_verdict == "reverse":
                # Same four columns, read for a debit: handled correctly, handled
                # wrongly, left unexplained, or -- the fourth, which stays zero unless
                # something has gone badly wrong -- reversed against a settlement ground
                # truth does not name.
                if link.bank_txn_id in reversed_txns:
                    slot[0] += 1
                elif link.bank_txn_id in any_reversed_txns:
                    slot[1] += 1
                else:
                    slot[2] += 1
            else:
                slot[3 if was_assigned else 2] += 1

    no_cand_by_rel: dict[str, int] = {}
    for txn_id in out.no_candidate:
        link = truth_by_txn.get(txn_id)
        rel = link.relation if link else "unknown"
        no_cand_by_rel[rel] = no_cand_by_rel.get(rel, 0) + 1

    # ---- the ambiguity case ----
    if not ambiguity_bank_txn_id:
        verdict = "not present"
    elif ambiguity_bank_txn_id in any_assigned_txns:
        verdict = "ASSIGNED -- WRONG, it must be refused"
    elif ambiguity_bank_txn_id in {r.bank_txn_id for r in out.refusals}:
        verdict = "refused (correct)"
    elif ambiguity_bank_txn_id in set(out.no_candidate):
        verdict = "no_candidate (not yet reached -- tier 3 pending)"
    else:
        verdict = "unknown"

    # ---- Layer 4: verification plan, and the projection it implies ----
    plan = None
    if credits_by_id is not None:
        from recon.verify import materiality as mat

        plan = mat.plan_for_assignments(out.assignments, credits_by_id, seed)
        wrong_set = set(wrong)
        projections = []
        for stratum in plan.strata:
            # The scorer supplies what a human verifier would have found. This is the
            # ONLY place ground truth enters Layer 4, and it happens offline -- the
            # runtime layer emits the plan, not the projection.
            errs = [i for i in stratum.sampled_ids if i in wrong_set]
            projections.append(
                mat.project(
                    stratum,
                    observed_misstatement_paise=sum(
                        credits_by_id.get(i, 0) for i in errs
                    ),
                    observed_count=len(errs),
                )
            )
        plan = mat.Plan(
            materiality_paise=plan.materiality_paise,
            strata=plan.strata,
            total_paise=plan.total_paise,
            above_materiality_paise=plan.above_materiality_paise,
            below_materiality_paise=plan.below_materiality_paise,
            projections=tuple(projections),
        )

    # ---- confidence distribution, by decile ----
    from collections import defaultdict

    buckets: dict[int, list[bool]] = defaultdict(list)
    correct_set = set(correct)
    for a in out.assignments:
        if a.confidence is None:
            continue
        buckets[min(9, int(a.confidence * 10))].append(a.bank_txn_id in correct_set)
    deciles = tuple(
        (round(d / 10 + 0.05, 2), len(v), sum(v) / len(v))
        for d, v in sorted(buckets.items())
    )

    # ---- the reachable ceiling ----
    #
    # Derived from truth, not asserted: a captured payment is REACHABLE when some link
    # says a credit should be assigned to it. Everything else is unreachable for a
    # reason ground truth already records -- it never settled, or it belongs to a
    # relation the engine does not model -- and counting those against the engine scores
    # it for failing to do something nobody claims it can do.
    assigned_ids = {pid for a in out.assignments for pid in a.payment_ids} | {
        pid for g in out.groups for pid in g.payment_ids
    }
    reachable = {
        pid
        for link in links
        if link.bank_txn_id and link.expected_verdict == "assign"
        for pid in link.payment_ids
    }
    missed = reachable - assigned_ids
    shortfall: dict[str, int] = {}
    short_txns: list[ShortfallTxn] = []
    refusal_by_txn = {r.bank_txn_id: r.category.value for r in out.refusals}
    no_candidate_ids = set(out.no_candidate)
    for link in links:
        if not link.bank_txn_id or link.expected_verdict != "assign":
            continue
        n = sum(1 for pid in link.payment_ids if pid in missed)
        if not n:
            continue
        for label in link.defect_labels:
            shortfall[label] = shortfall.get(label, 0) + n
        # Which credit, and what the engine did instead. A miss that shows up here as
        # `assigned` would mean the engine posted this credit to a DIFFERENT payment --
        # a precision failure wearing a coverage failure's clothes -- so the verdict is
        # recorded rather than assumed.
        short_txns.append(
            ShortfallTxn(
                bank_txn_id=link.bank_txn_id,
                payment_ids=tuple(pid for pid in link.payment_ids if pid in missed),
                defect_labels=tuple(link.defect_labels),
                relation=link.relation,
                engine_verdict=(
                    refusal_by_txn.get(link.bank_txn_id)
                    or ("no_candidate" if link.bank_txn_id in no_candidate_ids else "")
                    or ("assigned_elsewhere" if link.bank_txn_id in out.assignment_map
                        else "credit absent from batch")
                ),
                paise=credits_by_id.get(link.bank_txn_id, 0) if credits_by_id else 0,
            )
        )
    short_txns.sort(key=lambda s: -s.paise)

    return Scorecard(
        total_payments=total_payments,
        captured_payments=captured_payments,
        payments_assigned=payments_assigned,
        match_rate=_safe(payments_assigned, captured_payments),
        # `scored` is assignments PLUS group members, one entry per bank line. Using
        # len(out.assignments) here would leave grouped credits out of the denominator
        # while `correct` already counted them, quietly reporting a precision above 1.0
        # the moment a group is right.
        total_assignments=len(scored),
        correct_assignments=len(correct),
        match_precision=_safe(len(correct), len(scored)),
        precision_ci_lower=clopper_pearson_lower(len(correct), len(scored)),
        wrong_assignments=tuple(wrong),
        credits_with_candidates=credits_with_candidates,
        total_refusals=len(out.refusals),
        refusal_rate=_safe(len(out.refusals), credits_with_candidates),
        correct_refusals=correct_refusals,
        conservative_refusals=conservative_refusals,
        refusal_correctness=_safe(correct_refusals, len(out.refusals)),
        no_candidate=len(out.no_candidate),
        no_candidate_by_relation=no_cand_by_rel,
        reachable_payments=len(reachable),
        ceiling=_safe(len(reachable), captured_payments),
        short_of_ceiling=len(missed),
        shortfall_by_defect=dict(
            sorted(shortfall.items(), key=lambda kv: -kv[1])
        ),
        short_of_ceiling_txns=tuple(short_txns),
        exceptions_by_category=by_cat,
        paise_at_risk_by_category=risk_by_cat,
        precision_by_tier={k: (v[0], v[1]) for k, v in precision_by_tier.items()},
        recall_by_relation={k: (v[0], v[1]) for k, v in recall.items()},
        outcome_by_defect={
            k: (v[0], v[1], v[2], v[3]) for k, v in sorted(per_defect.items())
        },
        ambiguity_case_verdict=verdict,
        materiality=plan,
        confidence_deciles=deciles,
        confidence_calibrated=_calibrated(),
        throughput_records_per_s=throughput,
        unexamined_lines=unexamined[0],
        unexamined_paise=unexamined[1],
        reversals_found=len(out.reversals),
        reversals_expected=reversals_expected,
        reversals_correct=reversals_correct,
        reversals_wrong=tuple(reversals_wrong),
        reversals_missed=reversals_missed,
        unexplained_debits=len(out.unexplained_debits),
        unexplained_debit_paise=sum(u.debit_paise for u in out.unexplained_debits),
        reversed_paise=out.reversed_paise,
        settlement_groups=len(out.groups),
        grouped_credits=len(out.grouped_txn_ids),
        grouped_payments=len({pid for g in out.groups for pid in g.payment_ids}),
        grouped_paise=sum(g.credit_paise for g in out.groups),
    )


def _calibrated() -> bool:
    from recon.engine import confidence

    return confidence.is_calibrated()
