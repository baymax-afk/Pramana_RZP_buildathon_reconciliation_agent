"""
Verification-as-a-service: the four layers pointed at somebody else's matches.

the 2026-09-03 audit §8's third post-buildathon item, and the one it calls a wedge. The
argument comes from the README's own observation: reconciliation vendors publish coverage
and never precision, because precision needs ground truth *and* a refusal path. The four
layers here are properties of a CLAIM -- "this credit is these payments" -- not of this
matcher, so they hold whoever made it.

**The property that makes it a service rather than a benchmark is that no finding needs
ground truth.** A merchant can point it at an incumbent's Monday output, with nothing
labelled anywhere, and get back the specific claims that do not survive arithmetic. Every
test above the SCORED section here runs without reading truth, and one asserts that.

**The credibility check is the self-audit.** An auditor that flags the matcher it ships
with has a bug in one of the two and no way to say which from the outside. It must pass
its own engine's output cleanly, and that turned out to be a real constraint rather than
a formality -- see `test_uniqueness_is_judged_against_what_the_claimant_had_left`.
"""

from __future__ import annotations

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine.match import match_once
from recon.verify.foreign import (
    CONSERVATION,
    CONTEXT_DEPENDENT,
    DOUBLE_POSTED,
    IDENTITY,
    UNDERDETERMINED,
    UNKNOWN_ID,
    ForeignClaim,
    audit,
    claims_from,
)


@pytest.fixture(scope="module")
def batch():
    inputs = load_inputs()
    out = match_once(inputs)
    # Built by the shared helper, not by hand. Hand-building this dropped the
    # settlement grouping and made the auditor report the engine's own correct output as
    # four double-posts -- in the fixture of the test whose whole job is to catch that.
    own = claims_from(out)
    return inputs, out, own


# ---- the credibility check ------------------------------------------------
def test_the_auditor_passes_its_own_engines_output(batch):
    """
    The control arm. If the auditor flags the matcher it ships with, one of the two is
    wrong and an outside reader cannot tell which -- so every finding it makes about a
    third party becomes unreadable.
    """
    inputs, _, own = batch
    a = audit(inputs, own, claimant="self")
    assert a.survival == 1.0, [f.as_dict() for f in a.findings if f.check != CONTEXT_DEPENDENT]
    assert a.paise_at_risk == 0


def test_uniqueness_is_judged_against_what_the_claimant_had_left(batch):
    """
    Judging uniqueness in the RAW window flagged 2 of this engine's own 126 assignments.

    Both were true: another subset of the window did fit. Both were also fine: a
    different credit had already taken those payments. Any matcher that claims payments
    as it goes has the same property, so the fair question is whether the claim is unique
    given the claimant's OTHER claims -- which is order-free here, because the whole
    claim set is in hand rather than being built up.

    The raw-window case is still reported, as an observation that does not count against
    survival, because it says the claim rests partly on the claim SET.
    """
    inputs, _, own = batch
    a = audit(inputs, own)
    assert not [f for f in a.findings if f.check == UNDERDETERMINED]
    context = [f for f in a.findings if f.check == CONTEXT_DEPENDENT]
    assert context, (
        "no claim in this batch is unique only in context; if the batch changed, this "
        "test has stopped exercising the distinction and should be rewritten"
    )
    # An observation must not move the failure numbers.
    assert a.claims_surviving == a.claims
    assert a.paise_at_risk == 0


def test_the_exposure_figure_counts_failures_only(batch):
    """
    A first version summed every finding, so a self-audit printed 100% survival beside
    "exposure on failed claims Rs 75,890.75". A number that contradicts the line above it
    is worse than a missing one.
    """
    inputs, _, own = batch
    a = audit(inputs, own)
    assert a.survival == 1.0 and a.paise_at_risk == 0


# ---- the checks, each provoked deliberately -------------------------------
def test_a_claim_whose_money_does_not_add_up_fails_conservation(batch):
    inputs, _, own = batch
    victim = own[0]
    other = next(
        p.id for p in inputs.payments
        if p.captured and p.id not in victim.payment_ids
    )
    a = audit(inputs, (ForeignClaim(victim.bank_txn_id, (other,)),))
    checks = {f.check for f in a.findings}
    assert CONSERVATION in checks or IDENTITY in checks, (
        "swapping in an unrelated payment produced no finding at all"
    )
    assert a.survival == 0.0


def test_the_same_payment_claimed_twice_is_caught_on_both_claims(batch):
    """
    Double-posting is a property of the claim SET, so it is computed across all claims
    before any per-claim check. Checked per claim it would report the second occurrence
    and miss the first -- a half-reported double-post.
    """
    inputs, _, own = batch
    a, b = own[0], own[1]
    shared = a.payment_ids[0]
    claims = (a, ForeignClaim(b.bank_txn_id, (shared,)))
    result = audit(inputs, claims)
    flagged = {f.bank_txn_id for f in result.findings if f.check == DOUBLE_POSTED}
    assert flagged == {a.bank_txn_id, b.bank_txn_id}, (
        f"both claims on {shared} must be flagged, got {flagged}"
    )


def test_a_claim_naming_something_absent_is_reported_not_dropped(batch):
    """
    Silently dropping unresolvable rows would improve a claimant's audit by discarding
    its worst rows, which is exactly backwards.
    """
    inputs, _, _ = batch
    a = audit(inputs, (ForeignClaim("bank_txn_does_not_exist", ("pay_nope",)),))
    assert a.claims == 1 and a.claims_surviving == 0
    assert [f.check for f in a.findings] == [UNKNOWN_ID]


def test_claiming_nothing_does_not_buy_a_perfect_audit(batch):
    """
    Coverage and survival are reported together for the same reason the engine's own
    headline is a triple: either alone is trivially gamed.
    """
    inputs, _, _ = batch
    a = audit(inputs, ())
    assert a.survival == 0.0, "an empty claim set must not read as 100% survival"
    assert a.coverage == 0.0
    assert len(a.unclaimed_credits) == a.credits_in_batch
    assert a.unclaimed_paise > 0


# ---- no ground truth is read ----------------------------------------------
def test_no_module_in_the_audit_path_mentions_ground_truth():
    """
    The property that makes this a service and not a benchmark.

    `tests/test_isolation.py` already scans the whole `recon` package, and this file adds
    the specific claim for the two modules a buyer is told need no labelled data.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    pattern = re.compile(r"_truth|ground_truth|TRUTH_DIR")
    for rel in ("recon/verify/foreign.py", "external/naive_matcher.py"):
        source = (root / rel).read_text(encoding="utf-8")
        # Strip docstrings/comments is overkill; the modules simply must not name it.
        assert not pattern.search(source), (
            f"{rel} references ground truth. The claim that this needs no labelled data "
            f"is the whole product argument."
        )


# ---- does the truth-free audit actually predict wrongness? ----------------
@pytest.mark.parametrize(
    "dataset", ["reported", pytest.param("holdout", marks=pytest.mark.slow)]
)
def test_the_audit_misses_no_wrong_claim_on_a_straw_man(dataset):
    """
    The number that licenses the service: **wrong claims MISSED**.

    A missed wrong claim is money posted to the wrong receivable that nobody was told
    about. A false alarm costs an analyst a look. Measured against a deliberately naive
    matcher that always assigns and never refuses -- a straw man, and
    `external/naive_matcher.py` says so in its first line.

    Asserted on both the reported batch and the shifted holdout, because a detector that
    works on one distribution has demonstrated nothing about the next one.
    """
    from external.naive_matcher import match as naive
    from scorer.score import load_truth

    generated = cfg.HOLDOUT if dataset == "holdout" else None
    if generated is not None and not (generated / "manifest.json").is_file():
        pytest.skip("no holdout; run `python run.py holdout`")

    inputs = load_inputs(generated_dir=generated)
    truth_path = (
        (generated / "_truth" / "ground_truth.json")
        if generated is not None
        else cfg.TRUTH_DIR / "ground_truth.json"
    )
    _, links = load_truth(truth_path)
    truth = {
        l.bank_txn_id: set(l.payment_ids)
        for l in links
        if l.bank_txn_id and l.expected_verdict == "assign"
    }

    claims = naive(inputs)
    a = audit(inputs, claims)
    flagged = {f.bank_txn_id for f in a.findings if f.check != CONTEXT_DEPENDENT}

    missed = [
        c.bank_txn_id
        for c in claims
        if c.bank_txn_id not in flagged
        and truth.get(c.bank_txn_id) != set(c.payment_ids)
    ]
    assert not missed, (
        f"{len(missed)} wrong claim(s) survived every check with no ground truth read: "
        f"{missed[:6]}"
    )
    # And the straw man must actually be wrong often enough for that to mean something.
    wrong = sum(1 for c in claims if truth.get(c.bank_txn_id) != set(c.payment_ids))
    assert wrong > 20, (
        f"the straw man only made {wrong} wrong claims; catching all of them is not "
        f"evidence of anything and this test has stopped measuring"
    )
