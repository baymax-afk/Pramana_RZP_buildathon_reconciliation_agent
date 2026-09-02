"""
The composite confidence score -- where the four verification layers combine.

    confidence = permutation_gate x sigmoid( w1*residual_tightness
                                           + w2*uniqueness_margin
                                           + w3*fs_scaled
                                           + b )

This is deliberately NOT a fuzzy string-similarity score dressed up as a probability.
Every input is an output of a verification layer that works without ground truth:

    residual_tightness   Layer 1 / MR4 -- how exactly conservation holds
    uniqueness_margin    Layer 2       -- how far the next-best subset sat
    fs_scaled            Layer 3       -- non-amount evidence, through the two thresholds
    permutation_gate     Layer 1 / MR1 -- whether the answer was data-determined

**The gate is a multiplier, not a term.** An order-dependent assignment is not a
low-confidence assignment; it is evidence the data did not determine the answer at all.
Folding it in as a weighted term would let it be auto-posted anyway at a slightly lower
score, which is precisely the behaviour the gate exists to prevent. It multiplies by
zero or it multiplies by one.

**THE WEIGHTS ARE NOT FITTED YET.** They are disclosed constants chosen to order the
signals sensibly, and a number produced from them is an ORDERING, not a calibrated
probability -- 0.9 here does not yet mean "right 90% of the time". Fitting happens in
Block 8b against BenchRec, which is external and labelled; fitting them against this
run's own ground truth would breach the isolation boundary and would make the
calibration claim circular besides. `is_calibrated()` reports which state the module is
in, and the metrics block prints it, so an uncalibrated score is never quoted as though
it were a calibrated one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import config as cfg

# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------
# Unfitted, disclosed. Ordering rationale, not empirical:
#   - conservation carries most weight: it is the hardest evidence available, an
#     arithmetic identity over money rather than an inference about identity;
#   - uniqueness is next: it says nothing corroborates the answer, only that nothing
#     else contradicts it;
#   - Fellegi-Sunter is weighted lowest of the three because names are the softest
#     channel and the one most likely to agree by coincidence.
# The bias is negative so that an assignment with no positive evidence at all does not
# start at 0.5.
UNFITTED_WEIGHTS = {
    "residual_tightness": 3.0,
    "uniqueness_margin": 2.0,
    "fs_scaled": 1.5,
    "bias": -2.5,
}

FITTED_WEIGHTS: dict[str, float] | None = None
"""Populated by src/external/fit_calibration.py once BenchRec has been fitted."""

WEIGHT_SOURCE = "unfitted disclosed constants -- NOT calibrated (see Block 8b)"


def is_calibrated() -> bool:
    return FITTED_WEIGHTS is not None


def weights() -> dict[str, float]:
    return FITTED_WEIGHTS or UNFITTED_WEIGHTS


# --------------------------------------------------------------------------
# Scaling the Fellegi-Sunter weight
# --------------------------------------------------------------------------
def fs_scaled(fs_weight: float | None) -> float:
    """
    Map a Fellegi-Sunter match weight into [0, 1] through the two-threshold band.

    Absence of evidence maps to 0.5 -- neutral -- rather than 0. A settlement batch
    carries no payer name because it covers many payers; that is not weak evidence
    against the match, and scoring it as such would systematically depress confidence on
    exactly the many-to-one decompositions Layer 2 works hardest to earn.

    Within the clerical-review band the contribution is capped, because that band is a
    formalised "I don't know": evidence sitting there must not be able to push an
    assignment into high confidence on its own.

    **This function must be monotonically non-decreasing in `fs_weight`, and once was
    not.** The sub-threshold branch read `0.5 + w / (2 * LOWER)`, which put weight 3.9 --
    evidence too weak to reach the review band at all -- at 0.9875, while weight 4.0,
    the first value strong enough to enter that band, scored 0.5. Stronger evidence
    scored lower, for every assignment whose FS weight fell below the lower threshold.

    The sub-threshold branch now maps into [0, 0.5): it is a fraction of the way TOWARDS
    the review band, never past it. Zero weight (evidence exactly balanced) is 0.0, not
    0.5 -- neutrality is what `fs_weight is None` means, and conflating "no evidence"
    with "evidence that cancelled out" was what made the old branch look plausible.
    `tests/test_layer4_confidence.py` now asserts monotonicity directly.
    """
    if fs_weight is None:
        return 0.5
    if fs_weight >= cfg.FS_THRESHOLD_UPPER:
        return 1.0
    if fs_weight >= cfg.FS_THRESHOLD_LOWER:
        span = cfg.FS_THRESHOLD_UPPER - cfg.FS_THRESHOLD_LOWER
        frac = (fs_weight - cfg.FS_THRESHOLD_LOWER) / span if span else 0.0
        return min(cfg.FS_REVIEW_CONFIDENCE_CAP, 0.5 + 0.1 * frac)
    return max(0.0, min(0.5, fs_weight / (2 * cfg.FS_THRESHOLD_LOWER)))


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """
    The score plus every input that produced it.

    The breakdown is kept because a bare number is unauditable. An exception list that
    says "0.62" is far less useful than one saying conservation held to the paisa,
    nothing else came close, and the name channel had nothing to say.
    """

    confidence: float
    residual_tightness: float
    uniqueness_margin: float
    fs_scaled: float
    permutation_stability: float
    gate_open: bool
    calibrated: bool

    def explain(self) -> str:
        if not self.gate_open:
            return "refused: assignment was not stable under input reordering"
        parts = [
            f"conservation {self.residual_tightness:.2f}",
            f"uniqueness {self.uniqueness_margin:.2f}",
            f"name/reference {self.fs_scaled:.2f}",
        ]
        suffix = "" if self.calibrated else " (uncalibrated ordering, not a probability)"
        return f"{self.confidence:.3f} = " + " + ".join(parts) + suffix


def score(
    residual_tightness: float,
    uniqueness_margin: float | None,
    fs_weight: float | None,
    permutation_stability: float = 1.0,
) -> ConfidenceBreakdown:
    """Combine the layers into one number in [0, 1]."""
    w = weights()
    gate_open = permutation_stability >= 1.0
    fsv = fs_scaled(fs_weight)
    uniq = 1.0 if uniqueness_margin is None else uniqueness_margin

    z = (
        w["residual_tightness"] * residual_tightness
        + w["uniqueness_margin"] * uniq
        + w["fs_scaled"] * fsv
        + w["bias"]
    )
    raw = 1.0 / (1.0 + math.exp(-z))

    return ConfidenceBreakdown(
        confidence=raw if gate_open else 0.0,
        residual_tightness=residual_tightness,
        uniqueness_margin=uniq,
        fs_scaled=fsv,
        permutation_stability=permutation_stability,
        gate_open=gate_open,
        calibrated=is_calibrated(),
    )


def score_assignment(a) -> ConfidenceBreakdown:
    """Convenience wrapper over an `Assignment`."""
    return score(
        residual_tightness=a.residual_tightness,
        uniqueness_margin=a.uniqueness_margin,
        fs_weight=a.fs_weight,
        permutation_stability=a.permutation_stability,
    )
