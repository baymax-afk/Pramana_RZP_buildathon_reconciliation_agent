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
    materiality: object | None = None
    confidence_deciles: tuple[tuple[float, int, float], ...] = ()
    confidence_calibrated: bool = False
    throughput_records_per_s: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


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
) -> Scorecard:
    """
    Score one engine run.

    `out` came from the engine, which never saw `links`. That separation is the whole
    point, and it is enforced structurally: the engine's input type carries no paths,
    and an audit hook raises if anything under `recon.engine` opens the truth directory.
    """
    truth_by_txn = {l.bank_txn_id: l for l in links if l.bank_txn_id}

    # ---- assignments ----
    correct: list[str] = []
    wrong: list[str] = []
    precision_by_tier: dict[str, list[int]] = {}
    for a in out.assignments:
        link = truth_by_txn.get(a.bank_txn_id)
        ok = bool(
            link
            and link.expected_verdict == "assign"
            and set(link.payment_ids) == set(a.payment_ids)
        )
        (correct if ok else wrong).append(a.bank_txn_id)
        slot = precision_by_tier.setdefault(a.tier, [0, 0])
        slot[1] += 1
        if ok:
            slot[0] += 1

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
    credits_with_candidates = len(out.assignments) + len(out.refusals)
    payments_assigned = sum(len(a.payment_ids) for a in out.assignments)

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
    assigned_txns = {
        a.bank_txn_id
        for a in out.assignments
        if (l := truth_by_txn.get(a.bank_txn_id))
        and l.expected_verdict == "assign"
        and set(l.payment_ids) == set(a.payment_ids)
    }
    any_assigned_txns = {a.bank_txn_id for a in out.assignments}
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

    return Scorecard(
        total_payments=total_payments,
        captured_payments=captured_payments,
        payments_assigned=payments_assigned,
        match_rate=_safe(payments_assigned, captured_payments),
        total_assignments=len(out.assignments),
        correct_assignments=len(correct),
        match_precision=_safe(len(correct), len(out.assignments)),
        wrong_assignments=tuple(wrong),
        credits_with_candidates=credits_with_candidates,
        total_refusals=len(out.refusals),
        refusal_rate=_safe(len(out.refusals), credits_with_candidates),
        correct_refusals=correct_refusals,
        conservative_refusals=conservative_refusals,
        refusal_correctness=_safe(correct_refusals, len(out.refusals)),
        no_candidate=len(out.no_candidate),
        no_candidate_by_relation=no_cand_by_rel,
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
    )


def _calibrated() -> bool:
    from recon.engine import confidence

    return confidence.is_calibrated()
