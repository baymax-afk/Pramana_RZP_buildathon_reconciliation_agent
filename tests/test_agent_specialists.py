"""
Routing, the validation layer, and the two mistakes that cost precision on the way here.

The agent went from one investigator over every refusal to three specialists routed by
what the engine objected to. That is a bigger change than it looks, because it moves the
agent from a channel that can only stop a name veto to channels that change what the
ARITHMETIC is asked to explain -- the amount channel, which this engine treats as primary
and which `docs/AGENTIC.md` is most careful about.

**Two of these tests exist because the implementation failed them first, and both
failures were bought coverage paid for in precision.**

The invoice specialist's first version read the engine's residual on a refused candidate
and asserted it as a short payment. Every step was defensible; the composite was circular,
because a figure taken from the gap will always close the gap. It bought four payments and
two wrong postings: precision 1.0000 -> 0.9854.

The obvious patch -- only assert against an invoice the ledger marks `part_settled` --
looked principled and was still wrong, because a status says a shortfall happened and not
how large it was. The holdout caught what the primary batch had stopped showing: 1.0000 ->
0.9913, one wrong posting, same mechanism wearing a corroborating flag.

The rule that survived is strict and is what these tests defend: **a deduction is
admissible when a record NAMES the figure**, and a figure the engine already subtracts is
a restatement rather than evidence. The consequence, stated rather than engineered around,
is that on these batches no deduction channel can be accepted at all -- the machinery is
built and validated and the ledger cannot feed it. That is a fact about the generator's
invoice schema, not a fault in the channel.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import config as cfg
from recon.agent import investigate as inv_mod
from recon.agent import routing as agent_routing
from recon.agent import validate as validate_mod
from recon.agent.schemas import (
    EvidenceField,
    EvidenceProposal,
    SourceType,
)
from recon.agent.tools import Toolbox
from recon.agent.validate import EvidenceContext, validate_proposal
from recon.engine.match import match_once
from recon.engine.results import RefusalCategory


@pytest.fixture(scope="module")
def batch():
    from recon.generator import build

    return build.generate(seed=cfg.SEED_PRIMARY)


@pytest.fixture(scope="module")
def ctx(batch):
    return EvidenceContext(batch.inputs, match_once(batch.inputs), batch.payer_directory)


@pytest.fixture(scope="module")
def box(batch):
    return Toolbox(batch.inputs, match_once(batch.inputs), batch.payer_directory)


def _refused(ctx, category: str) -> str:
    for r in ctx.out.refusals:
        if r.category.value == category:
            return r.bank_txn_id
    pytest.skip(f"no {category} refusal at this seed")


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------
def test_each_specialist_is_routed_only_what_it_declares(batch):
    """
    `handles` is a claim a specialist makes about itself and the router enforces. If the
    two drift, the fleet develops either a role nothing reaches or a category routed to
    somebody who cannot work it -- both silent.
    """
    fleet = {
        "payment": inv_mod.PaymentInvestigator(),
        "bank": inv_mod.BankInvestigator(),
        "invoice": inv_mod.InvoiceInvestigator(),
    }
    for category, roles in agent_routing.ROUTE.items():
        for role in roles:
            assert role in fleet, f"{category} routes to {role!r}, which does not exist"
            assert category in fleet[role].handles, (
                f"{category} is routed to {role} and {role} does not declare it"
            )
    for role, specialist in fleet.items():
        for category in specialist.handles:
            assert role in agent_routing.ROUTE.get(category, ()), (
                f"{role} declares {category} and the router never sends it"
            )


def test_no_specialist_is_routed_a_tie(batch):
    """
    The five never-investigate categories are cases where refusing is the right answer.
    An agent asked which of two equal candidates is more likely will always produce one.
    """
    for category in agent_routing.NEVER:
        assert agent_routing.roles_for(category) == (), (
            f"{category} is routed to an investigator and must not be"
        )
        assert agent_routing.why_not(category), f"{category} is skipped without a reason"


def test_every_refusal_category_is_either_routed_or_explicitly_not(batch):
    """
    A category the table has never heard of falls through to nobody, which is safe but
    silent. Adding one to the engine should make somebody decide which of the two it is.
    """
    for category in RefusalCategory:
        known = (
            category.value in agent_routing.ROUTE
            or category.value in agent_routing.NEVER
        )
        assert known, (
            f"{category.value} is neither routed to a specialist nor listed as one no "
            f"agent may work. Decide which in recon/agent/routing.py -- an unlisted "
            f"category silently gets no investigation and no explanation"
        )


def test_the_never_set_covers_every_tie_the_engine_can_emit():
    for category in (
        "multiple_candidates",
        "ambiguous_grouping",
        "contested_payment",
        "solution_cap_reached",
        "order_dependent_assignment",
    ):
        assert category in agent_routing.NEVER


# --------------------------------------------------------------------------
# The validation layer
# --------------------------------------------------------------------------
def _proposal(txn_id, field, value, **kw):
    kw.setdefault("rationale", "because the record says so")
    return EvidenceProposal(bank_txn_id=txn_id, field=field, value=value, **kw)


def test_a_fact_with_no_source_that_moves_money_is_refused(ctx):
    """
    `model_assertion` is a real citation -- it says "nothing external" -- and it is not a
    way round the requirement. A model may recognise that a spelling variant names the
    same company. It may not conclude, from nothing, that money was withheld.
    """
    txn = _refused(ctx, "no_subset_fits")
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.CREDIT_NOTE_CONFIRMED,
            "issued",
            amount_paise=50_000,
            source_type=SourceType.MODEL_ASSERTION,
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "external source" in receipt.error


def test_an_external_citation_must_name_a_record_that_exists(ctx):
    txn = _refused(ctx, "amount_name_conflict")
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.AUTHORISED_PAYER_FOR,
            "Some Customer Ltd",
            source_type=SourceType.INVOICE_LEDGER,
            source_ref="INV-9999-0001",
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "no invoice" in receipt.error


def test_a_true_fact_about_an_unreachable_record_is_still_refused(ctx, batch):
    """
    Existing is not enough. A fact about an invoice no payment in this credit's pool
    settles is evidence about a different pair -- which `fellegi_sunter` already declines
    to weigh, and which this declines to record.
    """
    txn = _refused(ctx, "amount_name_conflict")
    reachable = ctx.invoices_for(txn)
    elsewhere = next(
        (i.invoice_no for i in batch.inputs.invoices if i.invoice_no not in reachable),
        None,
    )
    if elsewhere is None:
        pytest.skip("every invoice is reachable from this credit")
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.AUTHORISED_PAYER_FOR,
            "Some Customer Ltd",
            source_type=SourceType.INVOICE_LEDGER,
            source_ref=elsewhere,
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "not reachable" in receipt.error


def test_evidence_read_before_the_period_is_stale(ctx, batch):
    txn = _refused(ctx, "amount_name_conflict")
    payer = batch.payer_directory[0].payer_name
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.AUTHORISED_PAYER_FOR,
            "Some Customer Ltd",
            source_type=SourceType.PAYER_REGISTER,
            source_ref=payer,
            retrieved_at="2019-01-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "before this batch closed" in receipt.error


def test_staleness_is_measured_against_the_batch_not_a_wall_clock():
    """
    A clock here would make every test race the calendar, and -- more to the point --
    "is this stale" means "was it read after the money moved", which is a question about
    the batch.
    """
    # Parsed, not grepped. The module's own docstring names `datetime.now()` in order to
    # say it does not use one, and a scanner that could not tell prose from a call would
    # force the explanation out of the code -- the wrong trade, and the same
    # accommodation `tests/test_ui_navigation.py` makes for comments.
    source = inspect.getsource(validate_mod)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            fn = node.func
            called = getattr(fn, "attr", None) or getattr(fn, "id", None)
            assert called not in ("now", "today", "utcnow", "time"), (
                "the validation layer reads a wall clock; staleness must be measured "
                "against the batch's own latest bank date, or every test races the "
                "calendar"
            )
    assert "latest_bank_date" in source, (
        "the batch horizon is gone, so whatever staleness is measured against now is "
        "not the batch"
    )


def test_a_duplicate_assertion_on_the_same_channel_is_refused(ctx, batch):
    txn = _refused(ctx, "amount_name_conflict")
    payer = batch.payer_directory[0].payer_name
    proposal = _proposal(
        txn,
        EvidenceField.AUTHORISED_PAYER_FOR,
        "Some Customer Ltd",
        source_type=SourceType.PAYER_REGISTER,
        source_ref=payer,
        retrieved_at="2026-09-01",
    )
    receipt = validate_proposal(
        proposal, ctx, already={(txn, "authorised_payer_for")}
    )
    assert receipt.accepted is False
    assert "append-only" in receipt.error


# ---- the two failures that cost precision ---------------------------------
def test_a_deduction_taken_from_the_gap_is_refused(ctx, batch):
    """
    The first failure, as a test. A figure derived from the residual will always close
    the residual, so the ledger has to NAME it. This one does not: the invoice records a
    gross and a TDS and nothing else.
    """
    txn = _refused(ctx, "no_subset_fits")
    reachable = sorted(ctx.invoices_for(txn))
    if not reachable:
        pytest.skip("no invoice is reachable from this credit")
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.INVOICE_PART_PAYMENT,
            "short_paid",
            amount_paise=498,
            source_type=SourceType.INVOICE_LEDGER,
            source_ref=reachable[0],
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "names no other amount" in receipt.error or "states no other amount" in receipt.error


def test_a_corroborating_status_is_not_a_corroborating_amount(ctx, batch):
    """
    The second failure. `part_settled` says a shortfall happened; it does not say how
    large it was, so an amount asserted beside it still comes from the gap. The holdout
    caught this after the primary batch stopped showing it.
    """
    part_settled = {
        i.invoice_no for i in batch.inputs.invoices if i.status == "part_settled"
    }
    if not part_settled:
        pytest.skip("no part_settled invoice in this batch")
    for txn in (r.bank_txn_id for r in ctx.out.refusals):
        reachable = ctx.invoices_for(txn) & part_settled
        if not reachable:
            continue
        receipt = validate_proposal(
            _proposal(
                txn,
                EvidenceField.INVOICE_PART_PAYMENT,
                "short_paid",
                amount_paise=1_000,
                source_type=SourceType.INVOICE_LEDGER,
                source_ref=sorted(reachable)[0],
                retrieved_at="2026-09-01",
            ),
            ctx,
        )
        assert receipt.accepted is False, (
            "a part_settled status was accepted as corroboration for an amount it does "
            "not state -- this is the mechanism that cost precision on the holdout"
        )
        return
    pytest.skip("no refused credit reaches a part_settled invoice")


def test_restating_a_deduction_the_engine_already_makes_is_refused(ctx, batch):
    """
    `fees.known_deductions` reads `Payment.amount_refunded` and `Invoice.tds_amount`.
    Asserting either would deduct it twice -- manufacturing a gap rather than closing one.
    """
    refunded = next(
        (p for p in batch.inputs.payments if (p.amount_refunded or 0) > 0), None
    )
    if refunded is None:
        pytest.skip("no refunded payment in this batch")
    txn = next(
        (
            r.bank_txn_id
            for r in ctx.out.refusals
            if refunded.id in ctx.pool_ids(r.bank_txn_id)
        ),
        None,
    )
    if txn is None:
        pytest.skip("no refused credit has the refunded payment in its pool")
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.REFUND_STATUS,
            "partial",
            amount_paise=refunded.amount_refunded,
            source_type=SourceType.PAYMENT_RECORD,
            source_ref=refunded.id,
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "already subtracts" in receipt.error


def test_a_contradicting_tds_figure_is_refused(ctx, batch):
    txn = _refused(ctx, "no_subset_fits")
    reachable = sorted(ctx.invoices_for(txn))
    if not reachable:
        pytest.skip("no invoice reachable")
    inv = ctx.invoice(reachable[0])
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.TDS_CONFIRMED,
            "withheld",
            amount_paise=inv.tds_amount + 12_345,
            source_type=SourceType.INVOICE_LEDGER,
            source_ref=inv.invoice_no,
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False


def test_a_deduction_cannot_exceed_the_credit_it_explains(ctx, batch):
    txn = _refused(ctx, "no_subset_fits")
    line = ctx.txn(txn)
    reachable = sorted(ctx.invoices_for(txn))
    if not reachable:
        pytest.skip("no invoice reachable")
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.CREDIT_NOTE_CONFIRMED,
            "issued",
            amount_paise=line.credit + 1,
            source_type=SourceType.INVOICE_LEDGER,
            source_ref=reachable[0],
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False


def test_a_settlement_date_after_the_credit_is_refused(ctx):
    """Money does not reach the bank before it is settled."""
    txn = _refused(ctx, "no_subset_fits")
    line = ctx.txn(txn)
    from datetime import date, timedelta

    later = (date.fromisoformat(line.txn_date) + timedelta(days=1)).isoformat()
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.SETTLEMENT_DATE_CONFIRMED,
            later,
            source_type=SourceType.BANK_STATEMENT,
            source_ref=line.ref_no,
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "before it is settled" in receipt.error


def test_a_settlement_date_far_outside_the_window_is_tuning_not_evidence(ctx):
    """
    Re-anchoring the window is evidence about one credit. Reaching arbitrarily far back
    is a change to the search bound, which is the second of the five prohibitions.
    """
    txn = _refused(ctx, "no_subset_fits")
    line = ctx.txn(txn)
    from datetime import date, timedelta

    far = (
        date.fromisoformat(line.txn_date)
        - timedelta(days=cfg.LOOKBACK_DAYS + validate_mod.EVIDENCE_WINDOW_SLACK_DAYS + 5)
    ).isoformat()
    receipt = validate_proposal(
        _proposal(
            txn,
            EvidenceField.SETTLEMENT_DATE_CONFIRMED,
            far,
            source_type=SourceType.BANK_STATEMENT,
            source_ref=line.ref_no,
            retrieved_at="2026-09-01",
        ),
        ctx,
    )
    assert receipt.accepted is False
    assert "search bound" in receipt.error


# --------------------------------------------------------------------------
# What the validation layer must never do
# --------------------------------------------------------------------------
def test_the_validation_layer_reaches_no_verdict():
    """
    Parsed rather than read, the same technique `tests/test_isolation.py` uses on the
    boundary. A validation layer that started making decisions would be a second
    decision-maker with none of the first one's verification behind it.
    """
    tree = ast.parse(inspect.getsource(validate_mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    for banned in ("match_once", "Verdict", "Assignment"):
        assert not any(banned in name for name in imported), (
            f"the validation layer imports {banned}; it decides whether evidence is "
            f"admissible, never what the evidence means"
        )
    source = inspect.getsource(validate_mod)
    for banned in ("assignment_map", ".assignments"):
        assert banned not in source, (
            f"the validation layer reads {banned}; it must not know what was posted"
        )


def test_the_engines_deduction_tokens_match_the_agents():
    """
    `recon.engine` does not import `recon.agent` -- inverting that would let the agent
    package's imports run inside the matcher -- so the deduction vocabulary is stated in
    both. Restated, not duplicated silently: this pins them together.
    """
    from recon.agent.schemas import _FIELD_RULES
    from recon.engine.match import _DEDUCTION_TOKENS

    for field, rule in _FIELD_RULES.items():
        if not rule.deduction_for:
            continue
        assert field.value in _DEDUCTION_TOKENS, (
            f"{field.value} is a deduction channel the matcher does not read"
        )
        assert set(rule.deduction_for) == _DEDUCTION_TOKENS[field.value], (
            f"{field.value} means different things to the agent and the engine"
        )
    assert set(_DEDUCTION_TOKENS) <= {f.value for f in EvidenceField}


def test_the_docs_name_the_specialists_that_exist():
    """A design note describing a fleet that was not built is worse than none."""
    agentic = (Path(__file__).resolve().parents[1] / "docs" / "AGENTIC.md").read_text(
        encoding="utf-8"
    )
    for role in ("PaymentInvestigator", "BankInvestigator", "InvoiceInvestigator"):
        assert role in agentic, f"docs/AGENTIC.md does not describe {role}"
