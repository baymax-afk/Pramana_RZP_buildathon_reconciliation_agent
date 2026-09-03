"""
B5-B6: the investigator's bounds and the orchestrator's arithmetic.

The tests that matter here are not "does it find evidence" -- that depends on a register
and a model. They are:

  * the NULL-AGENT CONTROL: no investigator must reproduce the baseline byte for byte,
    because every claim this module makes is a delta against that baseline;
  * the BOUNDS: step budget, no-progress detection and malformed-argument retries each
    have to actually stop a loop, tested against deliberately misbehaving stubs rather
    than against a real model that happens to behave;
  * ATTRIBUTION: a verdict that moved must trace to a named proposal, and one that did
    not must not be credited to anything.

Nothing here calls a model. `conftest.py` already removes the API key for the session.
"""

from __future__ import annotations

import config as cfg
from recon.agent import (
    EvidenceField,
    EvidenceLedger,
    EvidenceProposal,
    RecordedInvestigator,
    Toolbox,
    orchestrate,
)
from recon.agent.schemas import InvestigationTrace
from recon.engine.match import match_once


# --------------------------------------------------------------------------
# The control arm
# --------------------------------------------------------------------------
def test_the_null_agent_reproduces_the_baseline_exactly(batch):
    """
    THE control. Every number this module reports is a delta against a run anyone can
    reproduce without an agent, so an agent that asserts nothing must change nothing --
    not "almost nothing".
    """
    run = orchestrate(batch.inputs, investigator=None, directory=batch.payer_directory)

    assert run.investigator == "none"
    assert run.proposals_accepted == 0
    assert run.payments_gained == 0
    assert run.deltas == []
    assert run.evidence_attributable_gain == 0.0
    assert run.baseline.assignment_map == run.enriched.assignment_map
    assert [r.bank_txn_id for r in run.baseline.refusals] == [
        r.bank_txn_id for r in run.enriched.refusals
    ]


def test_an_agent_that_proposes_nothing_also_changes_nothing(batch):
    """
    Distinct from the null agent: this one RUNS, calls tools, and concludes it has
    nothing. That is a correct outcome and must be free of side effects.
    """

    class Silent:
        name = "silent"

        def investigate(self, tb, txn_id):
            tb.get_exception(txn_id)
            tb.get_candidate_pool(txn_id)
            t = InvestigationTrace(bank_txn_id=txn_id)
            t.outcome = "insufficient_evidence"
            t.note = "nothing to assert"
            return t

    run = orchestrate(batch.inputs, Silent(), batch.payer_directory)
    assert run.investigated == run.exceptions_seen
    assert run.declined == run.exceptions_seen
    assert run.baseline.assignment_map == run.enriched.assignment_map


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------
def test_every_moved_verdict_is_attributed_to_a_named_proposal(batch):
    run = orchestrate(batch.inputs, RecordedInvestigator(), batch.payer_directory)
    if not run.deltas:
        import pytest

        pytest.skip("the register closed nothing at this seed")

    for d in run.deltas:
        assert d.attributed_to.strip(), (
            f"{d.bank_txn_id} moved with nothing named as the reason"
        )
        assert d.before != d.after


def test_the_agent_never_costs_precision(batch):
    """
    The one result that would sink this design. An agent that buys coverage by loosening
    correctness has done exactly what this project argues against, and the CLI exits
    non-zero when it happens.
    """
    from scorer.score import score

    run = orchestrate(batch.inputs, RecordedInvestigator(), batch.payer_directory)

    def _score(out):
        return score(
            out, batch.truth,
            total_payments=len(batch.inputs.payments),
            captured_payments=sum(1 for p in batch.inputs.payments if p.captured),
            ambiguity_bank_txn_id=batch.ambiguity_bank_txn_id or "",
            credits_by_id={x.id: x.credit for x in batch.inputs.bank_txns},
            seed=cfg.SEED_PRIMARY,
        )

    before, after = _score(run.baseline), _score(run.enriched)
    assert after.match_precision >= before.match_precision
    assert len(after.wrong_assignments) <= len(before.wrong_assignments)


def test_gain_is_zero_rather_than_undefined_with_no_proposals(batch):
    """An agent that asserted nothing gained nothing. That is a result, not a gap."""
    run = orchestrate(batch.inputs, investigator=None, directory=batch.payer_directory)
    assert run.evidence_attributable_gain == 0.0


# --------------------------------------------------------------------------
# The recorded investigator's decision procedure
# --------------------------------------------------------------------------
def test_an_arithmetic_refusal_is_declined_not_argued_with(batch):
    """
    A payer register cannot speak to a credit no subset of payments accounts for.
    Asserting anyway is how an investigator starts manufacturing evidence, so the
    out-of-scope case must be recognised and named.
    """
    run = orchestrate(batch.inputs, RecordedInvestigator(), batch.payer_directory)
    arithmetic = {
        r.bank_txn_id
        for r in run.baseline.refusals
        if r.category.value in ("no_subset_fits", "pool_exceeded", "unexplained_residual")
    }
    if not arithmetic:
        import pytest

        pytest.skip("no arithmetic refusals at this seed")

    for trace in run.traces:
        if trace.bank_txn_id in arithmetic:
            assert trace.outcome == "insufficient_evidence"
            assert "not a question about who paid" in trace.note
            assert not trace.proposals


def test_absence_from_the_register_is_declined_with_a_caveat(batch):
    """
    The register is deliberately incomplete, so "no entry" must not read as "this payer
    is unauthorised". An investigator that treated absence as disproof would manufacture
    confident wrong conclusions out of a gap in reference data.
    """
    run = orchestrate(batch.inputs, RecordedInvestigator(), batch.payer_directory)
    absent = [t for t in run.traces if "no register entry" in t.note]
    if not absent:
        import pytest

        pytest.skip("every payer was on the register at this seed")
    for t in absent:
        assert t.outcome == "insufficient_evidence"
        assert "not exhaustive" in t.note


def test_an_investigator_with_no_register_declines_everything(batch):
    """A batch with no side D must produce no assertions, and no errors either."""
    run = orchestrate(batch.inputs, RecordedInvestigator(), directory=())
    assert run.proposals_accepted == 0
    assert run.errors == 0
    assert run.baseline.assignment_map == run.enriched.assignment_map


# --------------------------------------------------------------------------
# The bounds
# --------------------------------------------------------------------------
def test_a_runaway_investigator_is_stopped_by_the_step_budget(batch):
    """
    Tested against a stub that deliberately never concludes, because a real model that
    happens to behave proves nothing about the bound.
    """
    calls = {"n": 0}

    class Runaway:
        name = "runaway"

        def investigate(self, tb, txn_id):
            trace = InvestigationTrace(bank_txn_id=txn_id)
            for _ in range(cfg.AGENT_STEP_BUDGET * 4):
                calls["n"] += 1
                tb.get_exception(txn_id)
                if calls["n"] > cfg.AGENT_STEP_BUDGET * 4:
                    break
            trace.outcome = "budget_exhausted"
            return trace

    run = orchestrate(batch.inputs, Runaway(), batch.payer_directory, max_exceptions=1)
    assert run.budget_exhausted == 1
    assert run.proposals_accepted == 0


def test_an_investigator_that_errors_is_counted_and_does_not_stop_the_run(batch):
    """One exception failing must cost one exception, not the batch."""

    class Broken:
        name = "broken"

        def investigate(self, tb, txn_id):
            t = InvestigationTrace(bank_txn_id=txn_id)
            t.outcome = "error"
            t.note = "simulated failure"
            return t

    run = orchestrate(batch.inputs, Broken(), batch.payer_directory)
    assert run.errors == run.exceptions_seen
    assert run.baseline.assignment_map == run.enriched.assignment_map


def test_a_proposal_the_boundary_refuses_is_counted_separately(batch):
    """
    An agent trying to name a record must show up in the numbers, not vanish. This is
    the row a reviewer should look at first.
    """

    class Cheater:
        name = "cheater"

        def investigate(self, tb, txn_id):
            t = InvestigationTrace(bank_txn_id=txn_id)
            pool = tb.get_candidate_pool(txn_id)
            target = (
                pool.payments[0].payment_id
                if not isinstance(pool, dict) and pool.payments
                else "pay_made_up"
            )
            receipt = tb.propose_evidence(
                txn_id, "authorised_payer_for", target, "naming the payment directly"
            )
            t.outcome = "proposed" if receipt.accepted else "insufficient_evidence"
            if receipt.accepted and receipt.proposal:
                t.proposals.append(receipt.proposal)
            return t

    run = orchestrate(batch.inputs, Cheater(), batch.payer_directory, max_exceptions=2)
    assert run.proposals_accepted == 0, "a payment id must never be accepted as evidence"
    assert run.baseline.assignment_map == run.enriched.assignment_map


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------
def test_a_saved_ledger_is_resumed_rather_than_re_investigated(batch, tmp_path):
    """
    The investigator makes network calls, so evidence already paid for must not be
    bought twice. And the resumed count is reported separately -- a run that resumed
    otherwise looked like a run that had silently skipped work.
    """
    from recon.agent import EvidenceReceipt

    target = sorted(
        match_once(batch.inputs).refusals, key=lambda r: -r.paise_at_risk
    )[0].bank_txn_id

    led = EvidenceLedger()
    led.add(EvidenceReceipt(True, EvidenceProposal(
        target, EvidenceField.AUTHORISED_PAYER_FOR, "Someone Else Ltd", "from an earlier run"
    )))
    path = led.write(tmp_path / "evidence.json")

    run = orchestrate(
        batch.inputs, RecordedInvestigator(), batch.payer_directory, ledger_path=path
    )
    assert run.resumed == 1
    assert run.investigated == run.exceptions_seen - 1
    assert not any(t.bank_txn_id == target for t in run.traces)
