"""
Fit the composite confidence weights, and measure whether the result is calibrated.

    python -m external.fit_calibration --report

**The population being calibrated matters more than the fitting method, and Block 8
established why.** Scoring only ACCEPTED assignments produces no spread: the engine
reaches the confidence stage solely for candidates where conservation already holds
tightly and uniqueness is already clean, because everything weaker was refused several
layers earlier. All 125 assignments landed in one decile at ~0.96, and a reliability
diagram over a single bucket is one point, not a curve.

So the fit runs over every candidate the engine CONSIDERED -- assigned and refused
alike. Refused candidates span the full range of evidence quality, which is exactly the
variation that accept-only lacks, and the resulting score answers a more useful
question: *given a proposed match with this evidence, how likely is it correct?* That is
the quantity a merchant needs when deciding whether to trust an auto-post, and it is the
quantity a reliability diagram can actually test.

**Held out by construction.** Fitting seeds are disjoint from the reported seeds. The
preferred source is BenchRec -- external, labelled, real Tier-1 bank data -- and the
fallback is generated batches at seeds the reported run never uses. The fallback is
weaker and is labelled as such everywhere it appears; it is held out in the seed sense
but not in the generator sense, so it cannot rule out that the engine and the generator
share an assumption. Never fitted on the reported run itself.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

import config as cfg
from recon.engine import confidence as conf
from recon.engine import fees
from recon.engine.match import match_once
from recon.generator import build

FEATURES = ("residual_tightness", "uniqueness_margin", "fs_scaled")

# Seeds used ONLY for fitting. Disjoint from SEED_PRIMARY and SEED_SECONDARY.
FIT_SEEDS = (11111, 22222, 33333, 44444, 55555, 66666)

# Densities to sample when collecting examples.
#
# Sampling only at the default density produces almost no negative examples: the engine
# refuses everything uncertain several layers before the confidence stage, so what
# survives is ~99% correct and every prediction lands in one bin. Errors only begin to
# appear when the candidate pool is crowded enough that coincidental collisions occur,
# which is what the top of this range is for. Calibration needs mistakes to calibrate
# against, and the density dial is the only honest way to produce them -- the
# alternative would be loosening a threshold until the engine started being wrong.
FIT_DENSITIES = (3, 6, 12, 24, 40)


@dataclass(frozen=True, slots=True)
class Example:
    residual_tightness: float
    uniqueness_margin: float
    fs_scaled: float
    correct: bool
    accepted: bool
    source: str

    def vector(self) -> tuple[float, ...]:
        return (self.residual_tightness, self.uniqueness_margin, self.fs_scaled)


# --------------------------------------------------------------------------
# Collecting examples
# --------------------------------------------------------------------------
def collect_from_generated(seeds=FIT_SEEDS, densities=FIT_DENSITIES) -> list[Example]:
    """
    Build examples from held-out generated batches.

    Both accepted assignments AND refused candidates are included. Excluding refusals
    would reproduce the very problem this fit exists to solve.
    """
    examples: list[Example] = []
    for seed in seeds:
        for ppw in densities:
            batch = build.generate(seed=seed, payments_per_window=ppw)
            out = match_once(batch.inputs)
            truth = {t.bank_txn_id: t for t in batch.truth if t.bank_txn_id}
            credits = {t.id: t.credit for t in batch.inputs.bank_txns}
            tag = f"generated:{seed}:ppw{ppw}"

            for a in out.assignments:
                link = truth.get(a.bank_txn_id)
                examples.append(
                    Example(
                        residual_tightness=a.residual_tightness,
                        uniqueness_margin=a.uniqueness_margin or 1.0,
                        fs_scaled=conf.fs_scaled(a.fs_weight),
                        correct=bool(
                            link
                            and link.expected_verdict == "assign"
                            and set(link.payment_ids) == set(a.payment_ids)
                        ),
                        accepted=True,
                        source=tag,
                    )
                )

            for r in out.refusals:
                if not r.candidates:
                    continue
                link = truth.get(r.bank_txn_id)
                best = min(r.candidates, key=lambda c: abs(c.residual_paise))
                interval = fees.NetInterval(
                    best.interval_lo, best.interval_hi, best.certain
                )
                credit = credits.get(r.bank_txn_id, 0)
                # A refused candidate's uniqueness margin is zero by definition when
                # several subsets fit -- that is what "refused for ambiguity" means.
                examples.append(
                    Example(
                        residual_tightness=fees.residual_tightness(credit, interval),
                        uniqueness_margin=0.0 if len(r.candidates) > 1 else 0.5,
                        fs_scaled=conf.fs_scaled(best.fs_weight),
                        correct=bool(
                            link
                            and link.expected_verdict == "assign"
                            and set(link.payment_ids) == set(best.payment_ids)
                        ),
                        accepted=False,
                        source=tag,
                    )
                )
    return examples


# How many labelled BenchRec pairs the calibration fit draws. See the note inside
# `collect_from_benchrec` for why this is capped and why the number is what it is.
BENCHREC_SAMPLE = 40_000


def collect_from_benchrec() -> list[Example]:
    """Map BenchRec's labelled pairs onto the same feature space. Raises if absent."""
    from external import benchrec_ingest as bri

    # Capped, and the cap is not arbitrary. `fit_logistic` is batch gradient descent in
    # pure Python -- 4,000 full passes -- and its docstring's "at a few thousand examples
    # this is fine" was written when the loader was broken and returned a degenerate
    # 37,000 rows fast. Against the real join it returns ~150,000 labelled pairs, and
    # 4,000 x 150,000 x 3 is several minutes of arithmetic to answer a question that
    # 40,000 examples settles: a reliability curve needs spread across its bins, not
    # sample size. The cap is stated here rather than hidden in a default so that anyone
    # reading the ECE knows what it was computed over.
    pairs = bri.load_pairs(limit=BENCHREC_SAMPLE)
    out: list[Example] = []
    for p in pairs:
        # Map onto the engine's own three features rather than onto whatever BenchRec
        # happens to carry. A fit over features the engine cannot compute at runtime
        # would not be a fit for this engine.
        out.append(
            Example(
                # How tight is the amount? The engine's residual_tightness is 1.0 for an
                # exact hit and falls as the residual grows against tolerance.
                residual_tightness=max(0.0, 1.0 - p.amount_delta_ratio * 100.0),
                # How alone is this candidate in its block? A credit with one plausible
                # counterparty is a different proposition from one with forty.
                uniqueness_margin=1.0 / max(p.block_size, 1),
                # Non-amount evidence, on the same 0..1 scale the engine scales its
                # Fellegi-Sunter weight onto.
                fs_scaled=(
                    1.0 if p.ref_agrees_exact else 0.5 if p.ref_agrees_partial else 0.0
                ),
                correct=p.is_match,
                accepted=True,
                source="benchrec",
            )
        )
    _reject_degenerate_labels(out)
    return out


def _reject_degenerate_labels(examples: list[Example]) -> None:
    """
    Refuse a label set that is all one class, because that is a broken join, not data.

    This guard exists because its absence cost a day. The first version of the ingest
    was written against a guessed schema and returned 37,123 rows every one of which was
    labelled negative; the fitter consumed them without complaint and reported a base
    rate of 0.000 with an ECE of 0.0032 over a single occupied bin. An ECE of 0.003
    reads like a good result, and nothing in the pipeline objected.

    BenchRec is a matched dataset: a sample of it containing no true matches, or no
    non-matches, is a defect in the reader. Failing loudly is the only correct behaviour
    -- see `DEFECT_LOG` 2026-09-04-08.
    """
    positives = sum(e.correct for e in examples)
    if not examples or positives == 0 or positives == len(examples):
        raise ValueError(
            f"BenchRec produced a degenerate label set: {positives} positives in "
            f"{len(examples)} examples. A matched dataset yielding one class means the "
            "join failed; refusing to fit rather than reporting a calibration over it."
        )


# --------------------------------------------------------------------------
# Logistic regression, in plain Python
# --------------------------------------------------------------------------
def fit_logistic(
    examples: list[Example], iterations: int = 4000, lr: float = 0.05, l2: float = 1e-3
) -> dict[str, float]:
    """
    Batch gradient descent with L2 regularisation. No numpy in this environment, and at
    a few thousand examples and three features it is not needed.

    The L2 term is not decoration. Accepted examples are close to perfectly separable --
    almost all of them are correct -- and unregularised logistic regression on separable
    data drives weights to infinity, producing confidences of exactly 1.0 that are pure
    artefact. The penalty keeps the fitted score honest about what three coarse features
    can actually support.
    """
    w = {f: 0.0 for f in FEATURES}
    b = 0.0
    n = max(1, len(examples))

    for _ in range(iterations):
        gw = {f: 0.0 for f in FEATURES}
        gb = 0.0
        for ex in examples:
            z = b + sum(w[f] * v for f, v in zip(FEATURES, ex.vector()))
            pred = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = pred - (1.0 if ex.correct else 0.0)
            for f, v in zip(FEATURES, ex.vector()):
                gw[f] += err * v
            gb += err
        for f in FEATURES:
            w[f] -= lr * (gw[f] / n + l2 * w[f])
        b -= lr * (gb / n)

    return {**w, "bias": b}


def predict(weights: dict[str, float], ex: Example) -> float:
    z = weights["bias"] + sum(weights[f] * v for f, v in zip(FEATURES, ex.vector()))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


# --------------------------------------------------------------------------
# Reliability and ECE
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Reliability:
    bins: tuple[tuple[float, int, float, float], ...]  # (mid, n, mean_pred, observed)
    ece: float
    n: int
    occupied_bins: int

    @property
    def is_meaningful(self) -> bool:
        """
        A reliability diagram needs spread. Three or fewer occupied bins is a handful of
        points, not a curve, and an ECE computed over it says almost nothing.
        """
        return self.occupied_bins >= 4 and self.n >= 100


def reliability(examples: list[Example], weights: dict[str, float], bins: int = 10) -> Reliability:
    buckets: dict[int, list[tuple[float, bool]]] = {}
    for ex in examples:
        p = predict(weights, ex)
        buckets.setdefault(min(bins - 1, int(p * bins)), []).append((p, ex.correct))

    rows = []
    ece = 0.0
    total = max(1, len(examples))
    for idx in sorted(buckets):
        vals = buckets[idx]
        mean_pred = sum(p for p, _ in vals) / len(vals)
        observed = sum(1 for _, c in vals if c) / len(vals)
        rows.append(((idx + 0.5) / bins, len(vals), mean_pred, observed))
        ece += (len(vals) / total) * abs(observed - mean_pred)

    return Reliability(tuple(rows), ece, len(examples), len(buckets))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
# Written beside the other generated artefacts rather than in the repository root. It is
# the output of an optional one-off fit and is read by nothing at runtime -- the fitted
# weights were deliberately NOT substituted into the engine, which is the finding -- so
# it is a report, and the root is for things a reader needs on arrival.
CALIBRATION_FILE = cfg.REPORTS / "calibration.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="print the reliability diagram")
    ap.add_argument("--write", action="store_true", help="persist the fitted weights")
    args = ap.parse_args(argv)

    from external import benchrec_ingest as bri

    avail = bri.availability()
    if avail:
        examples = collect_from_benchrec()
        source = "BenchRec (external, labelled, real Tier-1 bank data)"
        caveat = ""
    else:
        examples = collect_from_generated()
        source = f"generated batches at held-out seeds {FIT_SEEDS}"
        caveat = (
            "FALLBACK. BenchRec could not be read, so the fit uses this project's own\n"
            "    generator at seeds the reported run never touches. That is held out in\n"
            "    the SEED sense but not in the GENERATOR sense: it cannot rule out that\n"
            "    the engine and the generator share an assumption, so a calibration\n"
            "    result here is weaker evidence than the same result on BenchRec would\n"
            "    be. Reported as such rather than quietly substituted.\n"
            f"    {avail.note}"
        )

    weights = fit_logistic(examples)
    rel = reliability(examples, weights)

    print("=" * 78)
    print("  CONFIDENCE CALIBRATION")
    print("=" * 78)
    print(f"  source        : {source}")
    if caveat:
        print(f"  CAVEAT        : {caveat}")
    print(f"  examples      : {rel.n}  "
          f"({sum(1 for e in examples if e.accepted)} accepted, "
          f"{sum(1 for e in examples if not e.accepted)} refused candidates)")
    print(f"  base rate     : {sum(1 for e in examples if e.correct) / max(1, rel.n):.3f} correct")
    print(f"\n  fitted weights:")
    for k, v in weights.items():
        print(f"     {k:22} {v:+.4f}")

    print(f"\n  RELIABILITY  (bins occupied: {rel.occupied_bins}/10)")
    print(f"  {'bucket':>8} {'n':>6} {'mean predicted':>15} {'observed':>10} {'gap':>8}")
    for mid, n, pred, obs in rel.bins:
        print(f"  {mid:>8.2f} {n:>6} {pred:>15.3f} {obs:>10.3f} {obs - pred:>+8.3f}")
    print(f"\n  ECE = {rel.ece:.4f}   over n={rel.n}")

    if not rel.is_meaningful:
        print(
            "\n  NOT A MEANINGFUL CALIBRATION CURVE.\n"
            f"  Only {rel.occupied_bins} of 10 bins are occupied. A reliability diagram\n"
            "  needs spread in the predicted probability; with this few points the ECE\n"
            "  is a summary of a handful of buckets rather than evidence that the score\n"
            "  means what it says. Reported rather than suppressed."
        )
    else:
        print("\n  Spread is sufficient for the curve to be interpretable.")

    if args.write:
        CALIBRATION_FILE.write_text(
            json.dumps(
                {
                    "weights": weights,
                    "source": source,
                    "fallback": bool(caveat),
                    "ece": rel.ece,
                    "n": rel.n,
                    "occupied_bins": rel.occupied_bins,
                    "meaningful": rel.is_meaningful,
                    "fit_seeds": list(FIT_SEEDS) if not avail else [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  written to {CALIBRATION_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
