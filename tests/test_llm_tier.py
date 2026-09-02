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
    # The condition is NOT "some field was left empty". When the rail format is
    # unrecognised the regex tier's field boundaries are guesses: it may have extracted
    # something, but it cannot know whether it split the string correctly.
    # 'CR-SILVERLINEPACK-836870 INV-2026-1022' yields a name and a reference that happen
    # to be right; 'ACME INDUSTRIAL SU PAYMENT AGAINST BILLS' yields a "name" with three
    # noise words inside it. Both are unrecognised formats and both are legitimately
    # worth a second look.
    #
    # What must hold is the converse: a narration parsed under a KNOWN format is never
    # offered to a model, and neither is a settlement batch.
    for narration in offered:
        assert parse(narration).style not in {
            "neft", "rtgs", "imps", "upi", "settlement"
        }


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


def test_the_llm_tier_may_change_an_outcome_but_never_decides_one():
    """
    The trust boundary, stated precisely -- and it is NOT "the LLM changes no verdict".

    This test used to assert `on.assignment_map == off.assignment_map` and said in its
    own docstring that a change would be "a trust-boundary event". It changed, and it is
    not one. The tier recovered a merchant reference from a narration the regex tier
    could not read; that reference gave tier 1 something to match on; the DETERMINISTIC
    engine then reached a different and correct conclusion from better evidence.

    Conflating "the LLM must not decide a match" with "the LLM must not change any
    outcome" makes the weaker claim untestable and the stronger one unfalsifiable. The
    tier exists to fill narration fields. If filling them never changed anything, it
    would have no reason to exist -- and the boundary would be enforced by the tier
    being useless rather than by the type system.

    So what is asserted is what the boundary actually promises:

      1. the tier never OVERRIDES a field the regex tier already read;
      2. any outcome it changes is changed for the better, never for the worse.
    """
    inputs = load_inputs()
    off = match_once(inputs, llm=NullTier())
    on = match_once(inputs, llm=RecordedTier())

    # (1) Structural: every payer name the regex tier read survives the merge intact.
    from recon.engine.normalize import needs_llm, parse, parse_with_llm

    tier = RecordedTier()
    for t in inputs.bank_txns:
        if not t.is_credit:
            continue
        base = parse(t.narration)
        if not needs_llm(base) or not base.payer_name:
            continue
        merged = parse_with_llm(t.narration, tier)
        assert merged.payer_name == base.payer_name, (
            f"the LLM tier overrode a regex-parsed payer name on {t.id}: "
            f"{base.payer_name!r} -> {merged.payer_name!r}"
        )

    # (2) Evidence only ever gets stronger: tier 1 matches cannot decrease, and the
    #     tier must not cost an assignment the deterministic engine had without it.
    assert on.tier_counts.get("tier1_reference", 0) >= off.tier_counts.get(
        "tier1_reference", 0
    )
    assert len(on.assignments) >= len(off.assignments)


def test_any_outcome_the_llm_tier_changes_is_a_correct_one():
    """
    The half of the boundary that matters for money. The tier may turn a refusal into an
    assignment -- it did, on this batch, by recovering a reference that outweighed a
    third-party payer's name disagreement -- but every assignment it enables must be
    right. A tier that bought coverage with precision would be crossing the boundary in
    the only way that costs anything.
    """
    import config as cfg
    from scorer.score import load_truth, score

    truth_path = cfg.TRUTH_DIR / "ground_truth.json"
    if not truth_path.exists():
        import pytest

        pytest.skip("no ground truth on disk")

    inputs = load_inputs()
    raw, links = load_truth(truth_path)
    scores = {}
    for label, llm in (("off", NullTier()), ("on", RecordedTier())):
        out = match_once(inputs, llm=llm)
        scores[label] = score(
            out, links, total_payments=len(inputs.payments),
            captured_payments=sum(1 for p in inputs.payments if p.captured),
            ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
            credits_by_id={t.id: t.credit for t in inputs.bank_txns},
            seed=inputs.seed,
        )
    assert scores["on"].match_precision >= scores["off"].match_precision
    assert scores["on"].correct_assignments >= scores["off"].correct_assignments
    assert scores["on"].wrong_assignments == ()


def test_select_reports_which_tier_ran():
    """A recorded run must never be mistakable for a live-model run."""
    assert select(disabled=True).name == "disabled"
    assert select().name in {"recorded", "claude:claude-sonnet-5"}


def test_every_refusal_category_has_an_explanation_template():
    """
    THE test that stops this drifting again.

    The template table used to key on "fs_contradicted", a category the engine has never
    emitted, so every amount/name conflict fell through to the generic fallback. Nothing
    caught it because the fallback is a plausible sentence -- the operator saw the
    engine's internal reason string instead of prose written for them, which reads as
    terse rather than as broken.

    Coupling the table to the enum makes the next divergence a test failure instead of a
    silent downgrade.
    """
    from recon.engine.results import RefusalCategory
    from recon.llm.recorded import _TEMPLATES

    missing = sorted(c.value for c in RefusalCategory if c.value not in _TEMPLATES)
    assert not missing, f"RefusalCategory members with no explanation template: {missing}"

    stale = sorted(k for k in _TEMPLATES if k not in {c.value for c in RefusalCategory})
    assert not stale, f"templates keyed on categories the engine never emits: {stale}"


def test_explanations_are_operator_facing_not_engine_internals():
    """Each template must actually be used, and must not echo the internal reason."""
    from recon.engine.results import RefusalCategory
    from recon.llm.recorded import RecordedTier

    tier = RecordedTier()
    internal = "fs weight -3.20 below FS_THRESHOLD_LOWER=4.0"
    for category in RefusalCategory:
        prose = tier.explain(category.value, internal, 1234.5)
        assert internal not in prose.explanation, (
            f"{category.value} fell through to the generic fallback"
        )
        assert "1,234.50" in prose.explanation
        assert prose.proposed_resolution
