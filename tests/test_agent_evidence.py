"""
B4: the evidence channel, and the proof that having it changes nothing until it is used.

This is the foundation the whole agentic design sits on, and it earns that place by
being a no-op. `match_once` is a pure function of `ReconInputs`; MR1 -- the permutation
gate that is this project's headline verification claim -- is only meaningful because of
that, and every reported number is produced with `evidence=None`. So the first thing to
establish is that the parameter's existence cannot move a verdict.

The second thing is that when evidence IS supplied it enters as one named
Fellegi-Sunter field and the existing two-threshold rule decides. It does not override a
threshold, skip a tier, or nominate a payment. An agent's only lever is to widen the
evidence set and let the deterministic engine reach its own conclusion again.
"""

from __future__ import annotations

import hashlib
import json

import config as cfg
from recon.engine import fellegi_sunter as fs
from recon.engine.match import match_once


def _fingerprint(out) -> str:
    a = json.dumps(
        {k: sorted(v) for k, v in sorted(out.assignment_map.items())}, sort_keys=True
    )
    r = json.dumps(
        sorted((x.bank_txn_id, x.category.value, x.paise_at_risk) for x in out.refusals)
    )
    n = json.dumps(sorted(out.no_candidate))
    return hashlib.sha256((a + r + n).encode()).hexdigest()


# --------------------------------------------------------------------------
# The no-op
# --------------------------------------------------------------------------
def test_the_evidence_parameter_is_inert_when_unused(batch):
    """
    Four spellings of "no evidence" must all reproduce the baseline byte for byte.

    `{}` and `None` are the ones a caller writes. The fourth -- evidence keyed on a
    transaction that is not in the batch -- is the one that catches a lookup written
    against the wrong key, which would otherwise fail silently by applying to nothing.
    """
    inputs = batch.inputs
    baseline = _fingerprint(match_once(inputs))

    for label, ev in (
        ("None", None),
        ("empty dict", {}),
        ("unknown txn", {"bank_txn_99999": {"authorised_payer_for": "Nobody Ltd"}}),
        ("empty inner", {"bank_txn_0001": {}}),
    ):
        assert _fingerprint(match_once(inputs, evidence=ev)) == baseline, (
            f"evidence={label} changed the run"
        )


def test_an_absent_assertion_adds_no_field_at_all(batch):
    """
    Structural, not just arithmetic. A `None`-level field would weigh zero and leave
    `all(level is None)` intact -- so the verdict would be safe either way -- but it
    would put a row saying nothing into every explanation transcript and make the
    no-evidence case harder to read. Omitting it keeps the default path identical in
    shape as well as in value.
    """
    inputs = batch.inputs
    txn = next(t for t in inputs.bank_txns if t.is_credit)
    payments = [p for p in inputs.payments if p.captured][:1]
    u = fs.estimate_u(inputs.payments, inputs.bank_txns)
    from recon.engine.normalize import parse

    parsed = parse(txn.narration)

    without = fs.evidence_for(txn, parsed, payments, u, pool_size=5)
    assert {f.field for f in without.fields} == {"name", "reference"}

    with_it = fs.evidence_for(
        txn, parsed, payments, u, pool_size=5, authorised_payer_for="Some Customer Ltd"
    )
    assert {f.field for f in with_it.fields} == {
        "name", "reference", "authorised_payer",
    }


# --------------------------------------------------------------------------
# What the field does when it fires
# --------------------------------------------------------------------------
def test_a_register_hit_explains_a_name_mismatch_rather_than_overriding_it(batch):
    """
    The name still DISAGREES after the register fires -- that is recorded honestly. What
    changes is that the field evidence no longer nets negative, because a second
    independent channel says the disagreement was expected.

    That is the whole mechanism, and it is why `contradicts` was written to require the
    field weight to net negative rather than merely to contain a disagreement.
    """
    inputs = batch.inputs
    base = match_once(inputs)
    conflicts = [
        r for r in base.refusals if r.category.value == "amount_name_conflict"
    ]
    if not conflicts:
        import pytest

        pytest.skip("no amount/name conflict at this seed")

    by_pid = {p.id: p for p in inputs.payments}
    target = conflicts[0]
    pid = target.candidates[0].payment_ids[0]
    customer = by_pid[pid].notes.get("customer_name")

    enriched = match_once(
        inputs, evidence={target.bank_txn_id: {"authorised_payer_for": customer}}
    )

    assert target.bank_txn_id in enriched.assignment_map, (
        "a register hit that explains the mismatch should let the match proceed"
    )
    assert set(enriched.assignment_map[target.bank_txn_id]) == set(
        target.candidates[0].payment_ids
    ), "it must post the SAME payments the amount channel had already identified"


def test_an_assertion_about_a_different_customer_is_not_counted_either_way(batch):
    """
    A register entry naming somebody else is evidence about a different pair. Scoring it
    as DISAGREEMENT would let an agent damage a match by supplying true but irrelevant
    facts -- a way for evidence-gathering to make the engine worse, which would be a
    strange property for an evidence channel to have.
    """
    inputs = batch.inputs
    baseline = _fingerprint(match_once(inputs))
    base = match_once(inputs)
    conflicts = [
        r for r in base.refusals if r.category.value == "amount_name_conflict"
    ]
    if not conflicts:
        import pytest

        pytest.skip("no amount/name conflict at this seed")

    irrelevant = {
        conflicts[0].bank_txn_id: {
            "authorised_payer_for": "A Company That Owes Us Nothing Pvt Ltd"
        }
    }
    assert _fingerprint(match_once(inputs, evidence=irrelevant)) == baseline


def test_evidence_cannot_rescue_a_refusal_that_is_not_about_names(batch):
    """
    The field enters Layer 3 only. A credit no subset of payments accounts for is
    refused by conservation, upstream of any name evidence, and no amount of
    authorised-payer testimony may post it. This is the containment that makes the
    channel safe to hand to an agent.
    """
    inputs = batch.inputs
    base = match_once(inputs)
    unfit = [r for r in base.refusals if r.category.value == "no_subset_fits"]
    if not unfit:
        import pytest

        pytest.skip("no arithmetic refusals at this seed")

    ev = {
        r.bank_txn_id: {"authorised_payer_for": "Anyone At All Ltd"} for r in unfit
    }
    enriched = match_once(inputs, evidence=ev)
    for r in unfit:
        assert r.bank_txn_id not in enriched.assignment_map, (
            f"{r.bank_txn_id} was refused on the amounts and must stay refused"
        )


def test_the_weight_is_derived_from_the_disclosed_priors_not_hardcoded():
    """
    The number that does the work must be traceable to `config.py`, where it is frozen
    with its derivation, rather than appearing as a literal somewhere in the engine.
    """
    import math

    expected = math.log2(
        cfg.FS_M_PRIORS["authorised_payer"] / cfg.FS_U_AUTHORISED_PAYER
    )
    got = fs._field_weight(
        fs.Level.EXACT,
        cfg.FS_M_PRIORS["authorised_payer"],
        cfg.FS_U_AUTHORISED_PAYER,
    )
    assert abs(got - expected) < 1e-9
    assert got > 3.26, (
        "the register must be able to outweigh an exact name disagreement, which is "
        "what it exists to explain"
    )


# --------------------------------------------------------------------------
# Side D is reference data, and the engine must not read it
# --------------------------------------------------------------------------
def test_the_register_is_partial_and_carries_decoys(batch):
    """
    A register naming every relationship would make the defect class a lookup, and an
    agent that closes 100% of cases because the answer sat in a file demonstrates
    nothing. Decoys mean a hit is not self-evidently a match.
    """
    stats = batch.stats["payer_directory"]
    assert stats["relationships_created"] > 0
    assert stats["on_the_register"] < stats["relationships_created"], (
        "full coverage would turn investigation into a lookup"
    )
    assert stats["decoys"] == cfg.PAYER_DIRECTORY_DECOYS

    names = {r.payer_name for r in batch.payer_directory}
    assert len(names) >= 2


def test_the_register_is_not_written_inside_the_truth_directory(tmp_path, batch):
    """
    It is reference data a merchant already owns, not an answer key -- so it lives
    beside the three sides, not behind the isolation boundary. If it ever moved inside
    `_truth/`, the audit hook would block the agent from reading it and the whole design
    would fail closed, which is safe but wrong.
    """
    from recon.generator import build

    paths = build.write(batch, out_dir=tmp_path)
    directory = paths["payer_directory"]
    assert directory.is_file()
    assert "_truth" not in directory.parts
    assert directory.parent == tmp_path


def test_recon_inputs_still_carries_no_fourth_side(batch):
    """
    `ReconInputs` is what the engine receives. Adding the register to it would quietly
    hand the matcher a fourth side and dissolve the boundary the agentic design rests
    on: the engine weighs evidence, it does not go looking for it.
    """
    from dataclasses import fields as dc_fields

    from recon.schemas import ReconInputs

    names = {f.name for f in dc_fields(ReconInputs)}
    for forbidden in ("payer_directory", "authorisations", "register"):
        assert forbidden not in names
