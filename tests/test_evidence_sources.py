"""
Which evidence SOURCE closed an exception.

`REVIEW.md` section 8's post-buildathon list opens with this: *"Not 'our AI matched more'
but 'this evidence source closed N exceptions at unchanged precision.'"* The difference
is not presentational. The first is a claim about a vendor; the second is a claim about a
dataset, and only the second tells a controller whether to license the dataset.

Three properties make the number worth quoting, and each is pinned below.

1. **A source is credited by what was LOOKED AT, not by what was said.** The mapping runs
   off `EvidenceProposal.tool_calls`, so a proposal that consulted nothing external
   cannot be filed under a source that would make it look purchased.

2. **A proposal with no external citation is reported separately, always.** It may be
   right. It is not a dataset anyone can buy or re-read, and putting it in the same
   column as a register lookup is the overclaim this metric exists to refuse.

3. **Per-source numbers come from a counterfactual, not an apportionment.** Each row is a
   separate run of the engine carrying only that source's evidence. Rows may therefore
   overlap and need not sum to the total -- which the report says, rather than
   normalising the overlap away.
"""

from __future__ import annotations

import pytest

import config as cfg
from loaders import load_inputs, load_payer_directory
from recon.agent.investigate import RecordedInvestigator
from recon.agent.orchestrate import orchestrate
from recon.agent.sources import MODEL_ASSERTION, SourceContribution, sources_of
from scorer.score import load_truth, score


# ---- the mapping ----------------------------------------------------------
def test_a_tool_that_reads_an_external_dataset_credits_that_dataset():
    assert sources_of(("lookup_payer_relationship('ACME')",)) == (
        "authorised_payer_register",
    )
    assert sources_of(("search_invoices('acme')",)) == ("invoice_ledger",)


def test_a_proposal_built_only_from_engine_reads_cites_no_source():
    """
    The row that matters most, and the one an eager version of this would hide.

    `get_exception` and `get_candidate_pool` read the engine's own working. An agent that
    consulted only those and asserted something anyway has made a claim with no citation.
    """
    assert sources_of(("get_exception('bank_txn_0001')", "test_subset(...)")) == (
        MODEL_ASSERTION,
    )
    assert sources_of(()) == (MODEL_ASSERTION,), (
        "a proposal with no tool calls at all must still land in a named bucket -- an "
        "empty tuple would drop it out of a group-by instead of standing out in one"
    )


def test_a_proposal_citing_two_datasets_credits_both():
    got = sources_of(
        ("search_invoices('a')", "lookup_payer_relationship('b')", "search_invoices('c')")
    )
    assert set(got) == {"invoice_ledger", "authorised_payer_register"}
    assert len(got) == 2, "a repeated call must not double-count its source"


def test_an_unmeasured_precision_says_so_rather_than_reading_as_unchanged():
    """
    `precision_measured` is False until the scorer fills it in, because the agent runs
    inside the ground-truth boundary and cannot compute it. Serialising 0.0 in that state
    would report a catastrophic precision as though it were measured.
    """
    c = SourceContribution(source="authorised_payer_register", proposals=2)
    assert "precision" in c.as_dict()
    assert "unmeasured" in c.as_dict()["precision"]
    assert "precision_delta" not in c.as_dict()

    c.precision_before, c.precision_after, c.precision_measured = 1.0, 1.0, True
    assert c.as_dict()["precision_delta"] == 0.0


# ---- the measurement, end to end ------------------------------------------
@pytest.fixture(scope="module")
def run():
    inputs = load_inputs()
    return inputs, orchestrate(inputs, RecordedInvestigator(), load_payer_directory())


def test_every_closed_exception_is_attributed_to_a_named_source(run):
    inputs, r = run
    assert r.by_source, "the agent asserted evidence but credited no source"
    closed = {t for c in r.by_source.values() for t in c.bank_txn_ids}
    moved = {d.bank_txn_id for d in r.deltas if d.after == "assign"}
    assert moved <= closed, (
        f"verdicts moved with no source credited: {sorted(moved - closed)}. A coverage "
        f"gain nobody can attribute is the 'our AI matched more' claim."
    )


def test_a_sources_row_is_a_counterfactual_run_not_an_apportionment(run):
    """
    Every row must have a real `MatchOutput` behind it, because that is what makes the
    row a measurement. An apportionment would produce the same table with no runs.
    """
    inputs, r = run
    for source, contribution in r.by_source.items():
        isolated = r.source_outputs.get(source)
        assert isolated is not None, f"{source} has a row with no run behind it"
        for txn in contribution.bank_txn_ids:
            assert txn in isolated.assignment_map, (
                f"{source} is credited with closing {txn}, but the run carrying only "
                f"its evidence did not assign it"
            )
            assert txn not in r.baseline.assignment_map, (
                f"{source} is credited with closing {txn}, which the baseline had "
                f"already assigned"
            )


def test_no_source_buys_coverage_by_costing_precision(run):
    """
    The whole point of measuring per source: a source that gains coverage while moving
    precision is one to decline, and the report has to be able to say which.
    """
    inputs, r = run
    raw, links = load_truth(cfg.TRUTH_DIR / "ground_truth.json")

    def prec(out):
        return score(
            out, links,
            total_payments=len(inputs.payments),
            captured_payments=sum(1 for p in inputs.payments if p.captured),
            ambiguity_bank_txn_id=raw.get("ambiguity_bank_txn_id", ""),
            credits_by_id={x.id: x.credit for x in inputs.bank_txns},
            seed=inputs.seed,
        ).match_precision

    baseline = prec(r.baseline)
    for source, isolated in r.source_outputs.items():
        assert prec(isolated) >= baseline, (
            f"{source} raises coverage and lowers precision from {baseline} to "
            f"{prec(isolated)} -- report it as a source to decline, do not ship it"
        )


def test_the_null_agent_credits_nothing(run):
    """The control arm asserts nothing, so there is nothing to attribute."""
    inputs, _ = run
    null = orchestrate(inputs, None, load_payer_directory())
    assert null.by_source == {}
    assert null.source_outputs == {}
    assert null.as_dict()["by_source"] == []
