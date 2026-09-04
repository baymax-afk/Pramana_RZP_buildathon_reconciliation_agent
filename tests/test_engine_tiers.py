"""
Matching engine, tiers 1 and 2.

The engine's job at this stage is narrow and should be judged narrowly: match
one-to-one settlements on exact reference or on amount-plus-date, and decline
everything else. Coverage is expected to be incomplete — many-to-one decomposition is
Block 6 — so these tests assert **precision and refusal behaviour**, not coverage.

That split is the whole thesis in miniature: an engine that assigns less but is right
about what it assigns is more useful than one that assigns everything and is quietly
wrong about some of it.
"""

from __future__ import annotations

import json

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine import fees, tier2_amount_date
from recon.engine.match import match_once
from recon.engine.normalize import needs_llm, normalise_name, parse
from recon.engine.results import RefusalCategory
from recon.generator import build


@pytest.fixture(scope="module")
def written(tmp_path_factory):
    """Generate, write, and load back — exercising the real disk round trip."""
    out = tmp_path_factory.mktemp("generated")
    batch = build.generate(seed=cfg.SEED_PRIMARY)
    build.write(batch, out_dir=out)
    return batch, load_inputs(out), out


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------
def test_bank_txn_ids_survive_the_disk_round_trip(written):
    """
    The loader derives row ids from position in the file, so the generator's ids must
    agree with the file's final ordering. If they drift apart, every ground-truth
    reference points at the wrong row — and nothing raises. Precision would collapse
    for a reason that looks like a matcher failure.
    """
    batch, inputs, out = written
    truth = json.loads((out / "_truth" / "ground_truth.json").read_text(encoding="utf-8"))
    loaded = {t.id for t in inputs.bank_txns}
    referenced = {l["bank_txn_id"] for l in truth["links"] if l["bank_txn_id"]}
    assert not (referenced - loaded), "ground truth references rows the loader did not produce"


def test_amounts_survive_the_rupee_paise_round_trip(written):
    batch, inputs, _ = written
    by_id = {t.id: t for t in inputs.bank_txns}
    for original in batch.inputs.bank_txns:
        assert by_id[original.id].credit == original.credit
        assert by_id[original.id].debit == original.debit


# --------------------------------------------------------------------------
# Narration parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "narration,style,has_merchant_ref,has_name",
    [
        ("NEFT-PUNBR67363667630-SUNRISE TEXTILES L-INV-2026-1001-CR", "neft", True, True),
        ("NEFT-AXISP60587771111-ACME INDUSTRIAL SU-CR", "neft", False, True),
        ("UPI/98454901578/PAYMENT/ACME INDUSTR/INV-2026-1042", "upi", True, True),
        ("RTGS-HDFCN12345678901-BHARAT TRADERS-CR", "rtgs", False, True),
        ("RAZORPAY SETTLEMENT setl_dFGFrGyQBgxjkC 5 TXNS", "settlement", False, False),
    ],
)
def test_narration_parsing(narration, style, has_merchant_ref, has_name):
    p = parse(narration)
    assert p.style == style
    assert bool(p.merchant_ref) is has_merchant_ref
    assert bool(p.payer_name) is has_name


def test_settlement_batch_yields_no_payer_name_and_is_not_sent_to_the_llm():
    """
    A settlement batch covers many payers, so having no single name is the CORRECT
    parse, not a failure. Handing it to the LLM would invite a hallucinated payer —
    and the trust boundary depends on the LLM never being asked to invent identity.
    """
    p = parse("RAZORPAY SETTLEMENT setl_ABCDEFGH12345 7 TXNS")
    assert p.payer_name is None
    assert not needs_llm(p)


def test_quoted_invoice_is_not_mistaken_for_the_payer_name():
    p = parse("NEFT-PUNBR67363667630-SUNRISE TEXTILES L-INV-2026-1001-CR")
    assert p.merchant_ref == "INV-2026-1001"
    assert "INV-2026" not in (p.payer_name or "")


def test_parse_never_raises_on_junk():
    for junk in ["", "   ", "????", "\x00\x01", "A" * 500]:
        assert parse(junk) is not None


def test_normalise_name_folds_corporate_suffixes():
    assert normalise_name("Acme Retail Pvt Ltd") == normalise_name("Acme Retail Private Limited")
    assert normalise_name("Sunrise Textiles Ltd") != normalise_name("Sunline Textiles Ltd")


# --------------------------------------------------------------------------
# The fee band — the engine must stay blind to the exact rate
# --------------------------------------------------------------------------
def test_engine_never_imports_the_generator_fee_model():
    """
    If the engine used the generator's exact schedule, MR4 conservation would be
    tautological — the engine would reconcile because it was inverting the very
    function that produced the data.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fees))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("generator" in m for m in imported), imported


def test_known_fee_gives_a_tight_interval_and_unknown_gives_a_wide_one(written):
    _, inputs, _ = written
    priced = next(p for p in inputs.payments if p.fee is not None)
    tight = fees.net_interval(priced)
    assert tight.certain and tight.width <= 4

    from dataclasses import replace

    unpriced = replace(priced, fee=None, tax=None)
    wide = fees.net_interval(unpriced)
    assert not wide.certain and wide.width > tight.width


def test_uncertain_interval_scores_lower_confidence_than_a_certain_one(written):
    """
    Landing inside a band the engine could not narrow is weaker evidence than landing
    on a point it could. Without this penalty an unpriced payment would score
    identically to a priced one, overstating what is known.
    """
    from dataclasses import replace

    _, inputs, _ = written
    p = next(x for x in inputs.payments if x.fee is not None)
    credit = p.amount - p.fee
    certain = fees.residual_tightness(credit, fees.net_interval(p))
    uncertain = fees.residual_tightness(credit, fees.net_interval(replace(p, fee=None, tax=None)))
    assert certain > uncertain


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def test_match_is_deterministic(written):
    _, inputs, _ = written
    assert match_once(inputs).assignment_map == match_once(inputs).assignment_map


def test_no_payment_is_assigned_twice(written):
    """Double-posting is the worst failure mode available: it moves money twice."""
    _, inputs, _ = written
    out = match_once(inputs)
    seen: set[str] = set()
    for a in out.assignments:
        for pid in a.payment_ids:
            assert pid not in seen, f"{pid} assigned to more than one credit"
            seen.add(pid)


def test_every_credit_gets_exactly_one_verdict(written):
    _, inputs, _ = written
    out = match_once(inputs)
    credits = {t.id for t in inputs.bank_txns if t.is_credit}
    # FOUR outcomes, not three: a credit settled inside a settlement group is accounted
    # for too. This assertion was written against the three-way split and went on
    # passing right up until groups existed, at which point it reported four correctly
    # settled credits as having received no verdict. `credit_verdicts` is now the one
    # place that sentence is written.
    assert set(out.credit_verdicts) == credits
    assert len(out.credit_verdicts) == len(credits), "a credit received two verdicts"


def test_every_debit_gets_exactly_one_verdict(written):
    """
    The other half of the statement, which had no verdicts at all until Layer 2c.

    The relation above was true of what it examined and false of the statement, which is
    the more dangerous of the two: it asserted that every record was accounted for while
    silently meaning every CREDIT.
    """
    _, inputs, _ = written
    out = match_once(inputs)
    debits = {t.id for t in inputs.bank_txns if not t.is_credit and t.debit > 0}
    assert set(out.debit_verdicts) == debits
    assert len(out.debit_verdicts) == len(debits), "a debit received two verdicts"


def test_precision_is_perfect_on_what_tiers_1_and_2_choose_to_assign(written):
    """
    THE test for this block. Coverage is deliberately partial; being right about what
    is assigned is not negotiable. An engine that assigns less and is correct beats one
    that assigns everything and is quietly wrong.
    """
    batch, inputs, _ = written
    out = match_once(inputs)
    truth = {t.bank_txn_id: t for t in batch.truth if t.bank_txn_id}
    wrong = [
        a for a in out.assignments
        if a.bank_txn_id not in truth
        or set(truth[a.bank_txn_id].payment_ids) != set(a.payment_ids)
        or truth[a.bank_txn_id].expected_verdict != "assign"
    ]
    assert not wrong, f"{len(wrong)} incorrect assignments, e.g. {wrong[:2]}"


def test_many_to_one_is_only_ever_assigned_by_tier_3(written):
    """
    Tiers 1 and 2 match a credit to a SINGLE payment. If either ever assigns a
    settlement batch, it matched a multi-payment credit to one payment by coincidence
    -- a confident wrong answer, the exact failure this architecture exists to prevent.
    Decomposition is tier 3's job alone.
    """
    batch, inputs, _ = written
    out = match_once(inputs)
    truth = {t.bank_txn_id: t for t in batch.truth if t.bank_txn_id}
    for a in out.assignments:
        rel = truth[a.bank_txn_id].relation
        if rel == "many_to_one":
            assert a.tier == "tier3_subsetsum", (
                f"{a.bank_txn_id} is a settlement batch but was assigned by {a.tier}"
            )
        if a.tier in {"tier1_reference", "tier2_amount_date"}:
            assert len(a.payment_ids) == 1


def test_reference_match_with_a_wrong_amount_is_refused_not_assigned(written):
    """
    Reference and conservation are independent channels. When they disagree the amount
    channel wins and the engine refuses, rather than letting a matching reference
    override money that does not balance.

    **This test used to pass because of a generator defect.** It searched the batch for
    an existing `unexplained_residual` refusal and asserted its truth relation was
    `partial` or `many_to_one` -- which is to say it encoded the broken behaviour as the
    expected one. Those refusals existed only because `partial_payment` shrank the credit
    and left the payment at full value, hiding the money (DEFECT_LOG 2026-09-02-08). Fix
    the generator and the batch stops containing the conflict, and the test fails for
    finding nothing rather than for the property being false.

    The property is real, so it is now CONSTRUCTED: take a credit the reference tier
    matched, move its amount far outside tolerance, and assert the engine stops assigning
    it. That works whatever the generator happens to emit.
    """
    from dataclasses import replace

    batch, inputs, _ = written
    out = match_once(inputs)

    by_ref = [a for a in out.assignments if a.tier == "tier1_reference"]
    assert by_ref, "batch contains no tier-1 reference match to corrupt"
    victim = by_ref[0]

    # Move the money far enough that no fee model or tolerance could explain it.
    corrupted = tuple(
        replace(t, credit=t.credit + 5_000_00) if t.id == victim.bank_txn_id else t
        for t in inputs.bank_txns
    )
    after = match_once(replace(inputs, bank_txns=corrupted))

    assert victim.bank_txn_id not in after.assignment_map, (
        f"{victim.bank_txn_id} still assigned after its amount was moved Rs 5,000 away "
        f"-- a matching reference overrode money that does not balance"
    )
    refusal = next(
        (r for r in after.refusals if r.bank_txn_id == victim.bank_txn_id), None
    )
    assert refusal is not None, "the credit vanished rather than being refused"
    assert refusal.category in {
        RefusalCategory.UNEXPLAINED_RESIDUAL,
        RefusalCategory.NO_SUBSET_FITS,
        RefusalCategory.POOL_EXCEEDED,
    }, f"unexpected refusal category {refusal.category}"


def test_refusals_carry_rupees_at_risk_and_an_actionable_reason(written):
    """
    An exception a human cannot act on is not an exception, it is a shrug.

    Every refusal must name what is at stake and why. Candidates are required only
    where candidates EXIST: a `no_subset_fits` refusal means the search ran to
    completion and nothing fit, and it stays actionable by reporting the closest miss
    instead -- "no subset comes within Rs 23,653" tells an investigator where to look,
    while an empty candidate list is simply the truth.

    The two search-bound refusals must also stay DISTINGUISHABLE in their prose, because
    they are different facts and a different next step. `pool_exceeded` means the engine
    declined to look; `no_subset_fits` means it looked exhaustively and the money is not
    accounted for. Collapsing them is how the largest exception in the batch came to
    tell a human there were "too many candidates to search" about a credit with one.
    """
    from recon.engine.results import RefusalCategory as RC

    _, inputs, _ = written
    for r in match_once(inputs).refusals:
        assert r.paise_at_risk > 0
        assert r.reason.strip()
        if r.category in {RC.MULTIPLE_CANDIDATES, RC.SOLUTION_CAP_REACHED}:
            assert len(r.candidates) >= 2, (
                f"{r.category.value} must show every candidate it could not choose between"
            )
        elif r.category is RC.NO_SUBSET_FITS:
            assert "no subset" in r.reason, (
                "a completed search that found nothing must say so, and say how close "
                f"it came: {r.reason!r}"
            )
            assert "MAX_POOL" not in r.reason, (
                "this refusal is not a bound being hit; saying so misleads an operator"
            )
        elif r.category is RC.POOL_EXCEEDED:
            assert "MAX_POOL" in r.reason, (
                f"a declined search must name the bound it declined at: {r.reason!r}"
            )


def test_ambiguity_case_is_never_assigned_by_tiers_1_and_2(written):
    """
    The centrepiece case must not be resolved by a lower tier. Its narration carries no
    payer name and a reference matching no payment, so tier 1 cannot fire; its credit
    equals no single payment's net, so tier 2 cannot either.
    """
    batch, inputs, _ = written
    out = match_once(inputs)
    assert batch.ambiguity_bank_txn_id not in out.assignment_map


# --------------------------------------------------------------------------
# The lookback — see DEFECT_LOG 2026-09-01-07
# --------------------------------------------------------------------------
def test_lookback_covers_the_window_plus_maximum_drift():
    """
    The lookback must span the settlement window AND the drift a credit can carry.
    Using the window width alone silently drops every T+2-drifted credit — 15 of 25
    unmatched one-to-one credits, before this was separated.
    """
    assert cfg.LOOKBACK_DAYS == cfg.SETTLEMENT_WINDOW_DAYS + cfg.MAX_SETTLEMENT_DRIFT_DAYS
    assert cfg.LOOKBACK_DAYS > cfg.SETTLEMENT_WINDOW_DAYS


def test_no_true_one_to_one_payment_falls_outside_the_lookback(written):
    """
    Every genuine one-to-one pairing must be reachable. A miss here is structural — the
    engine could not match it at any tolerance — rather than a scoring near-miss.
    """
    batch, inputs, _ = written
    by_id = {p.id: p for p in inputs.payments}
    txns = {t.id: t for t in inputs.bank_txns}
    from datetime import date

    unreachable = []
    for link in batch.truth:
        if link.relation != "one_to_one" or not link.bank_txn_id:
            continue
        txn = txns.get(link.bank_txn_id)
        p = by_id.get(link.payment_ids[0])
        if not txn or not p:
            continue
        lag = (date.fromisoformat(txn.txn_date) - tier2_amount_date.payment_date(p)).days
        if not 0 <= lag <= cfg.LOOKBACK_DAYS:
            unreachable.append((link.bank_txn_id, lag))
    assert not unreachable, f"one-to-one pairings outside the lookback: {unreachable[:5]}"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def test_scorecard_metrics_are_internally_consistent(written):
    """
    Every credit that had a candidate is either assigned or refused, and the four
    headline numbers must agree with their own numerators and denominators. A metrics
    block whose parts do not add up is worse than none.
    """
    from scorer.score import score

    batch, inputs, _ = written
    out = match_once(inputs)
    sc = score(
        out, batch.truth,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
        ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id,
    )
    assert sc.credits_with_candidates == sc.total_assignments + sc.total_refusals
    assert sc.correct_assignments + len(sc.wrong_assignments) == sc.total_assignments
    assert sc.correct_refusals + sc.conservative_refusals == sc.total_refusals
    assert sc.match_precision == pytest.approx(
        sc.correct_assignments / sc.total_assignments
    )
    assert 0.0 <= sc.match_rate <= 1.0
    assert 0.0 <= sc.refusal_rate <= 1.0


def test_refusing_everything_would_not_flatter_precision():
    """
    Precision alone is gameable: refuse everything and it is 1.0 over an empty set.
    The scorecard must expose that by reporting match rate alongside it -- a run with
    no assignments has an undefined-but-zero precision AND a zero match rate, and the
    pair is obviously bad where either alone is not.
    """
    from recon.engine.results import MatchOutput
    from scorer.score import score

    empty = MatchOutput((), (), (), (), {})
    sc = score(empty, (), total_payments=100, captured_payments=100)
    assert sc.match_rate == 0.0
    assert sc.match_precision == 0.0


def test_conservative_refusals_are_counted_separately_from_correct_ones(written):
    """
    A refusal ground truth wanted assigned is a MISS, not an ERROR -- no money moved.
    Collapsing the two would treat caution and error as equivalent.
    """
    from scorer.score import score

    batch, inputs, _ = written
    sc = score(
        match_once(inputs), batch.truth,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
    )
    assert sc.correct_refusals + sc.conservative_refusals == sc.total_refusals


def test_ambiguity_case_is_never_reported_as_assigned(written):
    """The one verdict that must never appear for the centrepiece case."""
    from scorer.score import score

    batch, inputs, _ = written
    sc = score(
        match_once(inputs), batch.truth,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
        ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id,
    )
    assert "WRONG" not in sc.ambiguity_case_verdict


def test_a_settlement_narration_count_blocks_a_single_payment_match(written):
    """
    The bank's narration states how many transactions a settlement covers. The engine
    has always PARSED that count and, until now, never used it -- so a credit covering
    two payments could be posted to one whenever a single payment's net happened to
    equal the batch total.

    Not hypothetical. At seed 11111, ppw=6, a netted refund came to exactly the second
    payment's net, so the batch total equalled the first payment alone. Tier 2 found an
    exact one-to-one fit at residual 0, assigned it, and never reached tier 3 -- where
    enumeration would have found both decompositions and Layer 2 would have refused.
    The tier ordering short-circuited the uniqueness test and produced a confident wrong
    answer at confidence 0.96.

    The count is an independent, label-free channel: it comes from the bank's text, not
    from the amounts, so it can contradict an arithmetic fit without being derived from
    one.
    """
    from dataclasses import replace

    from recon.engine.normalize import parse

    batch, inputs, _ = written
    out = match_once(inputs)

    single = next(
        a for a in out.assignments
        if len(a.payment_ids) == 1 and a.tier in {"tier1_reference", "tier2_amount_date"}
    )
    original = next(t for t in inputs.bank_txns if t.id == single.bank_txn_id)

    # Restate the same credit as a settlement batch covering three transactions.
    relabelled = replace(
        original,
        narration="RAZORPAY SETTLEMENT setl_TESTCOUNT0001 3 TXNS",
        ref_no=original.ref_no,
    )
    assert parse(relabelled.narration).txn_count == 3, "fixture narration did not parse"

    after = match_once(
        replace(inputs, bank_txns=tuple(
            relabelled if t.id == original.id else t for t in inputs.bank_txns
        ))
    )
    assert single.bank_txn_id not in after.assignment_map, (
        "a credit whose narration says 3 transactions was still posted to one payment"
    )
    refusal = next(r for r in after.refusals if r.bank_txn_id == single.bank_txn_id)
    assert refusal.category is RefusalCategory.NARRATION_COUNT_CONFLICT
    assert "3 transaction(s)" in refusal.reason
    assert refusal.candidates, "the refusal must name what it declined to post"


def test_a_matching_narration_count_still_assigns(written):
    """
    The guard must not refuse everything. A settlement saying "1 TXN" that fits one
    payment is exactly consistent and must still be posted.
    """
    from dataclasses import replace

    batch, inputs, _ = written
    out = match_once(inputs)
    single = next(
        a for a in out.assignments
        if len(a.payment_ids) == 1 and a.tier == "tier2_amount_date"
    )
    original = next(t for t in inputs.bank_txns if t.id == single.bank_txn_id)
    relabelled = replace(
        original, narration="RAZORPAY SETTLEMENT setl_TESTCOUNT0002 1 TXNS"
    )
    after = match_once(
        replace(inputs, bank_txns=tuple(
            relabelled if t.id == original.id else t for t in inputs.bank_txns
        ))
    )
    assert set(after.assignment_map.get(single.bank_txn_id, ())) == set(single.payment_ids)


# --------------------------------------------------------------------------
# The implied deduction rate
# --------------------------------------------------------------------------
def test_an_unexplained_residual_names_the_rate_that_would_reconcile_it(written):
    """
    A residual is a symptom; the rate it implies is a diagnosis.

    "credit 643537p vs expected 644715..644719p" says the numbers disagree and leaves an
    operator to work out why. "implies a 2.77% deduction, above the 1.8-2.5% this engine
    assumes" names the thing to look for.

    Checked against ground truth rather than asserted: every refusal this fires on at the
    reported seed carries the `bank_charge` label -- a receiving-bank fee taken on top of
    MDR, which is exactly what a rate above the band means.
    """
    import config as cfg

    batch, inputs, _ = written
    truth = {t.bank_txn_id: t for t in batch.truth if t.bank_txn_id}
    out = match_once(inputs)

    residuals = [r for r in out.refusals if r.category.value == "unexplained_residual"]
    if not residuals:
        pytest.skip("no unexplained_residual refusals at this seed")

    diagnosed = 0
    for r in residuals:
        if "implies a" not in r.reason:
            continue
        diagnosed += 1
        assert "deduction from gross" in r.reason
        assert f"{cfg.MDR_RATE_BAND[1]:.1%}" in r.reason
        link = truth.get(r.bank_txn_id)
        assert link and "bank_charge" in link.defect_labels, (
            f"{r.bank_txn_id}: the reason blames a deduction above the band, but ground "
            f"truth says {link.defect_labels if link else 'nothing'} -- the diagnosis "
            f"has to be right, not merely plausible"
        )
    assert diagnosed, "no refusal carried an implied-rate diagnosis"


def test_the_rate_note_is_silent_when_the_rate_is_inside_the_band():
    """
    Where the residual is not a rate problem, a note about rates points the wrong way.
    Saying nothing is the correct output, and a diagnosis that always fires is not one.
    """
    import config as cfg
    from recon.engine.tier1_reference import _implied_rate_note

    gross = 100_000
    mid = (cfg.MDR_RATE_BAND[0] + cfg.MDR_RATE_BAND[1]) / 2
    assert _implied_rate_note(int(gross * (1 - mid)), gross) == ""

    above = _implied_rate_note(int(gross * (1 - cfg.MDR_RATE_BAND[1] - 0.01)), gross)
    assert "above the" in above

    below = _implied_rate_note(int(gross * (1 - cfg.MDR_RATE_BAND[0] + 0.01)), gross)
    assert "below the" in below and "more arrived" in below

    assert _implied_rate_note(1000, 0) == "", "a zero gross must not divide"
