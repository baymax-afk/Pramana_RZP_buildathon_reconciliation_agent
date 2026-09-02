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


# --------------------------------------------------------------------------
# split_settlement — a relation the engine's model cannot express
# --------------------------------------------------------------------------

def test_a_split_settlement_is_two_credits_for_one_payment(batch):
    links = _labelled(batch, "split_settlement")
    assert links, "no split settlements in this batch"
    by_payment = {}
    for l in links:
        by_payment.setdefault(l.payment_ids, []).append(l.bank_txn_id)
    assert any(len(v) == 2 for v in by_payment.values()), (
        "no payment is settled across two credits; the defect did not fire"
    )


def test_a_split_settlement_is_labelled_refuse_because_the_model_cannot_hold_it(batch):
    """
    `claimed` is a set, so a payment is taken once, and every tier asks which SUBSET OF
    PAYMENTS sums to a credit. There is nowhere to put half a payment.

    Labelling it `assign` would make it an automatic false negative -- the shape this
    project has shipped three times. Labelling it `refuse` is honest, because refusing
    IS correct: posting a part-settlement against a whole payment is a wrong answer, not
    a partial one. The coverage cost is real and is reported in the outcome-by-defect
    table rather than hidden behind a correct-looking refusal.
    """
    links = _labelled(batch, "split_settlement")
    assert all(l.expected_verdict == "refuse" for l in links)
    assert all(l.relation == "split" for l in links)


def test_the_engine_never_posts_half_a_payment(batch):
    out = match_once(batch.inputs)
    assigned = {a.bank_txn_id for a in out.assignments}
    for l in _labelled(batch, "split_settlement"):
        assert l.bank_txn_id not in assigned, (
            f"{l.bank_txn_id} is one half of a split settlement and was posted against "
            f"the whole payment"
        )


# --------------------------------------------------------------------------
# chargeback_debit — money leaving, on a line the engine never reads
# --------------------------------------------------------------------------

def test_the_statement_now_contains_debits(batch):
    """
    It contained ZERO before this defect existed, which is why the blind spot went
    unnoticed: the engine had never been shown the half of a bank statement it ignores
    by construction.
    """
    debits = [t for t in batch.inputs.bank_txns if t.debit]
    assert debits, "the statement has no debit lines at all"
    assert all(t.credit == 0 for t in debits)
    assert all(not t.is_credit for t in debits)


def test_a_chargeback_carries_the_reference_of_the_credit_it_reverses(batch):
    debits = [t for t in batch.inputs.bank_txns if t.debit and "CHARGEBACK" in t.narration]
    assert debits, "no chargebacks in this batch"
    credit_refs = {t.ref_no for t in batch.inputs.bank_txns if t.is_credit}
    for d in debits:
        assert d.ref_no in credit_refs, (
            "a chargeback must reference the credit it reverses, or it is unattributable"
        )


def test_no_truth_link_is_invented_for_a_debit(batch):
    """
    The engine structurally cannot produce a verdict for a debit, so scoring it against
    one would be theatre -- it would show up as a permanent miss that no amount of
    engine work could ever close. The metrics block DISCLOSES the unexamined lines
    instead, which is the honest form of the same information.
    """
    debit_ids = {t.id for t in batch.inputs.bank_txns if t.debit}
    linked = {l.bank_txn_id for l in batch.truth if l.bank_txn_id}
    assert not (debit_ids & linked), (
        f"ground truth invents a verdict for debit lines: {debit_ids & linked}"
    )


def test_the_metrics_block_discloses_what_it_did_not_examine(batch):
    from scorer.report import render
    from scorer.score import score

    out = match_once(batch.inputs)
    debits = [t for t in batch.inputs.bank_txns if t.debit]
    sc = score(
        out, batch.truth, total_payments=len(batch.inputs.payments),
        captured_payments=sum(1 for p in batch.inputs.payments if p.captured),
        ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id or "",
        credits_by_id={t.id: t.credit for t in batch.inputs.bank_txns},
        seed=cfg.SEED_PRIMARY,
        unexamined=(len(debits), sum(t.debit for t in debits)),
    )
    text = render(sc, cfg.SEED_PRIMARY, cfg.TARGET_POOL_SIZE, llm_enabled=False)
    assert "NOT EXAMINED" in text
    assert f"{len(debits)} debit line" in text


@pytest.mark.parametrize("seed", [20260905, 77771, 44444])
def test_the_structural_defects_appear_at_every_seed(seed):
    b = build.generate(seed=seed)
    seen = {lab for l in b.truth for lab in l.defect_labels}
    assert "split_settlement" in seen, f"no split settlement at seed {seed}"
    assert any(t.debit for t in b.inputs.bank_txns), f"no debit lines at seed {seed}"
