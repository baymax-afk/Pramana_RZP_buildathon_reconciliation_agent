"""
The explainability engine.

Two obligations, and the first is much more important than the second.

**An explanation must not change the thing it explains.** `match_once` is a pure
function of `ReconInputs`, and MR1 -- the permutation gate that is this project's
headline verification claim -- is only meaningful because of that. If recording the
decision perturbed the decision, every reported number would describe a run nobody ever
inspected and every transcript would describe a run that never happened. So the first
test here hashes the assignment map and the refusal set with recording on and off and
requires them to be identical.

**An explanation must be derived from the run, not written about it.** The transcript is
the recorded computation. Nothing in `recon.explain` calls a model, and the tests below
pin the specific numbers through: a residual in paise, a Fellegi-Sunter weight, a pool
size. If the engine's arithmetic changes, the transcript changes with it or these fail.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import config as cfg
from recon.engine.match import match_once
from recon.explain import Explainer, Recorder


def _fingerprint(out) -> str:
    """Order-independent hash of everything the engine decided."""
    assignments = json.dumps(
        {k: sorted(v) for k, v in sorted(out.assignment_map.items())}, sort_keys=True
    )
    refusals = json.dumps(
        sorted((r.bank_txn_id, r.category.value, r.paise_at_risk) for r in out.refusals)
    )
    no_cand = json.dumps(sorted(out.no_candidate))
    return hashlib.sha256((assignments + refusals + no_cand).encode()).hexdigest()


# --------------------------------------------------------------------------
# The one that matters
# --------------------------------------------------------------------------
def test_recording_cannot_change_the_verdicts_it_records(batch):
    """
    THE load-bearing test of this module.

    Not "the numbers look similar" -- byte-identical over assignments, refusals with
    their categories and rupees at risk, and the no-candidate set. A transcript is only
    worth reading if it describes the run that actually shipped.
    """
    inputs = batch.inputs

    silent = match_once(inputs)
    recorder = Recorder()
    recorded = match_once(inputs, recorder=recorder)

    assert _fingerprint(silent) == _fingerprint(recorded)
    assert silent.tier_counts == recorded.tier_counts
    assert recorder.records, "recording produced no records at all"


def test_every_credit_gets_exactly_one_record(batch):
    """
    A credit refused in round 1 and assigned in round 2 must leave ONE record, describing
    the decision that stood. The fixpoint loop exists precisely because an early refusal
    can be made stale by a later claim, and a transcript showing both without saying
    which won would be worse than none.
    """
    inputs = batch.inputs
    recorder = Recorder()
    out = match_once(inputs, recorder=recorder)

    credits = {t.id for t in inputs.bank_txns if t.is_credit}
    assert set(recorder.records) == credits

    decided = (
        set(out.assignment_map)
        | {r.bank_txn_id for r in out.refusals}
        | set(out.no_candidate)
    )
    assert decided == credits, "a credit left the matcher with no verdict at all"

    for txn_id, rec in recorder.records.items():
        expected = (
            "assign" if txn_id in out.assignment_map
            else "refuse" if txn_id in {r.bank_txn_id for r in out.refusals}
            else "none"
        )
        assert rec.verdict == expected, (
            f"{txn_id}: transcript says {rec.verdict!r}, engine says {expected!r}"
        )


def test_the_recorded_payments_are_the_assigned_payments(batch):
    """The transcript must name the same payments the engine actually posted."""
    inputs = batch.inputs
    recorder = Recorder()
    out = match_once(inputs, recorder=recorder)

    for txn_id, payments in out.assignment_map.items():
        assert set(recorder.records[txn_id].final_payment_ids) == set(payments)


# --------------------------------------------------------------------------
# The three reading levels
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def explained(request):
    from recon.generator import build

    b = build.generate(seed=cfg.SEED_PRIMARY)
    recorder = Recorder()
    out = match_once(b.inputs, recorder=recorder)
    ex = Explainer(b.inputs)
    return b, out, recorder, {k: ex.explain(v) for k, v in recorder.records.items()}


def test_every_credit_has_a_plain_sentence(explained):
    """
    Level 1. A finance operator clearing an exception needs a sentence, not a category.

    The bar is deliberately mechanical -- a real sentence, no unexpanded identifiers, no
    template holes -- because "explainability" that ships an f-string with a `None` in it
    is worse than a bare reason code: it looks answered.
    """
    _, _, _, explanations = explained
    for txn_id, e in explanations.items():
        assert e.plain.endswith("."), f"{txn_id}: {e.plain!r}"
        assert len(e.plain.split()) >= 8, f"{txn_id} is not a sentence: {e.plain!r}"
        for hole in ("None", "{", "}", "  "):
            assert hole not in e.plain, f"{txn_id} has an unfilled hole: {e.plain!r}"
        assert "Rs " in e.plain, f"{txn_id} never says how much money: {e.plain!r}"


def test_every_explanation_links_to_evidence_that_exists(explained):
    """
    Level 2. A claim you cannot open is a claim you have to take on trust.

    Every reference must resolve to a row actually in the batch -- an explanation that
    linked to a payment id the engine invented would be the exact failure this project
    exists to argue against.
    """
    batch, _, _, explanations = explained
    payments = {p.id for p in batch.inputs.payments}
    invoices = {i.invoice_no for i in batch.inputs.invoices}
    txns = {t.id for t in batch.inputs.bank_txns}
    universe = {"payment": payments, "invoice": invoices, "bank_txn": txns}

    for txn_id, e in explanations.items():
        assert e.evidence, f"{txn_id} cites nothing at all"
        assert any(v.kind == "bank_txn" for v in e.evidence)
        for v in e.evidence:
            assert v.kind in universe, f"unknown evidence kind {v.kind!r}"
            assert v.id in universe[v.kind], f"{txn_id} cites missing {v.kind} {v.id}"
            assert v.label.strip() and v.label != v.id, (
                f"{txn_id} cites {v.id} with no human-readable label"
            )
            assert v.href == f"#/{v.kind}/{v.id}"


def test_an_assignment_transcript_shows_the_actual_arithmetic(explained):
    """
    Level 3. The auditor's level: the residual in paise, the interval it sat inside, and
    the tolerance it was judged against -- the numbers, not an assurance about them.
    """
    _, out, _, explanations = explained
    txn_id = next(iter(out.assignment_map))
    steps = explanations[txn_id].steps

    stages = [s.stage for s in steps]
    assert stages[0] == "input" and stages[-1] == "verdict"
    assert "pool" in stages
    assert any(s.startswith("tier:") for s in stages)

    tier_step = next(s for s in steps if s.stage.startswith("tier:") and s.evidence)
    assert "residual" in tier_step.detail
    assert f"{cfg.TOL_ABS_PAISE}-paise tolerance" in tier_step.detail
    assert "Rs " in tier_step.detail

    pool_step = next(s for s in steps if s.stage == "pool")
    assert f"{cfg.LOOKBACK_DAYS}-day lookback" in pool_step.detail


def test_a_refusal_transcript_names_the_layer_that_objected(explained):
    """
    A refusal that says only "refused" is a shrug. Each must name the mechanism.

    This is the difference between an exception queue a controller can work and one they
    escalate wholesale.
    """
    _, out, _, explanations = explained
    for r in out.refusals:
        e = explanations[r.bank_txn_id]
        assert e.verdict == "refuse"
        verdict_step = e.steps[-1]
        assert verdict_step.stage == "verdict"
        assert "REFUSED" in verdict_step.headline
        assert e.plain.startswith("Not posted"), e.plain


def test_the_fellegi_sunter_step_shows_its_working(explained):
    """
    Layer 3 is the least intuitive of the four, so its transcript carries the most:
    per-field weights in bits, the prior the blocking pool implies, both thresholds, and
    a total. A bare "-6.72" explains nothing to the person who has to act on it.
    """
    _, out, _, explanations = explained
    conflicts = [
        r for r in out.refusals if r.category.value == "amount_name_conflict"
    ]
    if not conflicts:
        pytest.skip("no amount/name conflict at this seed")

    steps = explanations[conflicts[0].bank_txn_id].steps
    fs = next(s for s in steps if s.stage == "layer3")

    assert "CONTRADICTS" in fs.headline
    assert "TOTAL:" in fs.detail, "the total weight must not be swallowed"
    assert "bits" in fs.detail
    assert str(cfg.FS_THRESHOLD_LOWER) in fs.detail
    assert str(cfg.FS_THRESHOLD_UPPER) in fs.detail
    assert "prior from the blocking pool" in fs.detail


def test_explanations_serialise_for_the_api(explained):
    """The whole structure has to survive a JSON round trip to reach the UI."""
    _, out, _, explanations = explained
    payload = {k: e.as_dict() for k, e in explanations.items()}
    round_tripped = json.loads(json.dumps(payload))

    sample = round_tripped[next(iter(out.assignment_map))]
    assert set(sample) == {"bank_txn_id", "verdict", "plain", "evidence", "transcript"}
    assert sample["transcript"][0]["seq"] == 1
    assert all("href" in v for v in sample["evidence"])


def test_no_model_is_consulted_to_build_an_explanation(explained):
    """
    The transcript is the recorded computation, not a description written afterwards.

    `recon.llm` may rewrite an exception's prose, but it never sources a fact -- so an
    explanation cannot drift from the decision it describes, because there is no second
    inference that could disagree. Enforced by import, which is crude and exactly right:
    a future edit that reached for a model here would fail this.
    """
    import ast
    import inspect

    from recon.explain import render, trace

    # Parsed, not grepped. A text scan trips over the modules' own prose explaining WHY
    # they do not call a model -- which is exactly the documentation worth keeping, so
    # the test has to be precise rather than the docs vague.
    for module in (render, trace):
        tree = ast.parse(inspect.getsource(module))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        forbidden = [
            m for m in imported
            if "llm" in m.lower() or "anthropic" in m.lower()
        ]
        assert not forbidden, (
            f"{module.__name__} imports {forbidden}. The transcript is the recorded "
            f"computation; sourcing any part of it from a model would mean the "
            f"explanation could disagree with the decision."
        )


# --------------------------------------------------------------------------
# The narration step, and the two ways it said nothing
#
# The transcript's `parse` step carried the constant headline "Read the narration." --
# which is verbatim the UI's own label for that step (`STAGE_LABEL.parse` in App.jsx),
# so the row rendered as "READ THE NARRATION -- Read the narration." Every other step's
# headline carries its finding: the bank line names the amount and the date, the pool
# names how many candidates survived, a tier names what it concluded. This one restated
# its title and put the only real content in the detail line underneath.
#
# Underneath it was a second, quieter fault. The record's `parsed_by_llm` was assigned
# from `getattr(parsed, "llm_model", "")`, and `ParsedNarration` has no `llm_model`
# attribute -- so the getattr default swallowed the mistake and the field was ALWAYS
# False, for as long as it existed. Nothing rendered it, which is why nobody noticed.
# In a project that reports which tier produced what everywhere else, the step that
# reads the narration could not say whether a model had read it.
# --------------------------------------------------------------------------
def _parse_step(explanation) -> dict:
    step = next(s for s in explanation.as_dict()["transcript"] if s["stage"] == "parse")
    return step


def test_the_narration_step_does_not_restate_its_own_title(explained):
    """
    The failure was invisible to every other test because the string was well-formed
    English that happened to carry no information. Pinned against the UI's label rather
    than against a literal, so renaming the step on either side cannot re-open it.
    """
    from pathlib import Path
    import re

    jsx = (Path(__file__).resolve().parents[1] / "ui" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    )
    label = re.search(r"^\s*parse:\s*\"([^\"]+)\"", jsx, re.M)
    assert label, "the UI no longer labels the parse stage; update this test with it"
    label = label.group(1).rstrip(".").casefold()

    _, _, _, explanations = explained
    for explanation in explanations.values():
        headline = _parse_step(explanation)["headline"].rstrip(".").casefold()
        assert headline != label, (
            f"the narration step's headline is its own title again ({headline!r}); a "
            f"row that reads 'READ THE NARRATION -- Read the narration' has told the "
            f"reader nothing"
        )


def test_the_narration_step_says_what_it_read(explained):
    _, _, _, explanations = explained
    for explanation in explanations.values():
        step = _parse_step(explanation)
        headline, detail = step["headline"], step["detail"]
        if headline.startswith("Nothing usable"):
            assert "could be read" in detail
            continue
        # "Read n of 3 fields: ..." -- and the count has to match the fields it lists.
        m = __import__("re").match(r"Read (\d) of 3 fields: (.+)\.$", headline)
        assert m, f"unexpected narration headline: {headline!r}"
        n, listed = int(m.group(1)), m.group(2)
        assert 1 <= n <= 3
        assert len(listed.replace(" and ", ", ").split(", ")) == n, (
            f"the headline claims {n} fields and names a different number: {headline!r}"
        )
        assert detail.startswith("Extracted "), detail


def test_the_narration_step_names_which_fields_were_missing(explained):
    """
    Which field is ABSENT is usually the more useful half: a settlement batch carrying
    no merchant reference is the shape that falls through to the amount tiers, and a
    reader working out why should not have to infer it from a phrase not being there.
    """
    fields = ("payer name", "merchant reference", "transaction count")
    _, _, _, explanations = explained
    for explanation in explanations.values():
        step = _parse_step(explanation)
        named = {f for f in fields if f in step["headline"]}
        for field in fields:
            if field in named:
                continue
            assert field in step["detail"], (
                f"{field!r} was neither read nor reported as unread: {step['detail']!r}"
            )


def test_the_narration_step_says_who_read_it(explained):
    _, _, _, explanations = explained
    for explanation in explanations.values():
        detail = _parse_step(explanation)["detail"]
        assert "pattern rules" in detail, (
            "the step does not say which tier read the narration. This project reports "
            "tier provenance everywhere else, and the LLM tier fills these exact fields"
        )


def test_the_recorder_reads_only_fields_the_parser_actually_has():
    """
    The general form of the `llm_model` bug, and the reason it survived.

    `getattr(parsed, "...", default)` on a misspelled or removed attribute returns the
    default forever and raises nothing. Any field the recorder pulls off a
    `ParsedNarration` must exist on the dataclass, so a rename breaks a test instead of
    quietly zeroing a column of the transcript.
    """
    import ast
    import inspect

    from recon.engine import match as match_mod
    from recon.engine.normalize import ParsedNarration

    real = set(ParsedNarration.__dataclass_fields__) | {
        name for name in dir(ParsedNarration) if not name.startswith("_")
    }
    tree = ast.parse(inspect.getsource(match_mod))
    for node in ast.walk(tree):
        # `parsed.<attr>`
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "parsed"
        ):
            assert node.attr in real, (
                f"match.py reads parsed.{node.attr}, which ParsedNarration does not have"
            )
        # `getattr(parsed, "<attr>", ...)` -- the shape that hid this for months
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "parsed"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
        ):
            assert node.args[1].value in real, (
                f"match.py getattrs parsed.{node.args[1].value!r} with a default, and "
                f"ParsedNarration has no such attribute -- the default will be returned "
                f"forever and nothing will raise"
            )
