"""
Rendering the metrics block.

One block, one run, every number defined in `docs/METRICS.md`. It deliberately leads
with the four-number headline rather than a single figure, and it prints the tolerances
that produced the numbers alongside them -- a precision figure without its tolerance is
not interpretable, and putting them in separate places invites quoting one without the
other.

Anything not yet built is printed as "pending", never omitted. A metrics block that
silently drops the checks that have not landed yet reads as though they passed.
"""

from __future__ import annotations

import config as cfg

from .score import Scorecard

_RULE = "=" * 78
_THIN = "-" * 78


def _pct(x: float) -> str:
    return f"{x * 100:6.2f}%"


def render(
    sc: Scorecard,
    seed: int,
    payments_per_window: int,
    llm_enabled: bool,
    relations=None,
    ensemble=None,
) -> str:
    L: list[str] = []
    add = L.append

    add(_RULE)
    add(f"  RECONCILIATION METRICS   seed={seed}   density={payments_per_window}"
        f"   llm={llm_enabled}")
    add(_RULE)

    # ---- the headline: four numbers, never one ----
    add("")
    add("  HEADLINE  (all four, always -- see METRICS.md 'Why the headline is a triple')")
    add(_THIN)
    add(f"    match rate            {_pct(sc.match_rate)}"
        f"     {sc.payments_assigned}/{sc.captured_payments} captured payments assigned")
    add(f"    match precision       {_pct(sc.match_precision)}"
        f"     {sc.correct_assignments}/{sc.total_assignments} assignments correct")
    add(f"    refusal rate          {_pct(sc.refusal_rate)}"
        f"     {sc.total_refusals}/{sc.credits_with_candidates} credits with candidates")
    add(f"    refusal correctness   {_pct(sc.refusal_correctness)}"
        f"     {sc.correct_refusals}/{sc.total_refusals} refusals ground truth agrees with")
    if sc.conservative_refusals:
        add(f"      of which {sc.conservative_refusals} conservative: ground truth wanted an "
            f"assignment.")
        add(f"      These are MISSES, not errors -- no money was posted anywhere.")

    # ---- correctness detail ----
    add("")
    add("  ASSIGNMENT DETAIL")
    add(_THIN)
    for tier, (ok, total) in sorted(sc.precision_by_tier.items()):
        add(f"    {tier:<22} {ok}/{total} correct   ({_pct(_ratio(ok, total))})")
    if sc.wrong_assignments:
        add(f"    WRONG ASSIGNMENTS: {len(sc.wrong_assignments)} -> "
            f"{', '.join(sc.wrong_assignments[:6])}")
    else:
        add("    no incorrect assignments")

    # ---- what the engine reaches, by kind of case ----
    add("")
    add("  RECALL BY RELATION  (of cases ground truth expects to be assigned)")
    add(_THIN)
    for rel, (got, total) in sorted(sc.recall_by_relation.items()):
        add(f"    {rel:<22} {got}/{total}   ({_pct(_ratio(got, total))})")
    if sc.no_candidate:
        add(f"    no candidate found:   {sc.no_candidate}   {sc.no_candidate_by_relation}")

    # ---- exceptions ----
    add("")
    add("  EXCEPTIONS BY CATEGORY  (rupee-ranked)")
    add(_THIN)
    if sc.exceptions_by_category:
        ranked = sorted(
            sc.exceptions_by_category.items(),
            key=lambda kv: -sc.paise_at_risk_by_category.get(kv[0], 0),
        )
        for cat, n in ranked:
            risk = sc.paise_at_risk_by_category.get(cat, 0) / 100
            add(f"    {cat:<32} {n:>3}   Rs {risk:>14,.2f} at risk")
    else:
        add("    none")

    # ---- the centrepiece ----
    add("")
    add("  AMBIGUITY CASE")
    add(_THIN)
    add(f"    verdict: {sc.ambiguity_case_verdict}")

    # ---- the settings that produced all of the above ----
    add("")
    add("  TOLERANCES AND BOUNDS  (frozen in config.py before the run, never tuned)")
    add(_THIN)
    add(f"    TOL_ABS_PAISE          {cfg.TOL_ABS_PAISE}p"
        f"          TOL_REL_BPS  {cfg.TOL_REL_BPS} bps")
    add(f"    MDR_RATE_BAND          {cfg.MDR_RATE_BAND}"
        f"   GST_RATE     {cfg.GST_RATE}")
    add(f"    LOOKBACK_DAYS          {cfg.LOOKBACK_DAYS}"
        f"              (= window {cfg.SETTLEMENT_WINDOW_DAYS} + drift {cfg.MAX_SETTLEMENT_DRIFT_DAYS})")
    add(f"    MAX_POOL               {cfg.MAX_POOL}"
        f"             MAX_SUBSET_K {cfg.MAX_SUBSET_K}   MAX_SOLUTIONS {cfg.MAX_SOLUTIONS}")
    if sc.throughput_records_per_s:
        add(f"    throughput             {sc.throughput_records_per_s:,.0f} records/s")

    # ---- honesty about what is not built yet ----
    add("")
    add("  VERIFICATION LAYERS")
    add(_THIN)
    if relations is not None:
        total_v = sum(len(r.violations) for r in relations)
        add(f"    Layer 1  metamorphic relations   "
            f"{sum(1 for r in relations if r.passed)}/{len(relations)} pass, "
            f"{total_v} violation(s)")
        for rel in relations:
            mark = "pass" if rel.passed else f"FAIL x{len(rel.violations)}"
            add(f"               {rel.name}  {rel.kind:<12} checked={rel.checked:<5} {mark}")
            for v in rel.violations[:3]:
                add(f"                   ! {v.subject}: {v.detail[:88]}")
    else:
        add("    Layer 1  metamorphic relations MR1-MR6      not run (pass --verify)")

    if ensemble is not None:
        e = ensemble.summary()
        add(f"    Layer 1  runtime permutation gate  K={e['passes']}   "
            f"unstable {e['unstable']}/{e['txns_observed']}   "
            f"min stability {e['min_stability']:.3f}")
        if e["unstable"] == 0:
            add("               no assignment changed under reordering. The matcher sorts")
            add("               credits and refuses rather than choosing on ties, so it is")
            add("               order-independent by construction -- the gate is a live")
            add("               safety net, verified against a deliberately order-dependent")
            add("               matcher in tests/test_verification.py, not an unfired check.")
    else:
        add("    Layer 1  runtime permutation gate (K=8)     not run (pass --verify)")

    l2_refusals = sc.exceptions_by_category.get("multiple_candidates", 0) +         sc.exceptions_by_category.get("solution_cap_reached", 0)
    l2_assigned = sc.precision_by_tier.get("tier3_subsetsum", (0, 0))[1]
    add(f"    Layer 2  subset-sum uniqueness      {l2_assigned} unique decomposition(s) "
        f"assigned, {l2_refusals} refused as underdetermined")
    l3 = sc.exceptions_by_category.get("amount_name_conflict", 0)
    add(f"    Layer 3  Fellegi-Sunter          thresholds "
        f"{cfg.FS_THRESHOLD_LOWER:.0f}/{cfg.FS_THRESHOLD_UPPER:.0f} (Splink weights), "
        f"{l3} amount/name conflict(s) refused")
    add(f"               m from {cfg.FS_M_SOURCE[:52]}")
    add("               u estimated unsupervised from the batch; date excluded as the")
    add("               blocking key; absence of a name never counts as disagreement.")
    if sc.materiality is not None:
        p = sc.materiality
        add(f"    Layer 4  materiality Rs {p.materiality_paise / 100:,.0f} "
            f"(PCAOB AS 2315 .18/.22/.26)")
        for st in p.strata:
            add(f"               {st.name:<20} {st.size:>4} items  "
                f"Rs {st.total_paise / 100:>13,.2f}   sampled {st.sample_size:>3} "
                f"({st.coverage:.0%} of value)")
        for pr in p.projections:
            if pr.sample_size == 0:
                continue
            add(f"               projection[{pr.stratum}] {pr.observed_misstatements} "
                f"misstatement(s) in {pr.sample_size} -> projected "
                f"Rs {pr.projected_paise / 100:,.2f}, upper bound "
                f"Rs {pr.upper_bound_paise / 100:,.2f}")
            add(f"                   {pr.method}")
        full = p.items_requiring_full_verification
        pct = (p.above_materiality_paise / p.total_paise) if p.total_paise else 0
        add(f"               {full} of {sc.total_assignments} assignments sit at or above")
        add(f"               materiality ({pct:.0%} of assigned value) and require full")
        add(f"               verification -- sampling relieves only the remainder.")
    else:
        add("    Layer 4  materiality + projected error      not run")

    if sc.confidence_deciles:
        add("")
        add("  COMPOSITE CONFIDENCE  (Layer 1 x [conservation, uniqueness, Fellegi-Sunter])")
        add(_THIN)
        state = "CALIBRATED" if sc.confidence_calibrated else "UNCALIBRATED"
        add(f"    weights: {state} -- {_weight_source()}")
        if not sc.confidence_calibrated:
            add("    These scores are an ORDERING, not probabilities. 0.9 does NOT yet mean")
            add("    'right 90% of the time'. Fitting happens against BenchRec in Block 8b;")
            add("    fitting against this run's own ground truth would be circular.")
        add(f"    {'bucket':>8} {'n':>5} {'observed accuracy':>19}")
        for mid, n, acc in sc.confidence_deciles:
            add(f"    {mid:>8.2f} {n:>5} {acc * 100:>18.1f}%")
        add("    calibration curve / ECE                     pending (Block 8b)")
    add("")
    if ensemble is None:
        add("    Single-pass results only. Assignments have NOT been tested for")
        add("    order-dependence -- pass --verify to run the permutation gate.")
    else:
        add(f"    Assignments below survived the permutation gate at K={ensemble.passes}.")

    add("")
    add(_RULE)
    return "\n".join(L)


def _weight_source() -> str:
    from recon.engine import confidence

    return confidence.WEIGHT_SOURCE


def _ratio(n: int, d: int) -> float:
    return n / d if d else 0.0
