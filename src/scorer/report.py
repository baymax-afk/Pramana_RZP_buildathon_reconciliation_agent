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

import textwrap

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
    compare: tuple[Scorecard, int] | None = None,
) -> str:
    """
    The metrics block.

    `compare` adds a SECOND DENSITY beside the reported one, as (scorecard, ppw). The
    headline is the one place a reader looks, and a single density there invites the
    reading that the numbers are a property of the engine rather than of the engine at
    one crowding level. Density is the parameter the whole argument turns on: coverage
    is supposed to degrade under ambiguity while precision does not, and that is only
    visible if more than one arm is in front of you.
    """
    L: list[str] = []
    add = L.append

    add(_RULE)
    dens = (
        f"density={payments_per_window}"
        if compare is None
        else f"density={payments_per_window} vs {compare[1]}"
    )
    add(f"  RECONCILIATION METRICS   seed={seed}   {dens}   llm={llm_enabled}")
    add(_RULE)

    # ---- the headline: four numbers, never one ----
    add("")
    add("  HEADLINE  (all four, always -- see METRICS.md 'Why the headline is a triple')")
    add(_THIN)

    if compare is not None:
        other, other_ppw = compare
        add(f"    {'':<22}{f'ppw={payments_per_window}':>12}{f'ppw={other_ppw}':>12}"
            f"{'delta':>10}")
        for label, attr in (
            ("match rate", "match_rate"),
            ("match precision", "match_precision"),
            ("refusal rate", "refusal_rate"),
            ("refusal correctness", "refusal_correctness"),
        ):
            a, b = getattr(sc, attr), getattr(other, attr)
            add(f"    {label:<22}{_pct(a):>12}{_pct(b):>12}{(b - a) * 100:>+9.2f}pp")
        add("")
        add(f"    at ppw={payments_per_window}: "
            f"{sc.payments_assigned}/{sc.captured_payments} captured payments assigned, "
            f"{sc.correct_assignments}/{sc.total_assignments} assignments correct, "
            f"{sc.total_refusals}/{sc.credits_with_candidates} refused")
        add(f"    at ppw={other_ppw}: "
            f"{other.payments_assigned}/{other.captured_payments} captured payments "
            f"assigned, {other.correct_assignments}/{other.total_assignments} "
            f"assignments correct, "
            f"{other.total_refusals}/{other.credits_with_candidates} refused")
        # The bound belongs on BOTH headline branches. It first went only on the
        # single-density one, which is the branch the default invocation does not take --
        # so the figure a reader actually sees was the unqualified one.
        add(f"      precision 95% CI >= {sc.precision_ci_lower:.2%} at ppw="
            f"{payments_per_window} (exact, Clopper-Pearson, n={sc.total_assignments}); "
            f">= {other.precision_ci_lower:.2%} at ppw={other_ppw} "
            f"(n={other.total_assignments}). Neither sample can support more.")
    else:
        add(f"    match rate            {_pct(sc.match_rate)}"
            f"     {sc.payments_assigned}/{sc.captured_payments} captured payments assigned")
        add(f"    match precision       {_pct(sc.match_precision)}"
            f"     {sc.correct_assignments}/{sc.total_assignments} assignments correct")
        # The bound, printed with the number rather than under it. 1.0000 on 126 and
        # 1.0000 on 126,000 are the same figure and not the same claim.
        add(f"      95% CI >= {sc.precision_ci_lower:.2%} (exact, Clopper-Pearson, "
            f"n={sc.total_assignments}). {sc.total_assignments} observations cannot "
            f"support more.")
        add(f"    refusal rate          {_pct(sc.refusal_rate)}"
            f"     {sc.total_refusals}/{sc.credits_with_candidates} credits with candidates")
        add(f"    refusal correctness   {_pct(sc.refusal_correctness)}"
            f"     {sc.correct_refusals}/{sc.total_refusals} refusals ground truth agrees with")
    if sc.conservative_refusals:
        add(f"      of which {sc.conservative_refusals} conservative: ground truth wanted an "
            f"assignment.")
        add(f"      These are MISSES, not errors -- no money was posted anywhere.")

    # ---- the reachable ceiling ----
    #
    # A match rate invites comparison against 100%, and 100% is not on offer. Some
    # captured payments never settled, so no bank credit exists to match them; others
    # belong to a relation the engine does not model and are refused correctly. Reporting
    # the gap to what ground truth says is REACHABLE separates "the engine missed this"
    # from "nothing could have got this", which is the only part of the shortfall worth
    # arguing about -- and it is the difference between a defensive number and a claim.
    if sc.reachable_payments:
        add("")
        add(f"    reachable ceiling     {_pct(sc.ceiling)}"
            f"     {sc.reachable_payments}/{sc.captured_payments} payments ground truth "
            f"says CAN be matched")
        add(f"    short of the ceiling  {sc.short_of_ceiling:>6}"
            f"     payments the engine could have matched and did not")
        unreachable = sc.captured_payments - sc.reachable_payments
        add(f"      the other {unreachable} unmatched payment(s) are unreachable by "
            f"construction:")
        add(f"      they never settled, or they belong to a relation the engine does not")
        add(f"      model -- refusing those is the correct output, not a miss.")
        if sc.shortfall_by_defect:
            top = list(sc.shortfall_by_defect.items())[:4]
            add(f"      the shortfall carries: "
                + ", ".join(f"{label} x{n}" for label, n in top))

    if compare is not None:
        add("")
        add(f"    Everything below this block is the ppw={payments_per_window} run. The "
            f"second arm is")
        add("    reported for the headline only -- it is generated in-process, is not")
        add("    written to disk, and does not feed the exception list or the API.")

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

    # ---- what the engine never looked at ----
    #
    # A disclosure, not a score. The engine reads `is_credit` transactions only, so
    # every debit on the statement -- a chargeback, a reversal, a bank fee -- is
    # invisible to it: not matched, not refused, not counted. The statement carried no
    # debits at all until `chargeback_debit` existed, which is exactly why the blind
    # spot went unnoticed. Reporting the gap is the honest alternative to inventing a
    # ground-truth verdict the engine structurally cannot produce.
    if sc.unexamined_lines:
        add("")
        add("  NOT EXAMINED  (the engine reads credits only)")
        add(_THIN)
        add(f"    {sc.unexamined_lines} debit line(s) on the statement, "
            f"Rs {sc.unexamined_paise / 100:,.2f}")
        add("    Money LEAVING the account -- chargebacks, reversals, bank fees -- is")
        add("    outside the engine's model. It is not scored either way, because")
        add("    scoring it against a verdict the engine cannot produce would be")
        add("    theatre. It is disclosed so the exception list is not mistaken for a")
        add("    complete account of the statement.")

    # ---- the finer cut: what the engine does with each KIND of hard case ----
    if sc.outcome_by_defect:
        add("")
        add("  OUTCOME BY DEFECT  (a credit carrying several is counted under each)")
        add(_THIN)
        add(f"    {'defect':<22}{'matched':>9}{'missed':>8}{'refused':>9}"
            f"{'WRONGLY':>9}   note")
        add(f"    {'':<22}{'':>9}{'':>8}{'(correct)':>9}{'assigned':>9}")
        for label, (ok, miss, ref_ok, ref_bad) in sorted(
            sc.outcome_by_defect.items(), key=lambda kv: (-kv[1][3], -kv[1][1], kv[0])
        ):
            note = ""
            if ref_bad:
                note = "POSTED money truth says to refuse"
            elif miss:
                note = "conservative -- refused, no money posted"
            elif ref_ok and not ok:
                note = "unmatchable by construction; refusing is correct"
            add(f"    {label:<22}{ok:>9}{miss:>8}{ref_ok:>9}{ref_bad:>9}   {note}")
        add("")
        add("    'missed' and 'refused (correct)' are deliberately separate columns. A")
        add("    defect the engine declines is not a failure when ground truth also")
        add("    expects a refusal -- bank_charge is unmatchable by construction, and")
        add("    declining it IS the right answer. One recall figure would score the")
        add("    engine down for being right.")

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
    add("  LLM TIER")
    add(_THIN)
    add("    The trust boundary is enforced by the TYPE: NarrationFields carries no")
    add("    payment id, candidate or score, so a model cannot express a matching")
    add("    preference. parse_with_llm fills gaps only and never overrides")
    add("    deterministic output. Both properties are tested.")
    add("")
    add("    LLM-on vs LLM-off precision: UNMEASURED. No API key is available in this")
    add("    environment, and the offline stand-in applies the same word-filtering")
    add("    heuristic as the regex tier -- it agrees with it by construction and is")
    add("    not a valid proxy for a model. See DEFECT_LOG 2026-09-02-02.")
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
        # Wrapped, because the source string is a sentence and this report has a
        # fixed rule width; an overrun line reads as a formatting bug and invites the
        # reader to stop trusting the block.
        for i, line in enumerate(textwrap.wrap(
            f"weights: {state} -- {_weight_source()}", width=72
        )):
            add(f"    {line}" if i == 0 else f"      {line}")
        if not sc.confidence_calibrated:
            add("    These scores are an ORDERING, not probabilities. 0.9 does NOT yet mean")
            add("    'right 90% of the time'. BenchRec was fitted (2026-09-04) and the")
            add("    weights deliberately NOT substituted: it scores any candidate pair at")
            add("    a 0.202 base rate, this scores survivors of four layers at 0.992.")
        add(f"    {'bucket':>8} {'n':>5} {'observed accuracy':>19}")
        for mid, n, acc in sc.confidence_deciles:
            add(f"    {mid:>8.2f} {n:>5} {acc * 100:>18.1f}%")
        add("    calibration curve / ECE   0.0230 on BenchRec, 10/10 bins, n=40,001")
        add("                              -- external population, NOT this one")
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
