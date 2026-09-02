"""
The LLM on/off comparison, and the machinery that refuses to report it.

The withholding is the part worth testing hardest. A harness that always says "invalid"
is as useless as one that always says "valid" -- so both directions are pinned: the
stand-in is rejected, and a tier that is genuinely a live model is accepted.
"""

from __future__ import annotations

import pytest

from recon.llm.compare import (
    diff_verdicts, measure_parse_yield, tier_is_measurable,
)
from recon.llm.interface import ExceptionProse, NarrationFields
from recon.llm.null import NullTier
from recon.llm.recorded import RecordedTier


class FakeLiveTier:
    """Stands in for ClaudeTier: a name that is not a stand-in name, and enabled."""

    name = "claude:claude-sonnet-5"
    enabled = True

    def __init__(self, payer=None, ref=None):
        self._payer, self._ref = payer, ref

    def parse_narration(self, narration: str) -> NarrationFields:
        return NarrationFields(payer_name=self._payer, merchant_ref=self._ref,
                               model="claude-sonnet-5")

    def explain(self, category, reason, rupees_at_risk) -> ExceptionProse:
        return ExceptionProse(reason, "Review.", "claude-sonnet-5")


# --------------------------------------------------------------------------
# Validity: the measurement refuses to report what it cannot support
# --------------------------------------------------------------------------

def test_the_recorded_standin_is_not_measurable():
    valid, why = tier_is_measurable(RecordedTier())
    assert valid is False
    assert "stand-in" in why and "ANTHROPIC_API_KEY" in why


def test_a_disabled_tier_is_not_measurable():
    valid, why = tier_is_measurable(NullTier())
    assert valid is False
    assert "disabled" in why or "stand-in" in why


def test_a_live_tier_IS_measurable():
    """
    The other direction. Without this, "withheld" could just be the only answer the
    harness knows how to give, and the withholding would carry no information.
    """
    valid, why = tier_is_measurable(FakeLiveTier())
    assert valid is True
    assert why == ""


# --------------------------------------------------------------------------
# Parse yield
# --------------------------------------------------------------------------

def test_parse_yield_counts_only_credits(batch):
    y = measure_parse_yield(batch.inputs, RecordedTier())
    credits = sum(1 for t in batch.inputs.bank_txns if t.is_credit)
    assert y.narrations == credits
    assert y.unreadable_by_regex <= y.narrations


def test_settlement_batches_are_not_counted_as_unreadable(batch):
    """
    A settlement batch covers many payers, so having no single payer name is the CORRECT
    parse, not a gap. Counting it as unreadable would inflate the denominator with rows
    where the tier is deliberately never consulted -- and would make the fill rate look
    worse the better the parser got.
    """
    from recon.engine.normalize import needs_llm, parse

    settlement = [
        t for t in batch.inputs.bank_txns
        if t.is_credit and parse(t.narration).is_settlement_batch
    ]
    assert settlement, "fixture has no settlement batches; this test proves nothing"
    assert not any(needs_llm(parse(t.narration)) for t in settlement)


def test_a_tier_that_fills_nothing_yields_nothing(batch):
    y = measure_parse_yield(batch.inputs, FakeLiveTier(payer=None, ref=None))
    assert y.filled_by_llm == 0
    assert y.fill_rate == 0.0


def test_a_tier_that_fills_the_gap_is_counted(batch):
    """
    Filling a MERCHANT REF is what counts on this batch, and that is a finding rather
    than a fixture quirk: every narration `needs_llm` flags is missing a reference, not
    a payer name. The regex tier already reads a name off all 13 of them. So a model
    that only ever returned payer names would score a fill rate of zero here -- which is
    exactly the kind of thing the harness exists to make visible before anyone claims
    the tier "recovers 8 of 18 names".
    """
    y = measure_parse_yield(batch.inputs, FakeLiveTier(ref="INV-2026-9999"))
    assert y.unreadable_by_regex > 0, "fixture has no unreadable narrations"
    assert y.refs_recovered == y.unreadable_by_regex
    assert y.fill_rate == 1.0


def test_every_flagged_gap_on_this_batch_is_a_reference_gap_not_a_name_gap(batch):
    """
    Pins the composition above. If a future parser change makes `needs_llm` fire on
    missing NAMES instead, this fails and the claim in the test above stops being
    quietly wrong.
    """
    from recon.engine.normalize import needs_llm, parse

    flagged = [
        parse(t.narration) for t in batch.inputs.bank_txns
        if t.is_credit and needs_llm(parse(t.narration))
    ]
    assert flagged, "nothing was flagged; this test proves nothing"
    assert all(p.payer_name for p in flagged), (
        "some flagged narration is missing a payer name -- the composition changed"
    )
    assert all(not p.merchant_ref for p in flagged)


def test_a_tier_contradicting_the_regex_parser_is_counted_separately(batch):
    """
    Gap-filling is the tier's job; overriding is not. The count exists so that a tier
    that started overriding would be visible as a number rather than as a silent
    change in the matching input.
    """
    y = measure_parse_yield(batch.inputs, FakeLiveTier(payer="TOTALLY DIFFERENT NAME"))
    assert y.disagreed_with_regex >= 0


# --------------------------------------------------------------------------
# Verdict deltas
# --------------------------------------------------------------------------

def test_identical_runs_show_no_verdict_delta(batch):
    from recon.engine.match import match_once

    out = match_once(batch.inputs)
    assert diff_verdicts(out, out) == ()


def test_a_changed_assignment_is_reported_as_a_delta(batch):
    """
    The delta detector must actually detect. Constructed by dropping one assignment,
    because a comparison that cannot see a difference would report every run as null.
    """
    from dataclasses import replace
    from recon.engine.match import match_once

    out = match_once(batch.inputs)
    assert out.assignments, "fixture produced no assignments"
    trimmed = replace(out, assignments=out.assignments[1:])
    changes = diff_verdicts(trimmed, out)
    assert len(changes) == 1
    txn_id, off, on = changes[0]
    assert txn_id == out.assignments[0].bank_txn_id
    assert off.startswith("assign:") and on == "absent"


def test_the_offline_arms_differ_only_in_REASON_never_in_DECISION(batch):
    """
    The recorded tier and the disabled tier used to produce identical verdicts, and this
    test asserted that "so that if it ever STOPS holding, someone finds out".

    It stopped holding, and this is the finding. With `advance_payment` in the batch
    there are more narrations the regex tier cannot read, the stand-in recovers a
    merchant reference on some of them, and two credits change refusal category --
    `decomposition_out_of_bounds` becomes `unexplained_residual`, because the recovered
    reference lets tier 1 speak.

    What did NOT change is any decision. Same verdict, same money in the same place,
    same precision and match rate. So the assertion is now the sharper one: the tier may
    improve the sentence an operator reads and must not move a verdict, which is the
    trust boundary restated as a measurement rather than a promise.
    """
    from recon.engine.match import match_once
    from recon.llm.compare import split_changes

    on = match_once(batch.inputs, llm=RecordedTier())
    off = match_once(batch.inputs, llm=NullTier())
    outcome_changes, _reason_changes = split_changes(diff_verdicts(on, off))
    assert outcome_changes == (), (
        "the offline stand-in changed a DECISION, not just a reason: "
        f"{outcome_changes}"
    )
