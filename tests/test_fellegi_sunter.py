"""
Layer 3: Fellegi-Sunter evidence weights and the two-threshold band.

Like the permutation gate, this layer currently never vetoes on the reported batch --
there are no genuine name/amount conflicts in it. So, like the permutation gate, it is
shown a conflict it MUST catch. A gate that has never fired is not known to work.

The tests here also pin down the two decisions that were wrong on the first attempt and
cost real coverage: scoring the blocking variable, and comparing names as whole strings.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import config as cfg
from recon.engine import fellegi_sunter as fs
from recon.engine.match import match_once
from recon.engine.normalize import parse
from recon.engine.results import RefusalCategory


@pytest.fixture(scope="module")
def u_est(batch):
    return fs.estimate_u(batch.inputs.payments, batch.inputs.bank_txns)


# --------------------------------------------------------------------------
# Independence of the evidence channels
# --------------------------------------------------------------------------
def test_amount_is_not_an_input_to_the_weight():
    """
    Conservation reasons over amounts; Fellegi-Sunter reasons over names and references.
    Feeding amount into both would make them correlated, and the entire argument for
    combining them is that they are independent channels which cannot fail the same way.
    """
    import ast
    import inspect

    src = inspect.getsource(fs)
    tree = ast.parse(src)
    called = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "residual" not in called
    assert "net_interval" not in called
    assert "expected_credit_interval" not in called


def test_date_is_not_in_the_comparison_vector(batch, u_est):
    """
    Date is the BLOCKING KEY -- the pool was built by requiring the payment to fall
    inside the credit's lookback, so every pair reaching the model already agrees on it.
    Scoring it again double-counts the blocking, and it also means no comparison is ever
    fully absent, so a settlement batch looks like weak evidence instead of none.
    """
    txn = next(t for t in batch.inputs.bank_txns if t.is_credit)
    ev = fs.evidence_for(txn, parse(txn.narration), list(batch.inputs.payments[:1]), u_est, 10)
    assert {f.field for f in ev.fields} == {"name", "reference"}


# --------------------------------------------------------------------------
# Absent evidence
# --------------------------------------------------------------------------
def test_settlement_batch_yields_no_evidence_not_a_penalty(batch, u_est):
    """
    A settlement batch carries no payer name because it covers many payers, and quotes
    no invoice because it covers many invoices. That is the correct content of those
    fields, not disagreement.

    Treating silence as dissent refused 86 of 137 credits on the first attempt --
    including every many-to-one decomposition Layer 2 had just earned.
    """
    txn = next(
        t for t in batch.inputs.bank_txns
        if t.is_credit and "RAZORPAY SETTLEMENT" in t.narration
    )
    ev = fs.evidence_for(txn, parse(txn.narration), list(batch.inputs.payments[:2]), u_est, 10)
    assert ev.band == "no_evidence"
    assert ev.weight is None
    assert not ev.contradicts


def test_no_evidence_is_distinct_from_zero_evidence():
    """
    Weight None means there was nothing to weigh; weight 0.0 means the evidence balanced
    out. They must not lead to the same decision.
    """
    absent = fs.Evidence(None, -3.0, (fs.FieldComparison("name", None, 0.0),))
    balanced = fs.Evidence(0.0, -3.0, (fs.FieldComparison("name", fs.Level.PARTIAL, 3.0),))
    assert absent.band == "no_evidence"
    assert balanced.band != "no_evidence"


# --------------------------------------------------------------------------
# Name comparison -- tokenised
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bank,ledger,expected",
    [
        # Bank field-width truncation, the common real case.
        ("PINNACLE STEEL TRA", "PINNACLE STEELS TRADERS", fs.Level.PARTIAL),
        ("NOVA CHEMICALS IND", "NOVA CHEMICAL INDIA", fs.Level.PARTIAL),
        ("ACME RETAIL", "ACME RETAIL", fs.Level.EXACT),
        # Abbreviation: degrades to partial rather than a false identity.
        ("VERTEX ENGINEERIN", "VERTEX ENGG", fs.Level.PARTIAL),
        # CONFUSABLE PAIRS -- different legal entities that must NOT merge.
        ("SUNRISE TEXTILES", "SUNLINE TEXTILES", fs.Level.PARTIAL),
        ("HARBOUR MARINE", "QUANTUM INSTRUMENTS", fs.Level.DISAGREE),
    ],
)
def test_name_agreement_levels(bank, ledger, expected):
    assert fs._name_agreement(bank, ledger) == expected


def test_tokens_agree_rejects_the_confusable_prefix_trap():
    """
    A 3-character shared prefix would merge SUNRISE with SUNLINE and ACME with ACMI.
    The registry deliberately contains confusable pairs that are DIFFERENT legal
    entities, and a comparison loose enough to merge them posts money to the wrong
    customer -- the failure this layer exists to price.
    """
    assert not fs._tokens_agree("SUNRISE", "SUNLINE")
    assert not fs._tokens_agree("ACME", "ACMI")
    # But genuine truncation and inflection must still agree.
    assert fs._tokens_agree("TRA", "TRADERS")
    assert fs._tokens_agree("STEEL", "STEELS")
    assert fs._tokens_agree("CHEMICAL", "CHEMICALS")


def test_whole_string_prefix_matching_would_have_been_wrong():
    """
    Regression guard for the bug that cost six correct assignments: neither
    'NOVA CHEMICALS IND' nor 'NOVA CHEMICAL INDIA' is a prefix of the other, so a
    string-level test calls the same counterparty a disagreement.
    """
    a, b = "NOVA CHEMICALS IND", "NOVA CHEMICAL INDIA"
    assert not a.startswith(b) and not b.startswith(a)
    assert fs._name_agreement(a, b) != fs.Level.DISAGREE


# --------------------------------------------------------------------------
# u estimation stays unsupervised
# --------------------------------------------------------------------------
def test_u_is_estimated_without_labels(batch, u_est):
    """
    Chance-agreement rates come from the observed value distribution -- no ground truth
    is consulted, so estimating them crosses no boundary.
    """
    assert 0.0 < u_est.name_exact < 1.0
    assert 0.0 < u_est.reference < 1.0
    # A reference is far more discriminating than a name: many payments share a
    # counterparty, almost none share an invoice number.
    assert u_est.reference < u_est.name_exact
    assert u_est.name_exact <= u_est.name_partial


def test_collision_probability_is_one_for_a_constant_field():
    """A field where every record shares a value carries no information."""
    assert fs._collision_probability(["X"] * 50) == pytest.approx(1.0)
    assert fs._collision_probability([f"v{i}" for i in range(100)]) < 0.05


# --------------------------------------------------------------------------
# THE negative control: the gate must be able to fire
# --------------------------------------------------------------------------
def test_the_fs_gate_actually_catches_an_amount_name_conflict(batch, u_est):
    """
    Construct the disagreement case from the architecture's own table: amounts reconcile
    exactly, but the payer name on the bank side belongs to a completely different
    counterparty.

    A matcher trusting amounts alone posts this to the wrong customer. Two independent
    channels disagree, so neither is trusted, and the engine must refuse.
    """
    txn = next(
        t for t in batch.inputs.bank_txns
        if t.is_credit and "NEFT-" in t.narration
    )
    payment = batch.inputs.payments[0]
    conflicted = replace(txn, narration="NEFT-AXISP11111111111-ZZZZ UNRELATED CO-CR")

    ev = fs.evidence_for(conflicted, parse(conflicted.narration), [payment], u_est, 10)
    assert ev.contradicts, "a flatly wrong payer name must contradict"
    assert ev.field_weight < 0
    assert ev.band == "non_match"


def test_the_gate_does_not_fire_on_mere_absence_or_weak_support(batch, u_est):
    """
    The complement of the test above. Absence never vetoes, and weak-but-positive
    evidence never vetoes -- only active disagreement does.
    """
    payment = batch.inputs.payments[0]
    settlement = next(
        t for t in batch.inputs.bank_txns
        if t.is_credit and "RAZORPAY SETTLEMENT" in t.narration
    )
    assert not fs.evidence_for(
        settlement, parse(settlement.narration), [payment], u_est, 10
    ).contradicts


def test_a_conflicting_name_produces_a_refusal_end_to_end(batch):
    """
    The gate wired into the matcher, not just the model in isolation: rewriting one
    credit's narration to name an unrelated counterparty must turn an assignment into
    an `amount_name_conflict` refusal, with the amounts left untouched.
    """
    from recon.schemas import ReconInputs

    baseline = match_once(batch.inputs)
    target = next(
        a for a in baseline.assignments
        if a.tier == "tier2_amount_date" and len(a.payment_ids) == 1
    )
    txns = tuple(
        replace(t, narration="NEFT-AXISP11111111111-ZZZZ UNRELATED CO-CR")
        if t.id == target.bank_txn_id else t
        for t in batch.inputs.bank_txns
    )
    mutated = ReconInputs(
        batch.inputs.payments, txns, batch.inputs.invoices,
        batch.inputs.seed, batch.inputs.payments_per_window,
    )
    out = match_once(mutated)
    refusal = next((r for r in out.refusals if r.bank_txn_id == target.bank_txn_id), None)
    assert refusal is not None, "a flatly wrong payer name did not produce a refusal"
    assert refusal.category is RefusalCategory.AMOUNT_NAME_CONFLICT
    assert "contradicts" in refusal.reason


# --------------------------------------------------------------------------
# Thresholds and the model itself
# --------------------------------------------------------------------------
def test_thresholds_are_the_published_splink_correspondences():
    """
    Weight 4 is about 95% and weight 7 about 99% under 2^M/(1+2^M). These came from the
    Splink theory guide, not from tuning against results.
    """
    assert cfg.FS_THRESHOLD_LOWER == 4.0
    assert cfg.FS_THRESHOLD_UPPER == 7.0
    assert fs.Evidence(4.0, 0.0, ()).probability == pytest.approx(0.941, abs=0.01)
    assert fs.Evidence(7.0, 0.0, ()).probability == pytest.approx(0.992, abs=0.005)


def test_disagreement_counts_against_the_match():
    """
    What makes this a likelihood RATIO rather than a score: a field that disagrees
    actively subtracts, in proportion to how surprising that would be if the records
    truly matched.
    """
    agree = fs._field_weight(fs.Level.EXACT, 0.9, 0.05)
    disagree = fs._field_weight(fs.Level.DISAGREE, 0.9, 0.05)
    assert agree > 0 > disagree


def test_layer_3_costs_no_correct_assignments_on_the_reported_batch(batch):
    """
    Layer 3 must add evidence without costing coverage. It vetoed six correct
    assignments on the first attempt -- all of them the same tokenisation bug -- so this
    pins the outcome rather than the mechanism.
    """
    out = match_once(batch.inputs)
    truth = {t.bank_txn_id: t for t in batch.truth if t.bank_txn_id}
    conflicts = [
        r for r in out.refusals
        if r.category is RefusalCategory.AMOUNT_NAME_CONFLICT
    ]
    wrongly_refused = [
        r for r in conflicts
        if (l := truth.get(r.bank_txn_id)) and l.expected_verdict == "assign"
    ]
    assert not wrongly_refused, (
        f"Layer 3 refused {len(wrongly_refused)} assignments ground truth wanted: "
        f"{[r.bank_txn_id for r in wrongly_refused]}"
    )
