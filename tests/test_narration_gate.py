"""
What the regex tier does with a grammar it does not recognise.

Two separate questions, and conflating them cost this project a falsified claim and
nearly cost it a fake metric.

**1. Should the narration go to the model?** It used to depend on whether a REFERENCE was
found — `style == "unknown" and not reference`. Finding a UTR is not evidence the
narration was understood; those are different fields answering different questions. The
holdout reformats 18 narrations specifically to stress this path and every one of them
carries a reference, so **all 18 were withheld from the model**, and the "harder" batch
reported a *lower* `needs_llm` rate than the reported one (4.7% vs 9.2%). The artefact
built to measure whether a model generalises routed around the model by construction.

**2. Should the extracted name be trusted?** Here the obvious answer is wrong, and
`test_a_clean_name_in_an_unrecognised_grammar_is_kept` is the guard that says so.
"""

from __future__ import annotations

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine.normalize import needs_llm, parse


# ---- the gate -------------------------------------------------------------
def test_a_reference_is_not_evidence_the_narration_was_read():
    """An unknown grammar goes to the model whether or not a UTR happened to match."""
    with_ref = parse("*HDFCN00458156263* ACME RETAIL - RECD")
    assert with_ref.style == "unknown" and with_ref.reference
    assert needs_llm(with_ref), (
        "a narration this tier cannot read was withheld from the model because a "
        "reference regex happened to match inside it"
    )


def test_a_recognised_grammar_carrying_a_name_does_not_need_the_model():
    p = parse("NEFT-AXISP123456789012-ACME TEXTILES-PAYMENT")
    assert p.style == "neft" and p.payer_name and not needs_llm(p)


def test_a_settlement_batch_is_fully_parsed_and_never_offered_a_name_to_invent():
    p = parse("RAZORPAY SETTLEMENT setl_ABCDEFGH1234 12 TXNS")
    assert p.payer_name is None and not needs_llm(p), (
        "a settlement covers many payers; the absence of a name is the correct answer "
        "and must not be handed to a model to hallucinate one into"
    )


def test_the_holdout_stresses_the_model_tier_harder_than_the_reported_batch():
    """
    The Phase C property that was asserted in prose and never checked.

    `holdout.py` says its first stress is "narration formats the regex tier has never
    seen". If the shifted batch does not put MORE narrations in front of the model than
    the reported one, that stress is not being applied and the LLM tier's generalisation
    stays unmeasured no matter what else the holdout does.
    """
    if not (cfg.HOLDOUT / "manifest.json").is_file():
        pytest.skip("no holdout; run `python run.py holdout`")

    def rate(generated_dir):
        inputs = load_inputs(generated_dir=generated_dir)
        creds = [t for t in inputs.bank_txns if t.is_credit]
        return sum(1 for t in creds if needs_llm(parse(t.narration))) / len(creds)

    primary, holdout = rate(None), rate(cfg.HOLDOUT)
    assert holdout > primary, (
        f"the shifted holdout puts {holdout:.1%} of narrations in front of the model and "
        f"the reported batch puts {primary:.1%}. The holdout is not stressing the tier it "
        f"was built to stress."
    )


# ---- what may be trusted onto the name channel ----------------------------
def test_a_name_carrying_its_own_reference_is_withheld_and_kept_visible():
    p = parse("*HDFCN00458156263* ACME RETAIL - RECD")
    assert p.payer_name is None, "a name with a UTR inside it reached the name channel"
    assert not p.name_confident
    assert p.withheld_name and "HDFCN00458156263" in p.withheld_name, (
        "the withheld string must be kept so the explain transcript can show what was "
        "read and why it was not used -- silence and suppression are different facts"
    )


def test_a_clean_name_in_an_unrecognised_grammar_is_KEPT():
    """
    **The guard against the fix this nearly shipped.**

    The first version of the rule was "an unrecognised grammar yields no name". It
    measured +1.03pp coverage on the reported batch and +3.60pp on the holdout, at
    precision 1.0000 with zero wrong assignments — and it was wrong. The two credits it
    gained were these:

        'BY CLG/666792/VERTEX ENGINEERIN'               -> 'VERTEX ENGINEERIN'
        'INW REM 275492 ACME INDUSTRIAL SU INV20261143' -> 'ACME INDUSTRIAL SU'

    Both are correct extractions — real payer names, truncated by the bank's field width,
    in narrations this module simply has no style rule for. Both are `third_party_payer`
    cases. Discarding them does not fix a parse; it blinds the name channel so it cannot
    object, and the credit posts on the amount alone. A metric that improves because the
    engine stopped looking is not an improvement.
    """
    for narration, expected in (
        ("BY CLG/666792/VERTEX ENGINEERIN", "VERTEX ENGINEERIN"),
        ("INW REM 275492 ACME INDUSTRIAL SU INV20261143", "ACME INDUSTRIAL SU"),
    ):
        p = parse(narration)
        assert p.style == "unknown", "this test is about UNRECOGNISED grammars"
        assert p.payer_name == expected, (
            f"{narration!r} yielded {p.payer_name!r}. This name is clean and must reach "
            f"the name channel; withholding it buys coverage by holding less evidence."
        )
        assert p.name_confident and p.withheld_name is None


def test_withholding_is_never_the_source_of_a_coverage_gain():
    """
    The property that makes the narrow rule honest, asserted rather than argued.

    Every name this tier withholds must be one it can PROVE is contaminated. If a batch
    ever reports a withheld name that is clean, the rule has widened into the version
    rejected above, and the coverage number stops meaning what it says.
    """
    import re

    for generated_dir in (None, cfg.HOLDOUT):
        if generated_dir is not None and not (generated_dir / "manifest.json").is_file():
            continue
        inputs = load_inputs(generated_dir=generated_dir)
        for txn in inputs.bank_txns:
            if not txn.is_credit:
                continue
            p = parse(txn.narration)
            if not p.withheld_name:
                continue
            contaminated = (
                p.reference and p.reference.upper() in p.withheld_name.upper()
            ) or re.search(r"\d{6,}", p.withheld_name)
            assert contaminated, (
                f"{txn.id} withheld {p.withheld_name!r}, which carries neither the "
                f"reference nor an identifier-shaped digit run. Withholding a clean name "
                f"is how a coverage number stops being earned."
            )
