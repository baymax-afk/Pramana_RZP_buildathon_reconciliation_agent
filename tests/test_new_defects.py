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


def test_a_split_settlement_now_expects_an_assignment(batch):
    """
    This test used to assert the OPPOSITE, and the change of mind is the point.

    Its previous name was `..._is_labelled_refuse_because_the_model_cannot_hold_it`, and
    the reasoning was sound for as long as it held: `claimed` is a set, a payment is
    taken exactly once, every tier asks which SUBSET OF PAYMENTS sums to a credit, and
    there is nowhere to put half a payment. Refusing was not a shortcoming, it was the
    only correct verdict available -- posting a part-settlement against a whole payment
    is a wrong answer, not a partial one.

    Layer 2b changed what is available. The claim unit is now a GROUP of credits, the
    two halves balance against the payment exactly, and `expected_verdict="refuse"`
    would now score the engine DOWN for doing the work correctly.
    """
    links = _labelled(batch, "split_settlement")
    assert all(l.expected_verdict == "assign" for l in links)
    assert all(l.relation == "split" for l in links)


def test_the_engine_never_posts_half_a_payment(batch):
    """
    Still true, and it is the constraint the group model had to respect rather than
    escape. A split settlement is settled as a GROUP -- both credits together, against
    the whole payment -- and never as one credit against a payment it only half covers.
    """
    out = match_once(batch.inputs)
    singly_assigned = {a.bank_txn_id for a in out.assignments}
    for l in _labelled(batch, "split_settlement"):
        assert l.bank_txn_id not in singly_assigned, (
            f"{l.bank_txn_id} is one half of a split settlement and was posted against "
            f"the whole payment"
        )


def test_a_split_settlement_is_settled_as_one_group(batch):
    out = match_once(batch.inputs)
    links = _labelled(batch, "split_settlement")
    assert links
    by_payment: dict[tuple, set] = {}
    for l in links:
        by_payment.setdefault(l.payment_ids, set()).add(l.bank_txn_id)

    for payment_ids, txn_ids in by_payment.items():
        if len(txn_ids) < 2:
            continue
        match = [g for g in out.groups if set(g.bank_txn_ids) == txn_ids]
        assert match, (
            f"the split settlement {sorted(txn_ids)} was not resolved as a group; "
            f"groups found: {[g.bank_txn_ids for g in out.groups]}"
        )
        assert set(match[0].payment_ids) == set(payment_ids)


def test_a_settlement_group_conserves_money_over_the_whole_group(batch):
    """
    The invariant was RESTATED at group level, not relaxed. Each member credit is only
    part of the payment, so per-credit conservation cannot hold and must not be asked
    to; the group's total must close exactly.
    """
    from recon.engine import fees

    out = match_once(batch.inputs)
    assert out.groups, "no settlement groups in this batch"
    by_id = {p.id: p for p in batch.inputs.payments}
    credit_of = {t.id: t.credit for t in batch.inputs.bank_txns}
    inv = {i.invoice_no: i for i in batch.inputs.invoices}

    for g in out.groups:
        assert len(g.bank_txn_ids) >= 2, "a group of one is not a group"
        total = sum(credit_of[t] for t in g.bank_txn_ids)
        assert total == g.credit_paise
        interval = fees.expected_credit_interval(
            [by_id[pid] for pid in g.payment_ids], inv
        )
        assert abs(fees.residual(total, interval)) <= fees.tolerance_for(total)


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


def test_a_chargeback_now_carries_a_truth_link_and_the_engine_reverses_it(batch):
    """
    This one also used to assert the opposite, and for a reason that was conditional.

    It read: *"the engine structurally cannot produce a verdict for a debit, so scoring
    it against one would be theatre -- a permanent miss no amount of engine work could
    ever close."* Correct, while it was true. Layer 2c reads debits and ties each to the
    settlement it reverses, so `reverse` is a verdict the engine CAN produce, and
    withholding the label would now hide real work rather than avoid a fake miss.

    The link names the reversed PAYMENT, so a reversal posted against the wrong
    settlement is scored as an error instead of passing unexamined.
    """
    debit_ids = {t.id for t in batch.inputs.bank_txns if t.debit}
    reversal_links = {
        l.bank_txn_id for l in batch.truth if l.expected_verdict == "reverse"
    }
    assert reversal_links, "no reversal links in truth"
    assert reversal_links <= debit_ids, "a reversal link names something that is not a debit"
    assert all(l.relation == "reversal" for l in batch.truth
               if l.expected_verdict == "reverse")

    out = match_once(batch.inputs)
    found = {r.bank_txn_id: set(r.payment_ids) for r in out.reversals}
    truth = {l.bank_txn_id: set(l.payment_ids) for l in batch.truth
             if l.expected_verdict == "reverse"}
    assert found == truth, (
        f"the reversal ledger disagrees with truth: engine {found}, truth {truth}"
    )


def test_a_reversal_does_not_undo_the_assignment_it_reverses(batch):
    """
    Both events happened. Erasing the settlement would leave the books describing a
    batch that never occurred, and would break MR5's accounting besides -- the credit
    would lose its verdict and the payment its claim.
    """
    out = match_once(batch.inputs)
    assert out.reversals
    posted = {a.bank_txn_id for a in out.assignments} | set(out.grouped_txn_ids)
    for r in out.reversals:
        assert r.settled_by in posted, (
            "a reversal names a settlement the engine did not post"
        )


def test_a_chargeback_link_carries_only_its_own_defect_label(batch):
    """
    A defect label says what makes THIS line hard. The claw-back inherits none of what
    made the original credit hard: different date, different narration, different
    question. Inheriting them labelled a debit dated the following Sunday
    `weekend_bunching`, and the generator's own well-formedness checks then tested a
    chargeback against the rules for a settlement credit and failed it.
    """
    for l in batch.truth:
        if l.expected_verdict == "reverse":
            assert l.defect_labels == ("chargeback_debit",), l.defect_labels


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
    # The disclosure is still printed -- at zero, which is the point. It used to name
    # every debit as unexamined; the engine now reaches all of them, and a disclosure
    # that disappears the moment it reads zero is one nobody can check.
    assert "NOT EXAMINED" in text
    assert "REVERSAL LEDGER" in text
    assert f"{len(debits)}/{len(debits)}" in text, (
        "the reversal ledger should account for every chargeback in the batch"
    )


@pytest.mark.parametrize("seed", [20260905, 77771, 44444])
def test_the_structural_defects_appear_at_every_seed(seed):
    b = build.generate(seed=seed)
    seen = {lab for l in b.truth for lab in l.defect_labels}
    assert "split_settlement" in seen, f"no split settlement at seed {seed}"
    assert any(t.debit for t in b.inputs.bank_txns), f"no debit lines at seed {seed}"
