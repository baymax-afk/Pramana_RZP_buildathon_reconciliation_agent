"""
Guards on the hand-placed ambiguity case.

This case is the centrepiece of the demo: a bank credit that two different payment
subsets both satisfy within tolerance, which the engine must REFUSE rather than
resolve. It is worth guarding heavily, because it can fail in two opposite and equally
damaging ways:

- If a later change makes it RESOLVABLE, the demo becomes a lie -- the engine would
  confidently assign one of two equally-supported answers.
- If a later change makes it MORE ambiguous (a third candidate), it is a different
  puzzle than the one documented, and the "exactly two candidates" claim is false.

The engine-side verdict test lands with the matching engine (Block 6). These are the
generator-side structural guarantees, which must hold before any engine exists.
"""

from __future__ import annotations

from itertools import combinations

import pytest

import config as cfg
from recon.generator import build, fees


def test_ambiguity_case_has_exactly_two_candidates(batch):
    """Brute-force every subset of the window; exactly two must fit."""
    assert build.assert_ambiguity_is_exact(batch) == cfg.AMBIGUITY_EXPECTED_CANDIDATES


def test_ambiguity_holds_at_every_sweep_density():
    """
    The structural guarantee must survive the density sweep.

    Crowding the settlement windows is exactly the condition that could accidentally
    introduce a third candidate, so the sweep densities are where this is most likely
    to break.
    """
    for ppw in cfg.DENSITY_SWEEP:
        b = build.generate(seed=cfg.SEED_PRIMARY, payments_per_window=ppw)
        assert build.assert_ambiguity_is_exact(b) == cfg.AMBIGUITY_EXPECTED_CANDIDATES, (
            f"ambiguity case broke at payments_per_window={ppw}"
        )


def test_ambiguity_holds_at_second_seed(batch_second_seed):
    """It must be structural, not an artefact of one seed."""
    assert (
        build.assert_ambiguity_is_exact(batch_second_seed)
        == cfg.AMBIGUITY_EXPECTED_CANDIDATES
    )


def test_ambiguity_is_labelled_refuse(batch):
    """
    Ground truth must expect a REFUSAL.

    Without this the scorer would penalise the engine for behaving correctly, and the
    metric would quietly reward guessing.
    """
    link = next(
        t for t in batch.truth if t.bank_txn_id == batch.ambiguity_bank_txn_id
    )
    assert link.expected_verdict == "refuse"
    assert len(link.payment_ids) == 4


def test_the_four_net_amounts_collide_exactly(batch):
    """
    Two disjoint pairs must sum to the credit EXACTLY, not within tolerance.

    An approximate collision could be separated by a sufficiently tight matcher, which
    would make the case resolvable rather than genuinely ambiguous.
    """
    link = next(
        t for t in batch.truth if t.bank_txn_id == batch.ambiguity_bank_txn_id
    )
    by_id = {p.id: p for p in batch.inputs.payments}
    nets = sorted((by_id[pid].amount - by_id[pid].fee) for pid in link.payment_ids)
    assert nets == sorted(cfg.AMBIGUITY_NET_PAISE)

    exact = [
        c for c in combinations(nets, 2) if sum(c) == cfg.AMBIGUITY_CREDIT_PAISE
    ]
    assert len(exact) == 2, f"expected two exact pairs, got {exact}"


def test_the_four_gross_amounts_are_distinct(batch):
    """
    If the pairs shared gross amounts, payer identity alone could break the tie and
    the case would not test amount ambiguity at all.
    """
    link = next(
        t for t in batch.truth if t.bank_txn_id == batch.ambiguity_bank_txn_id
    )
    by_id = {p.id: p for p in batch.inputs.payments}
    grosses = [by_id[pid].amount for pid in link.payment_ids]
    assert len(set(grosses)) == 4


def test_no_triple_can_reach_the_credit(batch):
    """
    Any three of the four already exceed the credit, so only pairs can collide. This
    is what bounds the case to exactly two candidates rather than merely observing
    that it currently is.
    """
    for combo in combinations(cfg.AMBIGUITY_NET_PAISE, 3):
        assert sum(combo) > cfg.AMBIGUITY_CREDIT_PAISE


def test_filler_payments_cannot_participate(batch):
    """
    THE structural guarantee: every other payment in the ambiguity window nets MORE
    than the credit itself. Since all amounts are positive, no subset containing one
    can ever reach the credit -- so the ambiguity cannot be diluted by accident.
    """
    credit = next(
        t for t in batch.inputs.bank_txns if t.id == batch.ambiguity_bank_txn_id
    )
    link = next(
        t for t in batch.truth if t.bank_txn_id == batch.ambiguity_bank_txn_id
    )
    crafted = set(link.payment_ids)
    same_window = [
        p for p in batch.inputs.payments
        if p.captured and p.fee is not None
        and p.id not in crafted
        and build._same_window(p, credit)
    ]
    offenders = [
        p for p in same_window if (p.amount - p.fee) <= cfg.AMBIGUITY_CREDIT_PAISE
    ]
    assert not offenders, (
        f"{len(offenders)} payment(s) in the ambiguity window are small enough to "
        f"participate in a subset reaching the credit; the guarantee is broken"
    )


def test_credit_narration_carries_no_name_or_matching_reference(batch):
    """
    Tier 1 must not be able to fire, and the Fellegi-Sunter name channel must have
    equal (near-zero) evidence for all four candidates. If the narration named a payer
    or the reference matched a payment, the case would be resolvable on evidence other
    than amount, and it would not test ambiguity.
    """
    credit = next(
        t for t in batch.inputs.bank_txns if t.id == batch.ambiguity_bank_txn_id
    )
    assert "RAZORPAY SETTLEMENT" in credit.narration
    refs = {p.id for p in batch.inputs.payments} | {
        p.order_id for p in batch.inputs.payments if p.order_id
    }
    assert credit.ref_no not in refs
    for cust_name in {
        p.notes.get("customer_name", "") for p in batch.inputs.payments
    }:
        if cust_name:
            assert cust_name.upper()[:12] not in credit.narration.upper()


def test_fee_model_inversion_is_exact():
    """
    The case is specified in NET space, so the fee model must invert exactly. If it
    could not, the collision would be approximate and the case would be separable.
    """
    for net in cfg.AMBIGUITY_NET_PAISE:
        gross = fees.gross_for_target_net(net)
        assert fees.net_settled(gross) == net


def test_fee_model_matches_real_captured_payments():
    """
    The generator's fee model must still predict real Razorpay output within its
    measured bound. If this drifts, conservation residuals become untrustworthy and
    MR4 would report violations caused by the checker rather than the matcher.
    """
    import json

    path = cfg.DATA / "real_payments.json"
    if not path.exists():
        pytest.skip("real payments not present")
    captured = [
        p for p in json.loads(path.read_text(encoding="utf-8"))["items"]
        if p["status"] == "captured"
    ]
    residuals = [p["fee"] - fees.fee_and_tax(p["amount"])[0] for p in captured]
    assert (min(residuals), max(residuals)) == fees.KNOWN_RESIDUAL_PAISE
