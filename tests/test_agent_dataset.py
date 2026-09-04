"""
The agent, evaluated on more than one batch.

`run.py agent` had no `--dataset` flag, so every published agent number came from the
reported batch and the agent's generalisation was unmeasured. Worse, `load_payer_directory()`
defaults to the reported register — so the obvious way to run it against the holdout by
hand looks up one batch's payer names in another batch's authorisations and declines
almost everything. The first attempt at this measurement did exactly that and produced a
zero.

Measured properly, with each batch's own register:

    reported   5 name-conflict exceptions,  3 closed  (60%)   10 register rows
    holdout   10 name-conflict exceptions,  1 closed  (10%)   12 register rows

**That is the honest shape of this feature and it is worth stating plainly: the agent's
value is mostly a property of how complete the merchant's register is, not of the agent.**
"give us a better register and we close more exceptions at unchanged precision" is a
defensible product claim; "our agent closes exceptions" is not the same sentence.
"""

from __future__ import annotations

import pytest

import config as cfg
from loaders import load_inputs, load_payer_directory
from recon.agent.investigate import RecordedInvestigator
from recon.agent.orchestrate import orchestrate

pytestmark = pytest.mark.skipif(
    not (cfg.HOLDOUT / "manifest.json").is_file(),
    reason="no holdout; run `python run.py holdout`",
)


def _run(generated_dir, register):
    inputs = load_inputs(generated_dir=generated_dir)
    directory = load_payer_directory(register)
    return orchestrate(inputs, RecordedInvestigator(), directory, ledger_path=None)


def test_the_cli_can_point_the_agent_at_either_batch():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "pramana_cli.py").read_text(
        encoding="utf-8"
    )
    agent = src.split('sub.add_parser(\n        "agent"', 1)[1].split("set_defaults", 1)[0]
    assert "--dataset" in agent, (
        "the agent can only be run against the reported batch, so its numbers are "
        "single-batch and its generalisation is unmeasured"
    )


def test_each_batch_is_investigated_against_its_own_register():
    """
    The mistake this guards against produced a clean, wrong zero.

    Running the holdout against the reported merchant's register looks up payer names
    from one batch in another batch's authorisations. Everything declines, the run
    reports 0 exceptions closed, and nothing about it looks like an error.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "pramana_cli.py").read_text(
        encoding="utf-8"
    )
    assert "payer_directory.csv" in src, (
        "the agent command does not select a register per dataset, so a holdout run uses "
        "the reported batch's authorisations"
    )

    wrong = _run(cfg.HOLDOUT, None)                                  # reported register
    right = _run(cfg.HOLDOUT, cfg.HOLDOUT / "payer_directory.csv")   # its own
    assert right.proposals_accepted >= wrong.proposals_accepted, (
        "the holdout's own register should not do worse than a foreign one; if it does, "
        "the registers or the batches have been crossed"
    )


def test_the_agent_closes_less_on_the_shifted_batch_and_that_is_reported():
    """
    Not a bug — a measurement, and the one that keeps the product claim honest.

    If this ever inverts, the interesting question is why, and the answer belongs in the
    docs before the number goes on a slide.
    """
    reported = _run(None, None)
    holdout = _run(cfg.HOLDOUT, cfg.HOLDOUT / "payer_directory.csv")

    def conflicts(run):
        return sum(
            1 for r in run.baseline.refusals
            if r.category.value == "amount_name_conflict"
        )

    r_rate = reported.proposals_accepted / max(conflicts(reported), 1)
    h_rate = holdout.proposals_accepted / max(conflicts(holdout), 1)
    assert r_rate > h_rate, (
        f"the agent closes {r_rate:.0%} of name conflicts on the reported batch and "
        f"{h_rate:.0%} on the shifted one. If that has inverted, re-read why before "
        f"publishing it."
    )
    assert holdout.proposals_accepted >= 1, (
        "the agent asserts nothing at all on the shifted batch -- check the register was "
        "loaded, not just that the number is small"
    )


def test_precision_holds_on_both_batches_after_enrichment():
    """The only property that must not move, whichever batch it runs on."""
    from scorer.score import load_truth, score

    for generated_dir, register in (
        (None, None),
        (cfg.HOLDOUT, cfg.HOLDOUT / "payer_directory.csv"),
    ):
        inputs = load_inputs(generated_dir=generated_dir)
        truth_path = (
            (generated_dir / "_truth" / "ground_truth.json")
            if generated_dir is not None
            else cfg.TRUTH_DIR / "ground_truth.json"
        )
        raw, links = load_truth(truth_path)
        run = _run(generated_dir, register)

        def prec(out):
            return score(
                out, links,
                total_payments=len(inputs.payments),
                captured_payments=sum(1 for p in inputs.payments if p.captured),
                ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
                credits_by_id={x.id: x.credit for x in inputs.bank_txns},
                seed=inputs.seed,
            ).match_precision

        assert prec(run.enriched) >= prec(run.baseline), (
            "enrichment bought coverage by costing precision -- revert the evidence "
            "field rather than reporting the gain"
        )
