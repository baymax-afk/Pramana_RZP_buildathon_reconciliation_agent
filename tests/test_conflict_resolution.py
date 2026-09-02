"""
Layer 2, applied to the CLAIMING loop: two credits, one payment, and no sort order.

The matching loop used to be greedy -- credits were walked in a fixed order and each
took what it wanted, so when two credits both had a viable claim on the same payment
the winner was whichever the sort reached first. The permutation gate could detect that
after the fact, but detecting a design weakness is weaker than not having it.

These tests exercise `_resolve_contested` directly, because the production batch does
NOT contest a single payment (measured: 129 proposals, 0 contests over 3 rounds). A
mechanism that never fires on the real data is exactly the kind of thing that rots
unnoticed, so it is tested against constructed conflicts rather than hoped about.

The property that matters most is the LAST one: equal evidence refuses BOTH claims. A
tie is not something to break. It is the engine saying two credits have an equally good
claim on the same money, which is the same underdetermination Layer 2 refuses on,
arriving through a different door.
"""

from __future__ import annotations

import itertools

import pytest

from recon.engine.match import _Proposal, _resolve_contested
from recon.engine.results import Candidate
from recon.engine.tier1_reference import TIER as TIER1
from recon.engine.tier2_amount_date import TIER as TIER2
from recon.engine.tier3_subsetsum import TIER as TIER3


def _prop(txn_id: str, payment_ids: tuple[str, ...], tier: str = TIER3, fs: float = 5.0):
    return _Proposal(
        txn_id=txn_id,
        credit=100_000,
        cand=Candidate(
            payment_ids=payment_ids, residual_paise=0, tier=tier,
            interval_lo=99_900, interval_hi=100_100, certain=True,
        ),
        uniqueness=1.0,
        fs_weight=fs,
    )


def test_uncontested_proposals_are_all_granted():
    props = [_prop("t1", ("p1",)), _prop("t2", ("p2",)), _prop("t3", ("p3", "p4"))]
    granted, contested = _resolve_contested(props)
    assert {g.txn_id for g in granted} == {"t1", "t2", "t3"}
    assert contested == []


def test_stronger_fs_weight_wins_the_contested_payment():
    strong = _prop("t_strong", ("p1",), fs=8.0)
    weak = _prop("t_weak", ("p1",), fs=4.5)
    granted, contested = _resolve_contested([strong, weak])
    assert [g.txn_id for g in granted] == ["t_strong"]
    assert [c[0].txn_id for c in contested] == ["t_weak"]


def test_tier_precedence_outranks_a_higher_fs_weight():
    """
    Tier 1 is an exact reference agreement. A tier-3 subset-sum bid with a bigger FS
    weight does not outrank it, because the tiers ARE the declared evidence hierarchy --
    if FS weight could overturn a reference match, the hierarchy would be decorative.
    """
    reference = _prop("t_ref", ("p1",), tier=TIER1, fs=1.0)
    searched = _prop("t_sub", ("p1",), tier=TIER3, fs=9.0)
    granted, contested = _resolve_contested([reference, searched])
    assert [g.txn_id for g in granted] == ["t_ref"]
    assert [c[0].txn_id for c in contested] == ["t_sub"]


def test_equal_evidence_refuses_BOTH_claims():
    """The whole point. A tie is a refusal, not a coin toss."""
    a = _prop("t_a", ("p1",), tier=TIER2, fs=6.0)
    b = _prop("t_b", ("p1",), tier=TIER2, fs=6.0)
    granted, contested = _resolve_contested([a, b])
    assert granted == [], "a tie must not award the payment to either credit"
    assert {c[0].txn_id for c in contested} == {"t_a", "t_b"}


def test_a_tie_is_not_broken_by_float_noise():
    """
    FS weights are float sums. Two bids equal in every respect that matters must not be
    separated by the last bit -- that would be a coin toss wearing the costume of
    evidence. The evidence key rounds, so near-identical weights still tie.
    """
    a = _prop("t_a", ("p1",), tier=TIER2, fs=6.0)
    b = _prop("t_b", ("p1",), tier=TIER2, fs=6.0 + 1e-12)
    granted, contested = _resolve_contested([a, b])
    assert granted == []
    assert len(contested) == 2


def test_resolution_is_independent_of_proposal_order():
    """
    THE order-independence property, checked over every permutation rather than one
    shuffle. If this can fail, the engine has simply moved iteration-order dependence
    from the claiming loop into the resolver.
    """
    base = [
        _prop("t_a", ("p1", "p2"), tier=TIER2, fs=6.0),
        _prop("t_b", ("p2",), tier=TIER2, fs=6.0),
        _prop("t_c", ("p3",), tier=TIER3, fs=7.5),
        _prop("t_d", ("p3",), tier=TIER1, fs=2.0),
        _prop("t_e", ("p9",), tier=TIER3, fs=0.5),
    ]
    outcomes = set()
    for perm in itertools.permutations(base):
        granted, contested = _resolve_contested(list(perm))
        outcomes.add((
            frozenset(g.txn_id for g in granted),
            frozenset(c[0].txn_id for c in contested),
        ))
    assert len(outcomes) == 1, f"resolution depended on proposal order: {outcomes}"
    granted_ids, contested_ids = outcomes.pop()
    # t_a and t_b tie on p2 -> both refused. t_d (tier 1) beats t_c on p3.
    # t_e is uncontested.
    assert granted_ids == {"t_d", "t_e"}
    assert contested_ids == {"t_a", "t_b", "t_c"}


def test_a_multi_payment_bid_is_contested_if_ANY_of_its_payments_is():
    """
    A many-to-one decomposition is all-or-nothing: it is a claim that THESE payments
    together account for the credit. Losing one of them does not leave a smaller valid
    claim, it invalidates the arithmetic, so contesting any member contests the bid.
    """
    big = _prop("t_big", ("p1", "p2", "p3"), tier=TIER2, fs=6.0)
    small = _prop("t_small", ("p3",), tier=TIER2, fs=6.0)
    granted, contested = _resolve_contested([big, small])
    assert granted == []
    assert {c[0].txn_id for c in contested} == {"t_big", "t_small"}


def test_the_loser_names_the_rival_that_beat_it():
    """An exception that does not say who else wanted the money is not actionable."""
    strong = _prop("t_strong", ("p1",), fs=8.0)
    weak = _prop("t_weak", ("p1",), fs=4.5)
    _, contested = _resolve_contested([strong, weak])
    (loser, blockers) = contested[0]
    assert loser.txn_id == "t_weak"
    assert [b.txn_id for b in blockers] == ["t_strong"]


def test_three_way_contest_grants_only_the_strict_winner():
    a = _prop("t_a", ("p1",), tier=TIER2, fs=9.0)
    b = _prop("t_b", ("p1",), tier=TIER2, fs=6.0)
    c = _prop("t_c", ("p1",), tier=TIER2, fs=6.0)
    granted, contested = _resolve_contested([a, b, c])
    assert [g.txn_id for g in granted] == ["t_a"]
    assert {x[0].txn_id for x in contested} == {"t_b", "t_c"}


@pytest.mark.parametrize("n", [0, 1])
def test_degenerate_proposal_counts(n):
    props = [_prop(f"t{i}", (f"p{i}",)) for i in range(n)]
    granted, contested = _resolve_contested(props)
    assert len(granted) == n
    assert contested == []
