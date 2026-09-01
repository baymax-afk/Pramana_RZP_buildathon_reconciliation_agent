"""
Tolerance and density invariants.

These are the two settings that silently invalidate every many-to-one result if they
drift, and neither failure is visible in the output -- the engine keeps producing
confident assignments, they just stop meaning anything. So they are asserted rather
than trusted.
"""

from __future__ import annotations

import config as cfg
from recon.generator import build


def test_tolerance_far_below_smallest_payment(batch):
    """
    If tolerance approaches the smallest payment in a pool, a subset S and the subset
    S plus one small payment BOTH satisfy the constraint. Every many-to-one result
    becomes meaningless and the uniqueness test degrades into noise -- while still
    reporting high confidence. A 100x margin is required.
    """
    tol, smallest = build.assert_tolerance_sanity(batch)
    assert smallest / tol >= 100


def test_density_invariant_at_default(batch):
    """At the default density no settlement window may exceed MAX_POOL."""
    assert build.assert_pool_bound(batch) <= cfg.MAX_POOL


def test_date_range_is_derived_from_density_not_fixed():
    """
    The whole density invariant rests on this: holding payments-per-window fixed and
    letting the calendar widen with n. If the range were fixed instead, scaling n
    would crowd the windows and the subset-sum search would degrade -- quietly.
    """
    windows = {}
    for ppw in (6, 12, 24):
        b = build.generate(seed=cfg.SEED_PRIMARY, payments_per_window=ppw)
        windows[ppw] = b.stats["windows"]
    assert windows[6] > windows[12] > windows[24], (
        f"date range is not widening as density falls: {windows}"
    )


def test_higher_density_produces_larger_pools():
    """
    The sweep must actually vary what it claims to vary. If pool size did not rise
    with density, the sweep would be measuring nothing.
    """
    worst = {}
    for ppw in cfg.DENSITY_SWEEP:
        b = build.generate(seed=cfg.SEED_PRIMARY, payments_per_window=ppw)
        worst[ppw] = build.assert_pool_bound(b)
    ordered = [worst[p] for p in sorted(cfg.DENSITY_SWEEP)]
    assert ordered == sorted(ordered), f"pool size not monotonic in density: {worst}"
    assert ordered[-1] > ordered[0]


def test_high_density_is_allowed_to_exceed_max_pool():
    """
    Above the default density, an oversized pool is DATA, not a defect -- it is the
    condition the sweep exists to study, and the engine handles it by refusing. The
    generator must not refuse to build it, or the sweep becomes impossible.
    """
    b = build.generate(seed=cfg.SEED_PRIMARY, payments_per_window=max(cfg.DENSITY_SWEEP))
    worst = build.assert_pool_bound(b)  # must NOT raise
    assert worst > cfg.MAX_POOL


def test_all_nine_defect_categories_present(batch):
    """Every category must actually be injected, or the batch understates difficulty."""
    from recon.schemas import DEFECT_LABELS

    seen = {lbl for t in batch.truth for lbl in t.defect_labels}
    missing = set(DEFECT_LABELS) - seen
    assert not missing, f"defect categories never injected: {sorted(missing)}"


def test_batch_reaches_target_size(batch):
    assert batch.stats["payments"] >= 200


def test_all_three_provenance_tiers_present(batch):
    """R1, R2 and S must all be represented, or the disclosure is inaccurate."""
    prov = batch.stats["provenance"]
    assert set(prov) == {"R1", "R2", "S"}, prov
    assert prov["R1"] > 0 and prov["R2"] > 0 and prov["S"] > 0


def test_generation_is_deterministic():
    """Same seed, same batch -- otherwise nothing downstream is reproducible."""
    a = build.generate(seed=cfg.SEED_PRIMARY)
    b = build.generate(seed=cfg.SEED_PRIMARY)
    assert [p.id for p in a.inputs.payments] == [p.id for p in b.inputs.payments]
    assert [t.credit for t in a.inputs.bank_txns] == [t.credit for t in b.inputs.bank_txns]
    assert a.stats == b.stats


def test_different_seeds_produce_different_batches(batch, batch_second_seed):
    """Guards against a seed that is accidentally ignored."""
    assert [p.id for p in batch.inputs.payments] != [
        p.id for p in batch_second_seed.inputs.payments
    ]


def test_r2_records_are_marked_and_derived_from_real_orders(batch):
    """
    R2 payments carry a real Razorpay order id but a SYNTHETIC fee. The provenance
    stamp is the only thing preventing them being read as genuinely captured revenue,
    so it must be present on every one.
    """
    r2 = [p for p in batch.inputs.payments if p.provenance == "R2"]
    assert r2
    for p in r2:
        assert p.order_id and p.order_id.startswith("order_")
        assert p.fee is not None  # synthetic, from the R1 rate model
