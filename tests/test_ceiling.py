"""
The reachable ceiling, and the artefact boundary that carries it.

the 2026-09-03 audit §8 item 6 asked for the ceiling on the page, and the reason it was
worth asking for is that a match rate with no ceiling beside it invites comparison
against 100%. 100% is not on offer: of 22 unmatched captured payments, 17 are unreachable
by construction -- they never settled, or they belong to a relation this engine does not
model and refuses correctly. The number worth arguing about is 5, and until the panel
existed that number appeared only in terminal output during a scoring run.

Three things are pinned here, and one of them is the reason the artefact is separate.

1. **The arithmetic closes.** `payments_assigned + short_of_ceiling == reachable`, and
   `reachable + unreachable == captured`. A ceiling that does not close is a ceiling
   someone typed in.

2. **The ceiling MOVES between batches.** A constant would satisfy every arithmetic
   check on the batch it was written against, which is exactly how a hardcoded number
   survives a test suite. The holdout is a different distribution and must produce a
   different ceiling.

3. **The scorecard is a separate file from `run_output.json`, and the engine payload
   still carries no ground truth.** This is the load-bearing one. The ceiling is derived
   from truth; `run_output.json` is defined as what the engine could justify without an
   answer key. Merging them would make that claim unverifiable by opening the file, and
   the separation is only worth anything if something fails when it is broken.
"""

from __future__ import annotations

import json

import pytest

import config as cfg
from loaders import load_inputs
from recon.engine.match import match_once
from scorer import artifact
from scorer.score import load_truth, score


def _score_dir(generated_dir=None):
    inputs = load_inputs(generated_dir=generated_dir)
    truth_path = (
        (generated_dir / "_truth" / "ground_truth.json")
        if generated_dir is not None
        else cfg.TRUTH_DIR / "ground_truth.json"
    )
    raw, links = load_truth(truth_path)
    out = match_once(inputs)
    return score(
        out,
        links,
        total_payments=len(inputs.payments),
        captured_payments=sum(1 for p in inputs.payments if p.captured),
        ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
        credits_by_id={x.id: x.credit for x in inputs.bank_txns},
        seed=inputs.seed,
    )


@pytest.fixture(scope="module")
def primary():
    return _score_dir()


def test_the_ceiling_arithmetic_closes(primary):
    sc = primary
    assert sc.payments_assigned + sc.short_of_ceiling == sc.reachable_payments
    assert sc.reachable_payments <= sc.captured_payments
    assert sc.match_rate <= sc.ceiling


def test_every_payment_short_of_the_ceiling_is_named(primary):
    """
    The count and the list must agree.

    The panel shows both -- "short of it: 5" above a list of rows -- and a count that
    disagreed with its own list would be the exact shape of defect this project keeps
    finding: a number that looks right beside evidence that does not support it.
    """
    named = sum(len(t.payment_ids) for t in primary.short_of_ceiling_txns)
    assert named == primary.short_of_ceiling


def test_a_shortfall_is_a_refusal_and_never_a_wrong_post(primary):
    """
    Every entry cost coverage. None cost precision.

    A shortfall row whose `engine_verdict` was an assignment would mean the engine posted
    that credit to a DIFFERENT payment than truth wanted -- a precision failure wearing a
    coverage failure's clothes. The panel says "no money was posted anywhere" in as many
    words, so it has to be true.
    """
    for t in primary.short_of_ceiling_txns:
        assert t.engine_verdict != "assigned_elsewhere", (
            f"{t.bank_txn_id} is counted as a coverage miss but the engine assigned it "
            f"elsewhere -- that is a precision failure and must not be reported as a gap"
        )
    assert primary.match_precision == 1.0
    assert not primary.wrong_assignments


@pytest.mark.skipif(
    not (cfg.HOLDOUT / "manifest.json").is_file(),
    reason="no holdout; run `python run.py holdout`",
)
def test_the_ceiling_moves_between_batches(primary):
    """
    The check a hardcoded constant fails.

    Every other assertion in this file would pass against `return 0.9124`. This one
    would not: the holdout is a deliberately shifted distribution with a different mix
    of unsettled payments and unmodelled relations, so its ceiling is a different
    number. If these two ever coincide, read the generator before believing it.
    """
    holdout = _score_dir(cfg.HOLDOUT)
    assert holdout.reachable_payments > 0
    assert holdout.ceiling != primary.ceiling, (
        "the primary and holdout ceilings are identical, which is one batch too many "
        "for a number derived from each batch's own truth"
    )


def test_the_artefact_reports_the_three_way_split(primary):
    payload = artifact.build(primary, seed=1, dataset="primary")
    c = payload["coverage"]
    assert c["payments_assigned"] + c["short_of_ceiling"] + c["unreachable_payments"] == (
        c["captured_payments"]
    )
    assert len(payload["short_of_ceiling_txns"]) == len(primary.short_of_ceiling_txns)
    # The provenance travels with the numbers, not only in a docstring.
    assert "never received" in payload["provenance"]


def test_the_artefact_is_json_serialisable(primary):
    """`ShortfallTxn` is a slotted frozen dataclass; `asdict` has to survive the trip."""
    payload = artifact.build(primary, seed=1, dataset="primary")
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload


@pytest.mark.skipif(
    not (cfg.REPORTS / "run_output.json").is_file(),
    reason="no run_output.json; run `python run.py match --verify`",
)
def test_the_engine_payload_still_carries_no_ground_truth():
    """
    The reason the scorecard is a separate file.

    `run_output.json` is what a merchant sees, and its guarantee is that everything in it
    was justifiable without an answer key. Every term below is truth-derived: if one
    appears in that payload, the guarantee has quietly stopped holding and the two
    artefacts have merged back into one.
    """
    blob = (cfg.REPORTS / "run_output.json").read_text(encoding="utf-8")
    for term in (
        "ceiling",
        "reachable",
        "short_of_ceiling",
        "expected_verdict",
        "defect_labels",
        "ground_truth",
    ):
        assert term not in blob, (
            f"run_output.json contains {term!r} -- that is scoring data, and the payload "
            f"is defined as what the engine could justify with no ground truth"
        )


@pytest.mark.skipif(
    not (cfg.REPORTS / "scorecard.json").is_file(),
    reason="no scorecard.json; run `python run.py match --verify`",
)
def test_the_committed_scorecard_agrees_with_a_fresh_score(primary):
    """
    Drift protection, in the spirit of `tests/test_reported_numbers.py`.

    The committed scorecard is what the demo serves. Regenerating the batch without
    re-scoring would leave the panel quoting numbers no run produces any more.
    """
    served = json.loads((cfg.REPORTS / "scorecard.json").read_text(encoding="utf-8"))
    assert served["coverage"]["reachable_payments"] == primary.reachable_payments
    assert served["coverage"]["short_of_ceiling"] == primary.short_of_ceiling
    assert served["precision"]["match_precision"] == primary.match_precision
    assert [t["bank_txn_id"] for t in served["short_of_ceiling_txns"]] == [
        t.bank_txn_id for t in primary.short_of_ceiling_txns
    ]
