"""
Direct unit tests for the bounded subset-sum search -- the most complex algorithm here.

Until now this tier was exercised only end to end, and BOTH defects ever found in it
(the overshoot prune firing before the near-miss was recorded, and the uniqueness margin
being measured from the wrong origin) were caught by hand-built cases rather than by the
suite. That is the argument for this file: end-to-end coverage tells you the batch came
out right, not that a function is right across its domain, and every defect in the log
that survived longest was one that returned a plausible number.

The cases below are constructed, not sampled. Each one names the property it pins.
"""

from __future__ import annotations

import config as cfg
import pytest

from recon.engine import fees, tier3_subsetsum as t3
from recon.schemas import Payment

BASE_TS = 1_757_000_000


def pay(pid: str, amount: int, *, fee: int | None = 0, ts_offset: int = 0) -> Payment:
    """
    A payment with an EXACTLY known fee by default, so the settled interval collapses to
    a point (plus the measured +/-2p fee-model slack) and the arithmetic under test is
    the search, not the fee model.
    """
    return Payment(
        id=pid, amount=amount, currency="INR", status="captured", captured=True,
        method="netbanking", order_id=None, created_at=BASE_TS + ts_offset,
        description="", contact="", email="", provenance="S",
        fee=fee, tax=0 if fee is None else 0,
    )


def search(target, pool, **kw):
    return t3.search(target, pool, {}, **kw)


# --------------------------------------------------------------------------
# Enumeration: ALL solutions, never the first. This is the whole point of Layer 2.
# --------------------------------------------------------------------------

def test_single_exact_subset_is_found():
    pool = [pay("p1", 10_000), pay("p2", 25_000), pay("p3", 60_000)]
    r = search(35_000, pool)
    assert len(r.solutions) == 1
    assert r.solutions[0].payment_ids == ("p1", "p2")


def test_two_distinct_subsets_are_BOTH_enumerated():
    """
    A solver that stops at the first valid match reports a confident answer here. The
    J.P. Morgan formalisation's three algorithms all terminate on the first match; this
    is the gap Layer 2 fills, so it is pinned directly.
    """
    pool = [pay("p1", 10_000), pay("p2", 20_000), pay("p3", 30_000), pay("p4", 15_000)]
    r = search(30_000, pool)
    found = {s.payment_ids for s in r.solutions}
    assert found == {("p1", "p2"), ("p3",)}, found


def test_no_subset_within_tolerance_yields_no_solutions():
    pool = [pay("p1", 10_000), pay("p2", 20_000)]
    r = search(77_777, pool)
    assert r.solutions == ()


def test_enumeration_is_independent_of_pool_order():
    """
    The pool is sorted internally by (lo, id) so the result depends on the SET of
    candidates, not the order they arrived. Without that, tier 3 would be one more place
    for input order to reach the answer.
    """
    pool = [pay("p1", 10_000), pay("p2", 20_000), pay("p3", 30_000), pay("p4", 15_000)]
    baseline = {s.payment_ids for s in search(30_000, pool).solutions}
    for rotation in range(1, len(pool)):
        rotated = pool[rotation:] + pool[:rotation]
        assert {s.payment_ids for s in search(30_000, rotated).solutions} == baseline
    assert {s.payment_ids for s in search(30_000, list(reversed(pool))).solutions} == baseline


# --------------------------------------------------------------------------
# Bounds. Each is a documented design invariant and a REFUSAL, not a truncation.
# --------------------------------------------------------------------------

def test_subset_size_bound_is_enforced():
    """k <= MAX_SUBSET_K. A subset needing more members must not be returned."""
    n = cfg.MAX_SUBSET_K + 2
    pool = [pay(f"p{i}", 1_000) for i in range(n)]
    r = search(1_000 * n, pool)
    assert all(len(s.payment_ids) <= cfg.MAX_SUBSET_K for s in r.solutions)
    assert r.solutions == (), "the only exact subset exceeds k and must not be claimed"


def test_solution_cap_sets_capped_and_stops_enumerating():
    """
    Reaching MAX_SOLUTIONS is a VERDICT, not a performance knob: if that many
    decompositions satisfy the credit, the constraint has not identified an answer.
    """
    pool = [pay(f"p{i}", 10_000) for i in range(cfg.MAX_SOLUTIONS + 4)]
    r = search(10_000, pool)
    assert r.capped is True
    assert len(r.solutions) == cfg.MAX_SOLUTIONS


def test_pool_above_MAX_POOL_refuses_rather_than_truncating(monkeypatch):
    """
    Truncating to fit the bound could drop the true decomposition and leave a wrong one
    looking unique -- a confident wrong answer, the worst failure available.
    """
    from recon.engine import tier2_amount_date as t2
    from recon.schemas import BankTxn

    oversized = [pay(f"p{i}", 1_000, ts_offset=i) for i in range(cfg.MAX_POOL + 1)]
    monkeypatch.setattr(t2, "candidate_pool", lambda *a, **k: oversized)
    monkeypatch.setattr(t3.tier2_amount_date, "candidate_pool", lambda *a, **k: oversized)

    txn = BankTxn(
        id="bank_txn_0001", txn_date="2026-09-05", value_date="2026-09-05",
        narration="TEST", ref_no="UTR1", credit=5_000, debit=0, balance=0,
    )
    cands, cat, reason, margin = t3._decompose(txn, tuple(oversized), set(), {})
    assert cat is not None and cat.value == "decomposition_out_of_bounds"
    assert str(cfg.MAX_POOL) in reason
    assert cands == [] and margin == 0.0


# --------------------------------------------------------------------------
# best_miss and the uniqueness margin -- where both known defects lived.
# --------------------------------------------------------------------------

def test_a_near_miss_beyond_the_overshoot_prune_is_still_recorded():
    """
    REGRESSION, DEFECT_LOG 2026-09-02-05 item 3. The overshoot prune used to `break`
    before recording, so a subset sitting a few paise outside tolerance was never
    compared against, best_miss kept a far worse value, and the margin reported perfect
    isolation on a credit that had a near-twin.
    """
    tol = fees.tolerance_for(50_000)
    pool = [pay("p_exact", 50_000), pay("p_near", 50_000 + tol + 5)]
    r = search(50_000, pool, tolerance=tol)
    assert len(r.solutions) == 1
    assert r.best_miss is not None, "the near overshoot was pruned without being recorded"
    assert abs(abs(r.best_miss) - (tol + 5)) <= 4


def test_margin_separates_a_near_twin_from_a_genuinely_isolated_answer():
    """
    REGRESSION, DEFECT_LOG 2026-09-02-05 item 3, second half. Dividing the rival's
    absolute distance by tolerance scored ANY rival more than one tolerance away at 1.0,
    so a rival 5p outside the boundary and one 3 rupees outside both read "perfectly
    isolated". The distance is now measured from the tolerance EDGE.
    """
    tol = fees.tolerance_for(50_000)
    near = search(50_000, [pay("p1", 50_000), pay("p2", 50_000 + tol + 5)], tolerance=tol)
    far = search(50_000, [pay("p1", 50_000), pay("p2", 50_000 + 300_000)], tolerance=tol)

    near_margin = t3.uniqueness_margin(near, tol)
    far_margin = t3.uniqueness_margin(far, tol)
    assert 0.0 <= near_margin < 0.2, near_margin
    assert far_margin == 1.0
    assert near_margin < far_margin


def test_margin_is_zero_when_the_answer_is_not_unique():
    pool = [pay("p1", 10_000), pay("p2", 20_000), pay("p3", 30_000)]
    r = search(30_000, pool)
    assert len(r.solutions) == 2
    assert t3.uniqueness_margin(r, fees.tolerance_for(30_000)) == 0.0


def test_margin_is_one_when_nothing_came_close():
    r = search(10_000, [pay("p1", 10_000)])
    assert r.best_miss is None
    assert t3.uniqueness_margin(r, fees.tolerance_for(10_000)) == 1.0


@pytest.mark.parametrize("tol", [0, -1])
def test_margin_refuses_a_nonpositive_tolerance(tol):
    r = search(10_000, [pay("p1", 10_000)])
    assert t3.uniqueness_margin(r, tol) == 0.0


# --------------------------------------------------------------------------
# Interval arithmetic. Amounts are intervals, never point estimates.
# --------------------------------------------------------------------------

def test_an_unpriced_payment_widens_the_interval_and_is_marked_uncertain():
    """
    fee=None means Razorpay never priced it, so the engine only knows the net within
    MDR_RATE_BAND. The solution must record that it is not certain.
    """
    priced = search(10_000, [pay("p1", 10_000, fee=0)])
    assert priced.solutions[0].certain is True

    unpriced = [pay("p1", 10_000, fee=None)]
    lo, hi = fees.net_interval(unpriced[0]).lo, fees.net_interval(unpriced[0]).hi
    assert hi > lo, "an unpriced payment must span a range"
    r = search((lo + hi) // 2, unpriced)
    assert r.solutions and r.solutions[0].certain is False


def test_empty_pool_returns_an_empty_result_rather_than_raising():
    r = search(10_000, [])
    assert r.solutions == () and r.best_miss is None and r.pool_size == 0
    assert r.capped is False


def test_target_of_zero_finds_nothing_and_does_not_hang():
    r = search(0, [pay("p1", 10_000), pay("p2", 20_000)])
    assert r.solutions == ()
