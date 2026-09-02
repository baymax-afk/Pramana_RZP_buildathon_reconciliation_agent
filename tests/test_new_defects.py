"""
The five ordinary defects the batch was missing, and what the engine does with each.

These are not exotic. A merchant sees every one of them monthly, and the batch was
unrealistically clean without them — most visibly in that every payment carried an
invoice number, which made tier 1 (exact reference) available far more often than
reality allows.

Each test pins the PROPERTY, not the count, because counts move with the seed. Where the
correct behaviour is a refusal, that is asserted as correct rather than tolerated.
"""

from __future__ import annotations

import pytest

import config as cfg
from recon.engine import fees as engine_fees
from recon.engine.match import match_once
from recon.engine.results import RefusalCategory
from recon.generator import build


def _labelled(batch, label):
    return [l for l in batch.truth if label in l.defect_labels and l.bank_txn_id]


# --------------------------------------------------------------------------
# advance_payment — money against no invoice at all
# --------------------------------------------------------------------------

def test_an_advance_payment_carries_no_invoice(batch):
    links = _labelled(batch, "advance_payment")
    assert links, "no advance payments in this batch"
    pay = {p.id: p for p in batch.inputs.payments}
    for l in links:
        assert l.invoice_nos == (), f"{l.bank_txn_id} is an advance but names an invoice"
        for pid in l.payment_ids:
            assert not pay[pid].notes.get("invoice_no")


def test_advance_payments_still_reconcile_on_amount_alone(batch):
    """
    No invoice means no reference to quote, so tier 1 cannot fire and the amount channel
    has to stand on its own. That path was completely untested before this defect
    existed — every payment used to carry an invoice.
    """
    out = match_once(batch.inputs)
    assigned = {a.bank_txn_id: a for a in out.assignments}
    links = _labelled(batch, "advance_payment")
    matched = [l for l in links if l.bank_txn_id in assigned]
    assert matched, "no advance payment reconciled; the invoice-less path is broken"
    for l in matched:
        assert assigned[l.bank_txn_id].tier != "tier1_reference", (
            "tier 1 matched a payment that has no invoice reference to match on"
        )


# --------------------------------------------------------------------------
# overpayment — the mirror of partial_payment
# --------------------------------------------------------------------------

def test_an_overpayment_exceeds_its_invoice_and_the_invoice_says_so(batch):
    inv = {i.invoice_no: i for i in batch.inputs.invoices}
    pay = {p.id: p for p in batch.inputs.payments}
    links = _labelled(batch, "overpayment")
    assert links, "no overpayments in this batch"
    for l in links:
        paid = sum(pay[pid].amount for pid in l.payment_ids)
        for no in l.invoice_nos:
            assert paid > inv[no].gross_amount
            assert inv[no].status == "over_settled"


def test_an_overpayment_still_agrees_with_its_credit(batch):
    """
    Same model as `partial_payment`: the PAYMENT is what differs from the invoice, so
    payment, fee and credit still agree exactly. If this fails, money is being hidden
    again — the defect shape this project has now shipped three times.
    """
    txn = {t.id: t for t in batch.inputs.bank_txns}
    pay = {p.id: p for p in batch.inputs.payments}
    inv = {i.invoice_no: i for i in batch.inputs.invoices}
    for l in _labelled(batch, "overpayment"):
        if l.expected_verdict != "assign":
            continue
        t = txn[l.bank_txn_id]
        interval = engine_fees.expected_credit_interval(
            [pay[pid] for pid in l.payment_ids], inv
        )
        tol = engine_fees.tolerance_for(t.credit)
        assert interval.lo - tol <= t.credit <= interval.hi + tol


# --------------------------------------------------------------------------
# bank_charge — unmatchable by construction, and refusing is the right answer
# --------------------------------------------------------------------------

def test_bank_charge_is_labelled_refuse_not_assign(batch):
    """
    Rs 5–50 against a Rs 1 tolerance cannot be reconciled by arithmetic. Labelling it
    `assign` would make it an automatic false negative — the exact defect shape of
    `refund_netted` and the old `partial_payment`.
    """
    links = _labelled(batch, "bank_charge")
    assert links, "no bank charges in this batch"
    assert all(l.expected_verdict == "refuse" for l in links)


def test_the_engine_refuses_bank_charges_rather_than_absorbing_them(batch):
    """
    THE point of this defect. An engine that widened tolerance to swallow bank charges
    would also start swallowing genuine coincidences, and the subset-sum uniqueness
    argument rests on tolerance staying far below the smallest payment.
    """
    out = match_once(batch.inputs)
    assigned = {a.bank_txn_id for a in out.assignments}
    for l in _labelled(batch, "bank_charge"):
        assert l.bank_txn_id not in assigned, (
            f"{l.bank_txn_id} carries an unrecorded bank charge and was posted anyway"
        )


def test_a_bank_charge_exceeds_tolerance_by_a_wide_margin(batch):
    """If it did not, the defect would be absorbed and would test nothing."""
    import random

    rng = random.Random(7)
    from recon.generator import defects

    for _ in range(50):
        assert defects.bank_charge_for(rng) >= 5 * cfg.TOL_ABS_PAISE


# --------------------------------------------------------------------------
# third_party_payer — the amount is right and the name is wrong
# --------------------------------------------------------------------------

def test_a_third_party_payer_is_never_POSTED_to_the_wrong_place(batch):
    """
    The engine may refuse these — the counterparty genuinely disagrees and a human
    should confirm the parent is authorised. What it must never do is post one to the
    wrong payment.
    """
    out = match_once(batch.inputs)
    truth = {l.bank_txn_id: l for l in batch.truth if l.bank_txn_id}
    for a in out.assignments:
        link = truth.get(a.bank_txn_id)
        if link and "third_party_payer" in link.defect_labels:
            assert set(a.payment_ids) == set(link.payment_ids)


def test_a_quoted_reference_outweighs_a_mismatched_payer_name(batch):
    """
    The evidence policy, measured. Name mismatch alone escalates; name mismatch plus a
    matching invoice reference is accepted. If Layer 3 ever vetoed on the name whatever
    the reference said, it would refuse every third-party payment and this would fail.
    """
    out = match_once(batch.inputs)
    assigned = {a.bank_txn_id for a in out.assignments}
    links = [l for l in _labelled(batch, "third_party_payer")
             if l.expected_verdict == "assign"]
    assert links, "no third-party payers in this batch"
    assert any(l.bank_txn_id in assigned for l in links), (
        "every third-party payment was refused -- Layer 3 is vetoing on the name "
        "regardless of the reference evidence"
    )


def test_every_third_party_refusal_names_the_conflict(batch):
    out = match_once(batch.inputs)
    truth = {l.bank_txn_id: l for l in batch.truth if l.bank_txn_id}
    for r in out.refusals:
        link = truth.get(r.bank_txn_id)
        if (
            link
            and "third_party_payer" in link.defect_labels
            and link.expected_verdict == "assign"
        ):
            assert r.category is RefusalCategory.AMOUNT_NAME_CONFLICT
            assert r.reason


# --------------------------------------------------------------------------
# weekend_bunching — the ordinary reason a lookback must be generous
# --------------------------------------------------------------------------

def test_weekend_bunched_credits_land_on_a_working_day(batch):
    from datetime import date

    links = _labelled(batch, "weekend_bunching")
    assert links, "no weekend bunching in this batch"
    txn = {t.id: t for t in batch.inputs.bank_txns}
    for l in links:
        assert date.fromisoformat(txn[l.bank_txn_id].txn_date).weekday() < 5


def test_weekend_bunching_never_pushes_a_payment_out_of_reach(batch):
    """
    A credit the engine provably cannot see is missing data, not a defect — the lesson
    of DEFECT_LOG 2026-09-02-08. The shift is capped at LOOKBACK_DAYS for that reason,
    and `assert_truth_is_satisfiable` would fail the build if it were not.
    """
    from recon.engine import tier2_amount_date as t2

    pay = {p.id: p for p in batch.inputs.payments}
    txn = {t.id: t for t in batch.inputs.bank_txns}
    for l in _labelled(batch, "weekend_bunching"):
        lo, hi = t2.window_for(txn[l.bank_txn_id])
        for pid in l.payment_ids:
            assert lo <= t2.payment_date(pay[pid]) <= hi


# --------------------------------------------------------------------------
# The whole batch, across seeds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [20260905, 77771, 44444])
def test_the_new_defects_appear_at_every_seed(seed):
    """A defect that only fires at one seed is not a category, it is an accident."""
    b = build.generate(seed=seed)
    seen = {lab for l in b.truth for lab in l.defect_labels}
    for label in (
        "advance_payment", "overpayment", "bank_charge",
        "third_party_payer", "weekend_bunching",
    ):
        assert label in seen, f"{label} did not occur at seed {seed}"


@pytest.mark.parametrize("seed", [20260905, 77771, 44444])
def test_precision_survives_the_richer_batch(seed):
    """
    The batch got materially harder. Coverage may fall; precision must not.
    """
    from scorer.score import score

    b = build.generate(seed=seed)
    out = match_once(b.inputs)
    sc = score(
        out, b.truth, total_payments=len(b.inputs.payments),
        captured_payments=sum(1 for p in b.inputs.payments if p.captured),
        ambiguity_bank_txn_id=b.ambiguity_bank_txn_id or "",
        credits_by_id={t.id: t.credit for t in b.inputs.bank_txns}, seed=seed,
    )
    assert sc.match_precision == 1.0, f"wrong assignments: {sc.wrong_assignments}"
