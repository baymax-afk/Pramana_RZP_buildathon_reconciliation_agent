"""
Layer 1: the permutation gate and the six metamorphic relations.

The most important test in this file is `test_the_gate_actually_catches_order_dependence`.

On the current engine the gate reports stability 1.0 everywhere and never fires, which
is the *correct* result: `match_once` sorts credits into a total data-derived order, and
both tiers refuse rather than choose when several candidates fit, so the matcher is
order-independent by construction. But "the detector found nothing" and "the detector
does not work" produce identical output, and the difference matters enormously. So the
gate is deliberately shown a matcher that IS order-dependent, and must catch it.

A check that has never fired is not known to work. This one has.
"""

from __future__ import annotations

import random

import pytest

import config as cfg
from recon.engine.match import match_once
from recon.engine.results import Candidate, RefusalCategory
from recon.schemas import BankTxn, Invoice, Payment, ReconInputs
from recon.verify import metamorphic as mm
from recon.verify.stability import (
    Ensemble,
    TxnStability,
    apply_gate,
    match_gated,
    run_with_permutations,
)


# --------------------------------------------------------------------------
# The negative control: prove the gate can fire
# --------------------------------------------------------------------------
def _tied_batch() -> ReconInputs:
    """
    A batch containing a genuine tie: two payments with IDENTICAL settled amounts, both
    inside one credit's window, and one credit that either could explain.

    No amount, date or reference signal distinguishes them. A matcher that picks rather
    than refuses must pick by iteration order -- which is exactly what the gate exists
    to detect.
    """
    # Derive the timestamp from the date rather than hardcoding a magic number: the
    # payment must land INSIDE the credit's lookback or the tie is never reached and
    # the fixture silently tests nothing.
    import calendar
    from datetime import date as _date

    ts = calendar.timegm(_date(2026, 6, 2).timetuple()) + 12 * 3600
    pays = tuple(
        Payment(
            id=f"pay_TIE{i}", amount=100_000, currency="INR", status="captured",
            captured=True, method="netbanking", order_id=f"order_TIE{i}",
            created_at=ts, description="#TIE", contact="+910000000001",
            email="tie@example.com", provenance="S", fee=2_596, tax=396,
            notes={"customer_name": "Tie Co", "invoice_no": f"INV-2026-90{i}"},
        )
        for i in (1, 2)
    )
    net = 100_000 - 2_596
    txn = BankTxn(
        id="bank_txn_0001", txn_date="2026-06-04", value_date="2026-06-04",
        narration="RAZORPAY SETTLEMENT setl_TIEtest0001 1 TXNS",
        ref_no="AXISP00000000001", credit=net, debit=0, balance=net,
    )
    invs = tuple(
        Invoice(
            invoice_no=f"INV-2026-90{i}", customer_name="Tie Co",
            customer_gstin="29AAAAA0000A1Z5", invoice_date="2026-06-01",
            due_date="2026-07-01", gross_amount=100_000, tds_amount=0,
            currency="INR", status="open", po_reference="PO-TIE-1",
        )
        for i in (1, 2)
    )
    return ReconInputs(pays, (txn,), invs, seed=cfg.SEED_PRIMARY, payments_per_window=2)


def _greedy_first_in_iteration_order(txn, payments, claimed, invoices_by_no):
    """
    The mutant matcher: take the FIRST candidate encountered while walking the pool,
    instead of refusing when several fit.

    It must pick in genuine iteration order, not from tier 2's sorted candidate list.
    The real tier 2 sorts its candidates before returning them, so a mutant that picked
    `candidates[0]` would still be deterministic under shuffling and would prove
    nothing -- the first attempt at this test made exactly that mistake and reported a
    perfectly stable engine.

    This is the single most natural way to write a matcher, and quietly wrong: with two
    equally good candidates it silently posts money to whichever the loop reached first.
    """
    from recon.engine import fees as _fees
    from recon.engine.tier2_amount_date import TIER, candidate_pool

    tol = _fees.tolerance_for(txn.credit)
    for p in candidate_pool(txn, payments, claimed):
        interval = _fees.expected_credit_interval([p], invoices_by_no)
        resid = _fees.residual(txn.credit, interval)
        if abs(resid) <= tol:
            return (
                [Candidate((p.id,), resid, TIER, interval.lo, interval.hi, interval.certain)],
                None,
                "",
            )
    return [], None, ""


def test_the_real_engine_refuses_a_tie_rather_than_picking():
    """
    The first line of defence: tier 2 sees two equally good candidates and declines.
    This is why the gate has nothing to catch on the real engine.
    """
    out = match_once(_tied_batch())
    assert not out.assignments
    assert len(out.refusals) == 1
    assert out.refusals[0].category is RefusalCategory.MULTIPLE_CANDIDATES
    assert len(out.refusals[0].candidates) == 2


def test_the_gate_actually_catches_order_dependence(monkeypatch):
    """
    THE test that makes the gate credible.

    Replace tier 2's refuse-on-tie with pick-the-first -- the single most natural way
    to write a matcher, and a subtly wrong one. On the tied batch that mutant assigns
    whichever payment iteration reaches first, so shuffling the input changes the
    answer.

    The gate must notice, strip the assignment, and replace it with an
    `order_dependent_assignment` refusal naming both variants it saw.

    **Forced onto the sequential path, and that is not a dodge.** A monkeypatch lives in
    THIS process. `ProcessPoolExecutor` uses fork on Linux, where the workers inherit it,
    and spawn on Windows and macOS, where they re-import the module and the mutant
    silently disappears -- so every pass runs the correct matcher, the ensemble sees no
    instability, and the test fails for a reason that has nothing to do with the gate.

    That is what it did the first time this suite ran on Windows. The deeper problem is
    that the test's coverage was platform-dependent WITHOUT SAYING SO: it exercised the
    gate on Linux and, had the assertion been any weaker, would have passed vacuously
    elsewhere. Pinning the path makes the detection logic testable everywhere, and
    `test_the_parallel_and_sequential_paths_agree` separately proves the parallel path is
    interchangeable with the one tested here.
    """
    from recon.engine import tier2_amount_date as t2

    monkeypatch.setattr(cfg, "PERMUTATION_PARALLEL", False)
    monkeypatch.setattr(t2, "match", _greedy_first_in_iteration_order)

    inputs = _tied_batch()
    gated, ens = match_gated(inputs, k=8)

    assert ens.summary()["unstable"] == 1, "the ensemble failed to see the instability"
    assert not gated.assignments, "an order-dependent assignment survived the gate"
    assert len(gated.refusals) == 1
    r = gated.refusals[0]
    assert r.category is RefusalCategory.ORDER_DEPENDENT
    assert "iteration order" in r.reason
    assert "pay_TIE1" in r.reason and "pay_TIE2" in r.reason
    assert r.paise_at_risk > 0


def test_gate_is_hard_not_a_confidence_discount(monkeypatch):
    """
    An order-dependent assignment is not a low-confidence assignment -- it is evidence
    the data did not determine the answer. Softening it into a score would let it be
    auto-posted anyway, slightly discounted.
    """
    from recon.engine import tier2_amount_date as t2

    monkeypatch.setattr(t2, "match", _greedy_first_in_iteration_order)
    gated, _ = match_gated(_tied_batch(), k=8)
    assert gated.assignment_map == {}


# --------------------------------------------------------------------------
# The ensemble itself
# --------------------------------------------------------------------------
def test_ensemble_is_reproducible(batch):
    a = run_with_permutations(batch.inputs, k=4)
    b = run_with_permutations(batch.inputs, k=4)
    assert {k: v.observed for k, v in a.per_txn.items()} == {
        k: v.observed for k, v in b.per_txn.items()
    }


def test_absence_counts_against_stability_not_for_it():
    """
    A credit assigned in half the passes and dropped in the others is UNSTABLE. Scoring
    it on "passes where it was assigned" would call that perfectly stable and hide the
    order-dependence entirely.
    """
    s = TxnStability("t", ((frozenset({"p1"}), 4),), passes=8)
    assert s.stability == 0.5
    assert not s.is_stable


def test_real_engine_is_stable_under_permutation(batch):
    """The measured claim, not an assumed one."""
    ens = run_with_permutations(batch.inputs, k=cfg.PERMUTATION_K)
    assert ens.unstable() == ()
    assert ens.summary()["min_stability"] == 1.0


def test_gated_output_matches_single_pass_when_nothing_is_unstable(batch):
    gated, ens = match_gated(batch.inputs)
    assert not ens.unstable()
    assert gated.assignment_map == match_once(batch.inputs).assignment_map


# --------------------------------------------------------------------------
# The six relations
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", mm.ALL_RELATIONS)
def test_each_relation_holds_on_the_reported_batch(batch, name):
    results = {r.name: r for r in mm.run_all(batch.inputs)}
    r = results[name]
    assert r.passed, f"{name} violated: {[v.detail for v in r.violations][:3]}"


def test_relations_report_their_kind_honestly(batch):
    """
    MR4 and MR5 are single-run invariants, not metamorphic relations. Reporting six
    metamorphic relations would overstate the result -- an invariant checks one
    execution against arithmetic; a metamorphic relation checks one execution against
    another.
    """
    kinds = {r.name: r.kind for r in mm.run_all(batch.inputs)}
    assert kinds["MR1"] == kinds["MR2"] == kinds["MR3"] == kinds["MR6"] == "metamorphic"
    assert kinds["MR4"] == kinds["MR5"] == "invariant"


def test_mr3_intruder_is_constructively_unmatchable(batch):
    """
    The relation is worthless if the added record turns out to be matchable. It must be
    guaranteed on amount, date AND identity -- not merely unlikely to match.
    """
    intruder = mm._unmatchable_payment(batch.inputs)
    assert intruder.amount > max(t.credit for t in batch.inputs.bank_txns)
    from recon.schemas import date_of

    latest_credit = max(t.txn_date for t in batch.inputs.bank_txns)
    assert date_of(intruder.created_at).isoformat() > latest_credit
    assert intruder.notes["invoice_no"] not in {
        i.invoice_no for i in batch.inputs.invoices
    }


def test_mr5_catches_a_double_posted_payment(batch):
    """
    Negative control for MR5: assigning one payment to two credits must be reported.
    Double-posting inflates the match rate while moving money twice, so an invariant
    that cannot see it is not worth running.
    """
    from dataclasses import replace

    out = match_once(batch.inputs)
    first = out.assignments[0]
    duplicate = replace(out.assignments[1], payment_ids=first.payment_ids)
    corrupted = replace(
        out, assignments=(first, duplicate) + tuple(out.assignments[2:])
    )
    result = mm.mr5_residual_closure(batch.inputs, corrupted)
    assert not result.passed
    assert any("double-posted" in v.detail for v in result.violations)


def test_mr4_catches_a_tampered_residual(batch):
    """
    Negative control for MR4: a stored residual that disagrees with the raw records
    must be caught. This is what the relation genuinely detects -- bookkeeping
    corruption, not matcher logic.
    """
    from dataclasses import replace

    out = match_once(batch.inputs)
    tampered = replace(
        out,
        assignments=(replace(out.assignments[0], residual_paise=99_999),)
        + tuple(out.assignments[1:]),
    )
    result = mm.mr4_conservation(batch.inputs, tampered)
    assert not result.passed


def test_mr6_catches_an_engine_that_left_work_undone(batch):
    """
    Negative control for MR6: if the first pass declines something a rerun would take,
    output depends on how many times the engine happens to be run.
    """
    from dataclasses import replace

    out = match_once(batch.inputs)
    pretend_missed = replace(out, assignments=out.assignments[:-3])
    result = mm.mr6_idempotence(batch.inputs, pretend_missed)
    assert not result.passed
    assert len(result.violations) >= 1


def test_parallel_and_sequential_ensembles_are_identical(batch, monkeypatch):
    """
    The ensemble runs its K passes in worker processes. That is a speed decision, and it
    must stay only a speed decision: `match_once` is pure, results are collected by pass
    index rather than by completion order, and nothing observes which worker finished
    first. If those stop being true the gate's verdicts start depending on scheduling,
    which is the exact failure mode the gate exists to catch.
    """
    import config as cfg
    from recon.verify import stability

    parallel = stability.run_with_permutations(batch.inputs, k=4, seed=99)
    monkeypatch.setattr(cfg, "PERMUTATION_PARALLEL", False)
    sequential = stability.run_with_permutations(batch.inputs, k=4, seed=99)

    assert parallel.base.assignment_map == sequential.base.assignment_map
    assert parallel.summary() == sequential.summary()
    assert {t: s.stability for t, s in parallel.per_txn.items()} == {
        t: s.stability for t, s in sequential.per_txn.items()
    }


def test_ensemble_falls_back_to_sequential_when_the_llm_tier_cannot_be_pickled(batch):
    """
    A live LLM tier holds an open HTTP client and cannot cross a process boundary. That
    must degrade to the sequential path, not crash the run -- the ensemble is the
    engine's primary execution path, not an optional extra.
    """
    from recon.verify import stability

    class Unpicklable:
        name, enabled = "unpicklable", True

        def __reduce__(self):
            raise TypeError("cannot pickle this tier")

        def parse_narration(self, narration):
            from recon.llm.interface import NarrationFields
            return NarrationFields()

        def explain(self, category, reason, rupees_at_risk):
            from recon.llm.interface import ExceptionProse
            return ExceptionProse(reason, "Review manually.")

    assert stability._is_picklable(Unpicklable()) is False
    ens = stability.run_with_permutations(batch.inputs, k=2, seed=7, llm=Unpicklable())
    assert ens.passes == 2
    assert ens.base is not None


# --------------------------------------------------------------------------
# The two-density headline
# --------------------------------------------------------------------------

def test_headline_reports_both_densities_when_a_comparison_is_supplied(batch):
    """
    A single density in the headline invites reading the numbers as a property of the
    ENGINE, when they are a property of the engine at one crowding level. Density is the
    parameter the whole argument turns on.
    """
    import config as cfg
    from recon.engine.match import match_once
    from recon.generator import build
    from scorer.report import render
    from scorer.score import score

    def scored(b, seed):
        o = match_once(b.inputs)
        return score(
            o, b.truth, total_payments=len(b.inputs.payments),
            captured_payments=sum(1 for p in b.inputs.payments if p.captured),
            ambiguity_bank_txn_id=b.ambiguity_bank_txn_id or "",
            credits_by_id={t.id: t.credit for t in b.inputs.bank_txns}, seed=seed,
        )

    primary = scored(batch, cfg.SEED_PRIMARY)
    other_ppw = 12
    other = scored(
        build.generate(seed=cfg.SEED_PRIMARY, payments_per_window=other_ppw),
        cfg.SEED_PRIMARY,
    )

    text = render(
        primary, cfg.SEED_PRIMARY, cfg.TARGET_POOL_SIZE,
        llm_enabled=False, compare=(other, other_ppw),
    )
    assert f"density={cfg.TARGET_POOL_SIZE} vs {other_ppw}" in text
    assert f"ppw={cfg.TARGET_POOL_SIZE}" in text and f"ppw={other_ppw}" in text
    assert "delta" in text
    # The detail sections below the headline must still describe the PRIMARY arm only.
    assert f"Everything below this block is the ppw={cfg.TARGET_POOL_SIZE} run" in text


def test_headline_without_a_comparison_is_unchanged(batch):
    """The second arm is opt-in; the single-density block must not change shape."""
    import config as cfg
    from recon.engine.match import match_once
    from scorer.report import render
    from scorer.score import score

    o = match_once(batch.inputs)
    sc = score(
        o, batch.truth, total_payments=len(batch.inputs.payments),
        captured_payments=sum(1 for p in batch.inputs.payments if p.captured),
        ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id or "",
        credits_by_id={t.id: t.credit for t in batch.inputs.bank_txns},
        seed=cfg.SEED_PRIMARY,
    )
    text = render(sc, cfg.SEED_PRIMARY, cfg.TARGET_POOL_SIZE, llm_enabled=False)
    assert " vs " not in text.splitlines()[1]
    assert "match rate            " in text
    assert "Everything below this block" not in text


def test_the_parallel_and_sequential_paths_agree(monkeypatch):
    """
    The parallel path is an optimisation, so it must be indistinguishable from the
    sequential one -- not merely close.

    This is the other half of `test_the_gate_actually_catches_order_dependence`, which
    pins itself to the sequential path so a monkeypatched mutant survives on every
    platform. That pinning is only honest if the path it skips is proven equivalent
    here, on the real matcher, where no monkeypatch is involved and the process start
    method therefore does not matter.

    Results are collected by pass INDEX rather than completion order, so equality is a
    property of the design and not of the scheduler.
    """
    from loaders import load_inputs

    inputs = load_inputs()

    monkeypatch.setattr(cfg, "PERMUTATION_PARALLEL", False)
    seq_out, seq_ens = match_gated(inputs, k=4)

    monkeypatch.setattr(cfg, "PERMUTATION_PARALLEL", True)
    par_out, par_ens = match_gated(inputs, k=4)

    assert seq_ens.summary() == par_ens.summary(), (
        "the two paths disagree about stability"
    )
    assert seq_out.assignment_map == par_out.assignment_map, (
        "the two paths produced different assignments"
    )
    assert (
        sorted((r.bank_txn_id, r.category) for r in seq_out.refusals)
        == sorted((r.bank_txn_id, r.category) for r in par_out.refusals)
    ), "the two paths produced different refusals"


def test_the_parallel_path_is_actually_taken_when_it_can_be():
    """
    An optimisation that silently never runs is worse than none: it costs the
    complexity and delivers nothing, and every "the paths agree" assertion above it
    passes trivially because only one path ever executes.
    """
    from recon.llm.null import NullTier
    from recon.verify import stability

    assert cfg.PERMUTATION_PARALLEL, "the parallel path is disabled in config"
    assert stability._is_picklable(NullTier()), (
        "the default LLM tier is unpicklable, so the parallel path can never be taken"
    )
