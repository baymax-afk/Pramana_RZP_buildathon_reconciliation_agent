"""
Ground truth must not assert a match the engine cannot reach.

This is the fourth time this project has shipped that defect, in three different
disguises, so it is now checked structurally rather than per-defect:

  * `refund_netted` deducted a refund from a credit and recorded it nowhere
    (DEFECT_LOG 2026-09-02-05 item 4) -- all 5 cases refused, every one a miss.
  * `partial_payment` shrank the CREDIT and left the payment at full value
    (2026-09-02-08) -- partial recall 0/5 for the entire life of the project.
  * `_protect_ambiguity_window` shifted an interloper 6 days forward while its own
    credit was at most 5 days away, orphaning it (2026-09-02-08) -- 5 of 40 seeds.

Every one presented as an ENGINE coverage problem. The engine refused, correctly, on
the evidence it was given; the scorer recorded a miss; and the investigation went to the
matcher. That is the expensive part, and it is why this check exists at the generator.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import config as cfg
from recon.generator import build


SEEDS = [20260905, 77771, 11111, 22222, 33333, 44444, 55555]


@pytest.mark.parametrize("seed", SEEDS)
def test_every_assign_link_is_satisfiable(seed):
    batch = build.generate(seed=seed)
    assert build.assert_truth_is_satisfiable(batch) > 0


def test_the_check_catches_a_payment_moved_out_of_its_credits_window(batch):
    """
    The orphaning shape. Without this the check is only asserting that the current
    generator happens to be correct, which is not the same as being able to detect
    incorrectness.
    """
    link = next(
        l for l in batch.truth
        if l.expected_verdict == "assign" and l.bank_txn_id and l.payment_ids
    )
    target = link.payment_ids[0]
    moved = tuple(
        replace(p, created_at=p.created_at + 86_400 * 90) if p.id == target else p
        for p in batch.inputs.payments
    )
    broken = replace(batch, inputs=replace(batch.inputs, payments=moved))

    with pytest.raises(AssertionError, match="outside the credit's window"):
        build.assert_truth_is_satisfiable(broken)


def test_the_check_catches_money_hidden_from_a_credit(batch):
    """
    The hidden-money shape -- `refund_netted` and the old `partial_payment` both had it.
    Shrinking a credit while leaving its payments alone must be caught.
    """
    link = next(
        l for l in batch.truth
        if l.expected_verdict == "assign" and l.bank_txn_id and l.payment_ids
    )
    shrunk = tuple(
        replace(t, credit=int(t.credit * 0.5)) if t.id == link.bank_txn_id else t
        for t in batch.inputs.bank_txns
    )
    broken = replace(batch, inputs=replace(batch.inputs, bank_txns=shrunk))

    with pytest.raises(AssertionError, match="money is unaccounted for"):
        build.assert_truth_is_satisfiable(broken)


def test_a_partial_payment_agrees_with_its_credit(batch):
    """
    The fix for partial recall, stated as a property. A partial payment is a SMALLER
    PAYMENT against a larger invoice -- Razorpay cannot capture Rs 21,999 and settle
    Rs 13,573. Payment, fee and credit agree exactly; what is partial is the INVOICE's
    coverage.
    """
    from recon.engine import fees as engine_fees

    pay = {p.id: p for p in batch.inputs.payments}
    txn = {t.id: t for t in batch.inputs.bank_txns}
    inv = {i.invoice_no: i for i in batch.inputs.invoices}

    partials = [l for l in batch.truth if l.relation == "partial"]
    assert partials, "no partial cases in this batch; the test proves nothing"

    for link in partials:
        t = txn[link.bank_txn_id]
        group = [pay[pid] for pid in link.payment_ids]
        interval = engine_fees.expected_credit_interval(group, inv)
        tol = engine_fees.tolerance_for(t.credit)
        assert interval.lo - tol <= t.credit <= interval.hi + tol, (
            f"{link.bank_txn_id}: partial credit {t.credit}p does not agree with its "
            f"payment's settled interval [{interval.lo}, {interval.hi}]p"
        )
        # The invoice, not the payment, is what is left partial.
        for no in link.invoice_nos:
            assert inv[no].status == "part_settled", (
                f"invoice {no} settles a partial payment but is still {inv[no].status!r}"
            )
            assert inv[no].gross_amount > sum(p.amount for p in group), (
                f"invoice {no} is not actually under-settled"
            )


def test_partial_payments_never_touch_a_real_captured_record(batch):
    """
    R1 records are genuinely captured Razorpay payments whose amount, fee and tax are
    real API output. Manufacturing a defect by rewriting one would falsify exactly the
    provenance claim that makes those 18 records worth having.
    """
    pay = {p.id: p for p in batch.inputs.payments}
    for link in batch.truth:
        if link.relation != "partial":
            continue
        for pid in link.payment_ids:
            assert pay[pid].provenance == "S", (
                f"{pid} is provenance {pay[pid].provenance}, but partial_payment "
                f"rewrites amount and fee and may only be applied to synthetic records"
            )


@pytest.mark.parametrize("seed", SEEDS)
def test_a_shrunken_payment_never_falls_below_the_tolerance_floor(seed):
    """
    MIN_PAYMENT_PAISE is what keeps TOL_ABS_PAISE 100x below the smallest payment, and
    config.py asserts the two against each other at import. Shrinking a payment through
    that floor would quietly invalidate the subset-sum uniqueness argument for the whole
    batch.
    """
    b = build.generate(seed=seed)
    for p in b.inputs.payments:
        if p.captured:
            assert p.amount >= cfg.MIN_PAYMENT_PAISE, (
                f"{p.id} is {p.amount}p, below MIN_PAYMENT_PAISE="
                f"{cfg.MIN_PAYMENT_PAISE}p"
            )


# --------------------------------------------------------------------------
# The manifest: which seed actually produced the batch on disk
# --------------------------------------------------------------------------

def test_generated_batch_records_the_seed_that_built_it(tmp_path):
    import json

    b = build.generate(seed=44444)
    paths = build.write(b, out_dir=tmp_path)
    meta = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert meta["seed"] == 44444
    assert meta["payments"] == len(b.inputs.payments)


def test_loading_with_a_seed_the_batch_did_not_come_from_is_refused(tmp_path):
    """
    `match --seed X` does NOT regenerate -- it loads whatever is on disk. Without this
    guard a batch built at 20260905 could be matched, scored and printed as
    "seed=77771": the headline block naming a seed that did not produce its numbers.
    In a project whose argument is reproducibility that is not a cosmetic problem.
    """
    from loaders import load_inputs

    build.write(build.generate(seed=44444), out_dir=tmp_path)
    with pytest.raises(ValueError, match="the batch is seed 44444"):
        load_inputs(tmp_path, seed=77771)


def test_loading_without_a_seed_adopts_the_batchs_own(tmp_path):
    from loaders import load_inputs

    build.write(build.generate(seed=44444), out_dir=tmp_path)
    assert load_inputs(tmp_path).seed == 44444


# --------------------------------------------------------------------------
# R1 / R2 — a reported number must never name a run that did not produce it
# --------------------------------------------------------------------------

def test_reported_seed_and_density_come_from_the_batch_not_the_flags(tmp_path):
    """
    REGRESSION, REVIEW_2026-09-02 R1. `match --seed X` does not regenerate: it loads
    whatever is on disk. The manifest guard was added to stop the headline naming a seed
    that did not produce its numbers, and it closed only the loud path.

    Reproduced before the fix: a batch generated at seed 77771 / ppw 12, matched with no
    flags, printed `seed=20260905 density=6` and wrote a payload saying seed 20260905
    with density 12 -- inconsistent with the headline AND with itself, because the
    payload took density from the corrected inputs and seed from the uncorrected args.
    """
    from loaders import load_inputs

    build.write(build.generate(seed=77771, payments_per_window=12), out_dir=tmp_path)
    inputs = load_inputs(tmp_path)
    assert inputs.seed == 77771
    assert inputs.payments_per_window == 12


def test_naming_the_default_seed_explicitly_is_still_checked(tmp_path):
    """
    REGRESSION, REVIEW_2026-09-02 R2. The guard read `seed != cfg.SEED_PRIMARY`, but
    argparse DEFAULTS --seed to that value -- so an explicit `--seed 20260905` was
    indistinguishable from no flag at all and skipped the check entirely, silently
    relabelling the run. Only a sentinel can tell "not passed" from "passed, and happens
    to equal the default".
    """
    from loaders import load_inputs

    build.write(build.generate(seed=77771), out_dir=tmp_path)
    with pytest.raises(ValueError, match="seed 20260905 was requested"):
        load_inputs(tmp_path, seed=cfg.SEED_PRIMARY)


def test_a_density_mismatch_is_reported_too(tmp_path):
    """
    REGRESSION, REVIEW_2026-09-02 R2. `payments_per_window` was overwritten with NO
    check at all, so a density mismatch was never reported under any invocation.
    """
    from loaders import load_inputs

    build.write(build.generate(seed=44444, payments_per_window=12), out_dir=tmp_path)
    with pytest.raises(ValueError, match="density 6 was requested"):
        load_inputs(tmp_path, payments_per_window=6)


def test_both_mismatches_are_reported_together(tmp_path):
    """One error naming both beats two runs to discover them one at a time."""
    from loaders import load_inputs

    build.write(build.generate(seed=44444, payments_per_window=12), out_dir=tmp_path)
    with pytest.raises(ValueError) as e:
        load_inputs(tmp_path, seed=11111, payments_per_window=3)
    assert "seed 11111" in str(e.value) and "density 3" in str(e.value)


def test_omitting_both_flags_adopts_the_batch(tmp_path):
    from loaders import load_inputs

    build.write(build.generate(seed=44444, payments_per_window=12), out_dir=tmp_path)
    inputs = load_inputs(tmp_path)
    assert (inputs.seed, inputs.payments_per_window) == (44444, 12)


def test_a_batch_with_no_manifest_falls_back_to_config_defaults(tmp_path):
    """A fixture directory written before manifests existed must still load."""
    from loaders import load_inputs

    build.write(build.generate(seed=44444), out_dir=tmp_path)
    (tmp_path / "manifest.json").unlink()
    inputs = load_inputs(tmp_path)
    assert inputs.seed == cfg.SEED_PRIMARY
    assert inputs.payments_per_window == cfg.TARGET_POOL_SIZE


# --------------------------------------------------------------------------
# R9 / R10 — two guards that would have failed silently
# --------------------------------------------------------------------------

def test_the_ambiguity_guard_scans_exactly_the_engines_window(monkeypatch):
    """
    REGRESSION, REVIEW_2026-09-02 R9. `_protect_ambiguity_window` recomputed its scan as
    `SETTLEMENT_WINDOW_DAYS + 2` under a comment claiming to be "deliberately wider than
    the engine's rule". It was EQUAL to `cfg.LOOKBACK_DAYS`, so the stated margin was
    zero -- and worse, it would have fallen BEHIND the engine the moment
    `MAX_SETTLEMENT_DRIFT_DAYS` was widened, leaving interlopers the engine could see
    neither detected nor relocated and the centrepiece guarantee void with nothing
    raising.

    Measured with drift widened to 3: the old expression left an unguarded interloper on
    2 of 6 seeds; reading `cfg.LOOKBACK_DAYS` leaves none. The fix is the COUPLING, not a
    margin -- a positive margin makes the protected band wider than a payment's own
    candidate window, which makes some batches unconstructible (it made seed 11111 fail).
    """
    from recon.engine import tier2_amount_date as t2

    assert cfg.AMBIGUITY_GUARD_MARGIN_DAYS == 0

    b = build.generate(seed=cfg.SEED_PRIMARY)
    amb = b.ambiguity_bank_txn_id
    assert amb, "no ambiguity case in this batch"
    credit = next(t for t in b.inputs.bank_txns if t.id == amb)
    link = next(l for l in b.truth if l.bank_txn_id == amb)
    lo, hi = t2.window_for(credit)
    crafted = set(link.payment_ids)

    intruders = [
        p.id for p in b.inputs.payments
        if p.captured and p.fee is not None and p.id not in crafted
        and lo <= t2.payment_date(p) <= hi
        and (p.amount - p.fee) <= cfg.AMBIGUITY_CREDIT_PAISE
    ]
    assert not intruders, (
        f"payments the engine CAN see could join the ambiguity credit: {intruders}"
    )


@pytest.mark.parametrize("seed", [20260905, 77771, 11111, 22222, 33333, 55555])
def test_every_sweep_seed_is_still_constructible(seed):
    """
    The R9 fix must not cost a batch. Widening the guard's scan by 2 days made seed
    11111 -- one of the sweep's own default seeds -- impossible to construct, because a
    payment's entire candidate window fell inside the protected band.
    """
    b = build.generate(seed=seed)
    assert build.assert_truth_is_satisfiable(b) > 0
    assert build.assert_ambiguity_is_exact(b) == cfg.AMBIGUITY_EXPECTED_CANDIDATES


def test_a_narration_count_that_contradicts_the_link_is_caught(batch):
    """
    REGRESSION, REVIEW_2026-09-02 R10. `narration_count_conflict` became a hard refusal
    channel, which makes a third shape of unreachable link: `assign` over k payments on
    a credit narrating a different count. It held only by construction, and nothing
    asserted it -- the same position `partial_payment` was in when recall sat at 0/5.
    """
    from dataclasses import replace

    link = next(
        l for l in batch.truth
        if l.expected_verdict == "assign" and l.bank_txn_id and len(l.payment_ids) == 1
    )
    relabelled = tuple(
        replace(t, narration="RAZORPAY SETTLEMENT setl_COUNTCHECK01 4 TXNS")
        if t.id == link.bank_txn_id else t
        for t in batch.inputs.bank_txns
    )
    broken = replace(batch, inputs=replace(batch.inputs, bank_txns=relabelled))
    with pytest.raises(AssertionError, match="states 4 transaction"):
        build.assert_truth_is_satisfiable(broken)
