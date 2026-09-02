"""
The LLM tier, and the trust boundary it is not allowed to cross.

The single most important test here is `test_llm_output_type_cannot_express_a_match`.
Everything else checks behaviour; that one checks that the *architecture* forbids the
failure rather than merely discouraging it.
"""

from __future__ import annotations

from dataclasses import fields as dc_fields

import config as cfg
from loaders import load_inputs
from recon.engine.match import match_once
from recon.engine.normalize import needs_llm, parse, parse_with_llm
from recon.llm import select
from recon.llm.interface import ExceptionProse, LLMTier, NarrationFields
from recon.llm.null import NullTier
from recon.llm.recorded import RecordedTier


def test_llm_output_type_cannot_express_a_match():
    """
    THE structural guarantee. `NarrationFields` has no field for a payment id, a
    candidate, a score or a verdict -- so an LLM cannot nominate or endorse a match even
    if a prompt asked it to and it complied. The trust boundary is a property of the
    type, not a rule someone has to remember.
    """
    names = {f.name for f in dc_fields(NarrationFields)}
    assert names == {"payer_name", "merchant_ref", "model", "note"}
    for banned in ("payment_id", "payment_ids", "candidate", "score", "confidence",
                   "verdict", "match", "assignment"):
        assert banned not in names


def test_exception_prose_cannot_express_a_verdict():
    """Prose explains a decision already made; it must not carry one."""
    names = {f.name for f in dc_fields(ExceptionProse)}
    assert names == {"explanation", "proposed_resolution", "model"}


def test_every_implementation_satisfies_the_same_protocol():
    for tier in (NullTier(), RecordedTier()):
        assert isinstance(tier, LLMTier)


def test_engine_runs_to_completion_with_the_tier_disabled():
    """`--no-llm` must be a real path, not a branch that skips calls."""
    inputs = load_inputs()
    out = match_once(inputs, llm=NullTier())
    assert out.assignments
    assert len(out.assignments) + len(out.refusals) + len(out.no_candidate) == len(
        [t for t in inputs.bank_txns if t.is_credit]
    )


def test_disabled_tier_extracts_nothing():
    f = NullTier().parse_narration("CMS/LOTUSPAPERMILLS/INV/2026/1010/CR")
    assert f.is_empty


def test_llm_is_offered_only_narrations_regex_could_not_read():
    """
    Everything the deterministic tier handles must never reach a model. That is what
    makes the on/off comparison meaningful rather than two labels on one number.
    """
    inputs = load_inputs()
    offered = [
        t.narration for t in inputs.bank_txns
        if t.is_credit and needs_llm(parse(t.narration))
    ]
    assert offered, "no unparseable narrations in the batch; the comparison is vacuous"
    for narration in offered:
        assert parse(narration).payer_name is None or parse(narration).merchant_ref is None


def test_settlement_batches_are_never_sent_to_the_llm():
    """
    A settlement batch covers many payers, so having no single payer name is the
    CORRECT parse. Sending it to a model invites a hallucinated counterparty, and the
    ambiguity case depends on that name channel staying empty.
    """
    p = parse("RAZORPAY SETTLEMENT setl_ABCDEFGH1234 7 TXNS")
    assert p.payer_name is None
    assert not needs_llm(p)


def test_llm_never_overwrites_a_field_regex_already_extracted():
    """The model fills gaps. It does not get to revise deterministic output."""
    narration = "NEFT-PUNBR67363667630-SUNRISE TEXTILES L-INV-2026-1001-CR"
    base = parse(narration)
    withllm = parse_with_llm(narration, RecordedTier())
    assert withllm.merchant_ref == base.merchant_ref
    assert withllm.payer_name == base.payer_name


def test_recorded_tier_recovers_references_from_messy_narrations():
    f = RecordedTier().parse_narration("CMS/LOTUSPAPERMILLS/INV/2026/1010/CR")
    assert f.merchant_ref == "INV-2026-1010"
    assert f.payer_name


def test_payer_name_is_never_the_whole_narration():
    """
    Regression guard for DEFECT_LOG 2026-09-02-02. A garbage name is worse than no name:
    absence contributes zero Fellegi-Sunter weight, but nonsense scores an active
    DISAGREEMENT and refuses matches that were correct.
    """
    for narration in (
        "ACME INDUSTRIAL SU FUND TRF 398693 INV20261003",
        "INW REM 445512 KAVERI AGRO EXPORT INV20261044",
    ):
        name = parse(narration).payer_name or ""
        assert name != narration
        assert not any(ch.isdigit() for ch in name), f"digits leaked into name: {name!r}"
        assert len(name) < len(narration)


def test_llm_does_not_change_precision_on_this_batch():
    """
    The measured Block 9 result, pinned so a later change cannot quietly alter it.

    The tier upgrades 9 matches from tier 2 to tier 1 -- stronger evidence -- but
    changes no verdict. If a future change makes the LLM start altering outcomes, that
    is a trust-boundary event and this test should fail loudly.
    """
    inputs = load_inputs()
    off = match_once(inputs, llm=NullTier())
    on = match_once(inputs, llm=RecordedTier())
    assert on.assignment_map == off.assignment_map
    assert on.tier_counts.get("tier1_reference", 0) >= off.tier_counts.get("tier1_reference", 0)


def test_select_reports_which_tier_ran():
    """A recorded run must never be mistakable for a live-model run."""
    assert select(disabled=True).name == "disabled"
    assert select().name in {"recorded", "claude:claude-sonnet-5"}
