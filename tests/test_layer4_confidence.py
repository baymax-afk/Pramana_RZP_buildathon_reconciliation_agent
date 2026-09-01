"""
Layer 4 (materiality and projected error) and the composite confidence score.

The most important assertions here are about what these components must REFUSE to
claim. Layer 4's failure mode is manufacturing assurance from an unverified sample, and
the confidence score's failure mode is being quoted as a probability before it has been
calibrated. Both are tested for directly.
"""

from __future__ import annotations

import pytest

import config as cfg
from recon.engine import confidence as conf
from recon.engine.match import match_once
from recon.verify import materiality as mat


# --------------------------------------------------------------------------
# Stratification
# --------------------------------------------------------------------------
def test_items_at_or_above_materiality_are_never_sampled_away():
    """
    AS 2315 does not permit sampling away an item that could on its own exceed tolerable
    misstatement. The high stratum is verified at 100%, by construction.
    """
    items = {f"t{i}": v for i, v in enumerate([100, 5_000_00, 900_00, 12_000_00, 300])}
    plan = mat.build_plan(items, materiality_paise=500_000)
    above = next(s for s in plan.strata if s.name == "above_materiality")
    assert set(above.sampled_ids) == set(above.item_ids)
    assert above.coverage == 1.0


def test_stratification_partitions_every_item_exactly_once():
    items = {f"t{i}": (i + 1) * 10_000 for i in range(40)}
    plan = mat.build_plan(items)
    ids = [i for s in plan.strata for i in s.item_ids]
    assert sorted(ids) == sorted(items)
    assert len(ids) == len(set(ids))
    assert plan.total_paise == sum(items.values())


def test_sampling_plan_is_reproducible_from_the_seed():
    """Two people running the same batch must be asked to check the same items."""
    items = {f"t{i}": 1_000 for i in range(100)}
    a = mat.build_plan(items, seed=cfg.SEED_PRIMARY)
    b = mat.build_plan(items, seed=cfg.SEED_PRIMARY)
    c = mat.build_plan(items, seed=cfg.SEED_SECONDARY)
    below = lambda p: next(s for s in p.strata if s.name == "below_materiality")
    assert below(a).sampled_ids == below(b).sampled_ids
    assert below(a).sampled_ids != below(c).sampled_ids


# --------------------------------------------------------------------------
# Projection -- the honesty tests
# --------------------------------------------------------------------------
def test_zero_observed_errors_does_not_mean_zero_projected_error():
    """
    THE most misleading number this system could emit would be "projected error: 0"
    after checking five of twenty-one items. Zero failures in a sample bounds the rate;
    it does not establish that the rate is zero.

    The rule of three: with 0 failures in n independent trials the true rate is below
    about 3/n at 95% confidence.
    """
    stratum = mat.Stratum(
        name="below_materiality",
        item_ids=tuple(f"t{i}" for i in range(20)),
        total_paise=1_000_000,
        sampled_ids=tuple(f"t{i}" for i in range(5)),
        sampled_paise=250_000,
    )
    p = mat.project(stratum, observed_misstatement_paise=0, observed_count=0)
    assert p.projected_paise == 0
    assert p.upper_bound_paise > 0, "zero observed errors must still carry an upper bound"
    assert p.upper_bound_paise == pytest.approx(3 / 5 * 1_000_000, rel=0.01)
    assert "rule of three" in p.method


def test_a_fully_verified_stratum_reports_observation_not_projection():
    """Nothing was left unexamined, so there is nothing to project and no bound to add."""
    ids = tuple(f"t{i}" for i in range(6))
    stratum = mat.Stratum("above_materiality", ids, 900_000, ids, 900_000)
    p = mat.project(stratum, observed_misstatement_paise=12_345, observed_count=1)
    assert p.projected_paise == p.observed_paise == 12_345
    assert p.upper_bound_paise == 12_345
    assert "100%" in p.method


def test_observed_misstatement_scales_to_the_whole_stratum():
    stratum = mat.Stratum(
        "below_materiality", tuple(f"t{i}" for i in range(40)), 800_000,
        tuple(f"t{i}" for i in range(10)), 200_000,
    )
    p = mat.project(stratum, observed_misstatement_paise=5_000, observed_count=1)
    assert p.projected_paise == pytest.approx(5_000 * 4, rel=0.01)
    assert p.upper_bound_paise > p.projected_paise, "the bound must exceed the point estimate"


def test_strata_are_projected_separately_then_summed():
    """
    AS 2315 .26 fn. 5. Projecting the combined population in one step would let the
    high-value stratum's behaviour stand in for the low-value one -- exactly the error
    stratification exists to prevent.
    """
    ids_hi = ("a", "b")
    ids_lo = tuple(f"l{i}" for i in range(20))
    plan = mat.Plan(
        materiality_paise=500_000,
        strata=(
            mat.Stratum("above_materiality", ids_hi, 2_000_000, ids_hi, 2_000_000),
            mat.Stratum("below_materiality", ids_lo, 400_000, ids_lo[:5], 100_000),
        ),
        total_paise=2_400_000,
        above_materiality_paise=2_000_000,
        below_materiality_paise=400_000,
        projections=(
            mat.project(mat.Stratum("above_materiality", ids_hi, 2_000_000, ids_hi, 2_000_000), 1_000, 1),
            mat.project(mat.Stratum("below_materiality", ids_lo, 400_000, ids_lo[:5], 100_000), 0, 0),
        ),
    )
    assert plan.total_projected_upper_paise == sum(
        p.upper_bound_paise for p in plan.projections
    )


def test_plan_is_built_over_accepted_assignments_not_exceptions(batch):
    """
    Exceptions already go to a human by construction -- they are the output. The
    unexamined population is what the engine ACCEPTED, because that is what would post
    with nobody looking at it.
    """
    out = match_once(batch.inputs)
    credits = {t.id: t.credit for t in batch.inputs.bank_txns}
    plan = mat.plan_for_assignments(out.assignments, credits, batch.inputs.seed)
    covered = {i for s in plan.strata for i in s.item_ids}
    assert covered == {a.bank_txn_id for a in out.assignments}
    assert not covered & {r.bank_txn_id for r in out.refusals}


# --------------------------------------------------------------------------
# Composite confidence
# --------------------------------------------------------------------------
def test_permutation_gate_is_a_multiplier_not_a_term():
    """
    An order-dependent assignment is not a low-confidence assignment -- it is evidence
    the data did not determine the answer. As a weighted term it could be auto-posted at
    a slightly lower score, which is what the gate exists to prevent. It zeroes.
    """
    strong = conf.score(1.0, 1.0, 12.0, permutation_stability=1.0)
    unstable = conf.score(1.0, 1.0, 12.0, permutation_stability=0.875)
    assert strong.confidence > 0.5
    assert unstable.confidence == 0.0
    assert not unstable.gate_open


def test_absent_name_evidence_is_neutral_not_negative():
    """
    A settlement batch has no payer name because it covers many payers. Scoring that as
    weak evidence AGAINST would systematically depress confidence on exactly the
    many-to-one decompositions Layer 2 works hardest to earn.
    """
    assert conf.fs_scaled(None) == 0.5
    assert conf.fs_scaled(None) > conf.fs_scaled(-2.0)


def test_review_band_contribution_is_capped():
    """
    The clerical-review band is a formalised "I don't know". Evidence sitting there must
    not push an assignment to high confidence on its own.
    """
    mid = (cfg.FS_THRESHOLD_LOWER + cfg.FS_THRESHOLD_UPPER) / 2
    assert conf.fs_scaled(mid) <= cfg.FS_REVIEW_CONFIDENCE_CAP
    assert conf.fs_scaled(cfg.FS_THRESHOLD_UPPER) == 1.0


def test_confidence_is_monotonic_in_each_signal():
    base = conf.score(0.5, 0.5, 5.0).confidence
    assert conf.score(0.9, 0.5, 5.0).confidence > base
    assert conf.score(0.5, 0.9, 5.0).confidence > base
    assert conf.score(0.5, 0.5, 9.0).confidence > base


def test_score_is_declared_uncalibrated_until_it_is_fitted():
    """
    The score must never be quoted as a probability before fitting. `is_calibrated()`
    gates that claim and the metrics block prints the state, so an ordering is never
    passed off as "right 90% of the time".
    """
    assert conf.is_calibrated() is (conf.FITTED_WEIGHTS is not None)
    if not conf.is_calibrated():
        assert "NOT calibrated" in conf.WEIGHT_SOURCE
        assert not conf.score(1.0, 1.0, 9.0).calibrated


def test_breakdown_explains_itself(batch):
    """A bare number is unauditable; an exception list needs the reasoning."""
    out = match_once(batch.inputs)
    b = conf.score_assignment(out.assignments[0])
    text = b.explain()
    assert "conservation" in text and "uniqueness" in text
    assert "uncalibrated" in text or b.calibrated


def test_every_assignment_carries_a_confidence(batch):
    out = match_once(batch.inputs)
    assert all(a.confidence is not None for a in out.assignments)
    assert all(0.0 <= a.confidence <= 1.0 for a in out.assignments)


def test_confidence_currently_has_little_spread_and_that_is_reported(batch):
    """
    An honest limitation, pinned so it cannot be quietly forgotten.

    On this batch every assignment scores in a narrow band, because the engine only
    assigns when conservation is tight and uniqueness is clean -- so the inputs barely
    vary. A score that does not spread cannot be meaningfully calibrated, and the
    metrics block says so rather than presenting a single-bucket reliability table as
    evidence of calibration.
    """
    out = match_once(batch.inputs)
    spread = max(a.confidence for a in out.assignments) - min(
        a.confidence for a in out.assignments
    )
    assert spread < 0.25, (
        "spread has widened -- revisit the metrics block's caveat about calibration"
    )
