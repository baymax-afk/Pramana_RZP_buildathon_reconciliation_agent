"""
Layer 4 -- materiality stratification and projected error, following PCAOB AS 2315.

The audit profession has machinery for making a defensible statement about a population
nobody can fully verify. That is exactly the situation here: an engine may produce 125
assignments and no human is going to check all of them, so the question is what can
honestly be claimed about the ones nobody looks at.

The standard's answer, and this module's:

  * Set a **tolerable misstatement** in rupees (AS 2315 .18, .18A).
  * **Stratify**: verify 100% of items at or above materiality, sample below it (.22).
  * **Project** the sample's misstatement over the unsampled remainder, and report it
    with a confidence bound (.26, and fn. 5 for summing across strata).

**What this layer can and cannot do at runtime.** Projection needs an observed error
rate, and observing errors needs verification -- which at runtime means a human. So the
runtime output is a *verification plan*: which items must be checked in full, which
sample stands for the rest, and what the projection would be for any error rate that
sample turns up. The arithmetic is only exercised end-to-end offline, where the scorer
can supply real observed errors from ground truth. Presenting a projected error
computed from zero actual verification would be inventing assurance, which is the
opposite of the point.

The sample is drawn from the run seed, so the plan is reproducible: two people running
the same batch are asked to check the same items.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import config as cfg


@dataclass(frozen=True, slots=True)
class Stratum:
    name: str
    item_ids: tuple[str, ...]
    total_paise: int
    sampled_ids: tuple[str, ...]
    sampled_paise: int

    @property
    def size(self) -> int:
        return len(self.item_ids)

    @property
    def sample_size(self) -> int:
        return len(self.sampled_ids)

    @property
    def coverage(self) -> float:
        return self.sampled_paise / self.total_paise if self.total_paise else 1.0


@dataclass(frozen=True, slots=True)
class Projection:
    stratum: str
    sample_size: int
    observed_misstatements: int
    observed_paise: int
    projected_paise: int
    upper_bound_paise: int
    confidence: float
    method: str


@dataclass(frozen=True, slots=True)
class Plan:
    materiality_paise: int
    strata: tuple[Stratum, ...]
    total_paise: int
    above_materiality_paise: int
    below_materiality_paise: int
    projections: tuple[Projection, ...] = field(default_factory=tuple)

    @property
    def items_requiring_full_verification(self) -> int:
        return next((s.size for s in self.strata if s.name == "above_materiality"), 0)

    @property
    def total_projected_upper_paise(self) -> int:
        """
        Projected upper bound summed across strata (AS 2315 .26 fn. 5).

        Strata are projected separately and then summed -- projecting the combined
        population in one step would let the high-value stratum's behaviour stand in for
        the low-value one, which is the error stratification exists to prevent.
        """
        return sum(p.upper_bound_paise for p in self.projections)


def build_plan(
    items: dict[str, int],
    materiality_paise: int | None = None,
    sampling_rate: float | None = None,
    seed: int = cfg.SEED_PRIMARY,
) -> Plan:
    """
    Stratify `items` (id -> rupee value in paise) and draw the sampling plan.

    Everything at or above materiality goes into a stratum sampled at 100%: the standard
    does not permit sampling away an item that could on its own exceed tolerable
    misstatement.
    """
    m = materiality_paise if materiality_paise is not None else cfg.MATERIALITY_PAISE
    rate = sampling_rate if sampling_rate is not None else cfg.SAMPLING_RATE_BELOW_MATERIALITY

    above = {k: v for k, v in items.items() if v >= m}
    below = {k: v for k, v in items.items() if v < m}

    above_ids = tuple(sorted(above))
    above_stratum = Stratum(
        name="above_materiality",
        item_ids=above_ids,
        total_paise=sum(above.values()),
        sampled_ids=above_ids,  # 100% -- never sampled
        sampled_paise=sum(above.values()),
    )

    below_ids = tuple(sorted(below))
    n_sample = min(len(below_ids), max(0, round(len(below_ids) * rate)))
    rng = random.Random(seed + 4004)
    sampled = tuple(sorted(rng.sample(below_ids, n_sample))) if n_sample else ()
    below_stratum = Stratum(
        name="below_materiality",
        item_ids=below_ids,
        total_paise=sum(below.values()),
        sampled_ids=sampled,
        sampled_paise=sum(below[i] for i in sampled),
    )

    return Plan(
        materiality_paise=m,
        strata=(above_stratum, below_stratum),
        total_paise=sum(items.values()),
        above_materiality_paise=above_stratum.total_paise,
        below_materiality_paise=below_stratum.total_paise,
    )


def project(
    stratum: Stratum,
    observed_misstatement_paise: int,
    observed_count: int,
    confidence: float | None = None,
) -> Projection:
    """
    Project a stratum's sample misstatement over the whole stratum, with an upper bound.

    Two regimes, and the distinction matters:

    * **Zero misstatements observed.** The point projection is zero, but zero errors in
      a sample of n does NOT mean zero errors in the population. The 95% upper bound is
      the *rule of three*: with no failures in n independent trials, the true rate is
      below roughly 3/n at 95% confidence. Reporting "0 projected error" without that
      bound would be the single most misleading number this system could emit -- it
      would present the absence of observed error as the presence of assurance.

    * **Some misstatement observed.** Point projection scales the observed rate by the
      stratum's value, and the bound adds a normal-approximation allowance for sampling
      error.

    A 100%-verified stratum is not a sample at all: its projection is its observation,
    with no bound, because nothing was left unexamined.
    """
    conf = confidence if confidence is not None else cfg.PROJECTION_CONFIDENCE

    if stratum.sample_size >= stratum.size:
        return Projection(
            stratum=stratum.name,
            sample_size=stratum.sample_size,
            observed_misstatements=observed_count,
            observed_paise=observed_misstatement_paise,
            projected_paise=observed_misstatement_paise,
            upper_bound_paise=observed_misstatement_paise,
            confidence=1.0,
            method="100% verified -- observation, not projection",
        )

    n = max(1, stratum.sample_size)
    scale = (
        stratum.total_paise / stratum.sampled_paise if stratum.sampled_paise else 0.0
    )

    if observed_count == 0:
        upper_rate = 3.0 / n  # rule of three at ~95%
        return Projection(
            stratum=stratum.name,
            sample_size=stratum.sample_size,
            observed_misstatements=0,
            observed_paise=0,
            projected_paise=0,
            upper_bound_paise=int(upper_rate * stratum.total_paise),
            confidence=conf,
            method=f"rule of three: 0 errors in {n} sampled -> rate < 3/{n}",
        )

    projected = int(observed_misstatement_paise * scale)
    rate = observed_count / n
    se = (rate * (1 - rate) / n) ** 0.5
    allowance = 1.645 * se  # one-sided ~95%
    upper = int(projected + allowance * stratum.total_paise)
    return Projection(
        stratum=stratum.name,
        sample_size=stratum.sample_size,
        observed_misstatements=observed_count,
        observed_paise=observed_misstatement_paise,
        projected_paise=projected,
        upper_bound_paise=upper,
        confidence=conf,
        method=f"mean-per-unit projection, scale {scale:.2f}x, one-sided {conf:.0%}",
    )


def plan_for_assignments(assignments, credits_by_id: dict[str, int], seed: int) -> Plan:
    """
    Build the verification plan over ACCEPTED assignments.

    The accepted set is the right population, not the exception list. Exceptions are
    already going to a human by construction -- they are the output. The interesting
    and unexamined population is everything the engine decided to accept, because that
    is what would post without anybody looking at it.
    """
    items = {a.bank_txn_id: credits_by_id.get(a.bank_txn_id, 0) for a in assignments}
    return build_plan(items, seed=seed)
