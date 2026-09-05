"""
B1-B3: the tool surface, the ledger, and what the agent structurally cannot do.

The audit's sharpest finding was that "carries no payment id" is not the same as
"cannot name one": `merchant_ref` reaches a payment through `ReferenceIndex`, and tier 1
outranks everything in `evidence_key`, so a plausible invoice number selects a payment
and wins contested money (`REVIEW.md` section 5). This surface is much wider than the
narration tier's, so the same mistake here would be much worse. These tests are written
against that specific failure mode rather than against the general principle.
"""

from __future__ import annotations

import pytest

import config as cfg
from recon.agent import EvidenceField, EvidenceLedger, EvidenceProposal, Toolbox
from recon.agent.tools import _normalise
from recon.engine.match import match_once


@pytest.fixture(scope="module")
def box(request):
    from recon.generator import build

    b = build.generate(seed=cfg.SEED_PRIMARY)
    out = match_once(b.inputs)
    return b, out, Toolbox(b.inputs, out, b.payer_directory)


# --------------------------------------------------------------------------
# Scoping: what the tools refuse to do
# --------------------------------------------------------------------------
def test_no_tool_can_post_unpost_or_rescore_a_match(box):
    """
    The surface is eight reads and one write, and the write appends to a ledger. If a
    mutating verb ever appears here, this fails -- which is a cruder check than reading
    the code, and survives the code being edited by someone who has not read this file.

    The exact set is spelled out so that ADDING a tool fails this test on purpose. A new
    read is a new thing an agent can see, and widening what an investigator sees is a
    decision worth making deliberately rather than noticing later.
    """
    from recon.agent.tools import TOOL_NAMES

    assert set(TOOL_NAMES) == {
        "get_exception", "get_candidate_pool", "test_subset",
        "lookup_payer_relationship", "search_invoices",
        "get_payment_record", "get_bank_line", "get_invoice",
        "propose_evidence",
    }
    for name in TOOL_NAMES:
        for verb in ("assign", "post", "match", "score", "approve", "resolve", "set_"):
            assert verb not in name, f"{name} reads like a mutation"


def test_no_read_exposes_the_engines_own_confidence(box):
    """
    `Assignment` carries `confidence`, `fs_weight`, `uniqueness_margin` and
    `permutation_stability`. None of them is projected by any tool, and that is not an
    oversight: an investigator that could see how sure the engine was would be
    investigating the engine's opinion rather than the merchant's records -- and the
    first thing it would learn is which credits are worth arguing about.
    """
    import json

    b, out, tb = box
    refused = out.refusals[0].bank_txn_id
    assigned = next(iter(out.assignment_map))
    payment = sorted(out.assignment_map[assigned])[0]
    invoice = next(
        (p.notes.get("invoice_no") for p in b.inputs.payments if p.id == payment), ""
    )

    seen = []
    for result in (
        tb.get_exception(refused),
        tb.get_candidate_pool(refused),
        tb.get_bank_line(assigned),
        tb.get_payment_record(payment),
        tb.get_invoice(invoice),
        tb.test_subset(refused, (payment,)),
    ):
        if hasattr(result, "as_dict"):
            seen.append(json.dumps(result.as_dict()))
    assert len(seen) >= 5, "the reads under test did not return typed results"
    blob = " ".join(seen)
    for leak in ("confidence", "fs_weight", "uniqueness_margin", "permutation_stability"):
        assert leak not in blob, f"a tool projects the engine's own {leak}"


def test_test_subset_answers_with_the_engines_own_arithmetic(box):
    """
    `test_subset` must not reimplement conservation. A second implementation would be a
    second source of truth about money, and the two would eventually disagree somewhere
    nobody was looking.
    """
    batch, out, tb = box
    txn_id, payments = next(iter(out.assignment_map.items()))
    verdict = tb.test_subset(txn_id, tuple(sorted(payments)))

    assert verdict.fits is True
    assert abs(verdict.residual_paise) <= verdict.tolerance_paise
    assert verdict.tolerance_paise == cfg.TOL_ABS_PAISE
    assert verdict.expected_lo_paise <= verdict.credit_paise <= verdict.expected_hi_paise

    # And it agrees with what the engine actually posted.
    assignment = next(a for a in out.assignments if a.bank_txn_id == txn_id)
    assert verdict.residual_paise == assignment.residual_paise


def test_test_subset_cannot_be_used_to_post_anything(box):
    """It answers a question. The run it was built from is unchanged afterwards."""
    batch, out, tb = box
    before = {k: sorted(v) for k, v in out.assignment_map.items()}
    refused = out.refusals[0]
    tb.test_subset(refused.bank_txn_id, tuple(p.id for p in batch.inputs.payments[:3]))
    assert {k: sorted(v) for k, v in out.assignment_map.items()} == before


def test_unknown_ids_come_back_as_errors_not_exceptions(box):
    """
    A model will pass a wrong id. It must get a typed error it can read and correct,
    not a traceback that ends the investigation.
    """
    _, _, tb = box
    assert "error" in tb.get_exception("bank_txn_99999")
    assert "error" in tb.get_candidate_pool("nope")
    assert "error" in tb.test_subset("bank_txn_0001", ("pay_does_not_exist",))
    assert "error" in tb.test_subset("bank_txn_0001", ())


# --------------------------------------------------------------------------
# The write, and the boundary it enforces
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "pay_SYN001441035",       # a payment id
        "PAY_SYN001441035",       # ...case-shifted
        "order_MmMxQbDa79TkDi",   # an order id
        "bank_txn_0103",          # a bank transaction id
        "ICICR92259685884",       # a UTR
    ],
)
def test_the_agent_cannot_name_a_record_even_indirectly(box, value):
    """
    THE test this surface exists to pass.

    `EvidenceProposal` has no payment-id field, but the audit showed that absence is not
    the same as impossibility -- a free-text value can carry an identifier that resolves
    to a record downstream. So the value is checked against the shape of every
    identifier in the batch and rejected, with an error that says why.
    """
    _, _, tb = box
    receipt = tb.propose_evidence(
        "bank_txn_0103", "authorised_payer_for", value, "trying it on"
    )
    assert receipt.accepted is False
    assert "identifier" in receipt.error
    assert "REVIEW.md" in receipt.error


def test_an_invented_evidence_channel_is_refused_loudly(box):
    """
    A field the engine does not weigh must be REFUSED, not accepted and ignored. Accepted
    and ignored is the worst outcome: the agent reports success, the verdict never
    moves, and nothing anywhere says why.
    """
    _, _, tb = box
    receipt = tb.propose_evidence(
        "bank_txn_0103", "confidence_override", "0.99", "because I say so"
    )
    assert receipt.accepted is False
    assert "authorised_payer_for" in receipt.error


def test_a_proposal_must_carry_its_reasoning(box):
    _, _, tb = box
    receipt = tb.propose_evidence(
        "bank_txn_0103", "authorised_payer_for", "Acme Ltd", "   "
    )
    assert receipt.accepted is False
    assert "why" in receipt.error


def test_an_accepted_proposal_records_what_it_looked_at(box):
    """
    Attribution is the product. "The match rate went up" is worthless; "this named
    evidence, gathered by these calls, changed this verdict" is the claim.
    """
    batch, out, tb = box
    tb.get_exception("bank_txn_0103")
    tb.lookup_payer_relationship("ACME INDUSTRIAL SU")
    receipt = tb.propose_evidence(
        "bank_txn_0103", "authorised_payer_for", "Deccan Pharma Distributors",
        "the register lists this payer as group treasury for that customer",
    )
    assert receipt.accepted is True
    assert any("get_exception" in c for c in receipt.proposal.tool_calls)
    assert any("lookup_payer_relationship" in c for c in receipt.proposal.tool_calls)


# --------------------------------------------------------------------------
# The register lookup
# --------------------------------------------------------------------------
def test_the_lookup_matches_a_truncated_bank_name(box):
    """
    Bank exports truncate to a fixed field width, so equality would never fire on real
    statement text. `'ACME INDUSTRIAL SU'` and the register's full legal name are the
    same counterparty, and folding plus prefix matching is what makes the tool usable.
    """
    assert _normalise("ACME INDUSTRIAL SU") == "ACME INDUSTRIAL SU"
    assert _normalise("Acme Industrial Supplies Private Limited").startswith(
        _normalise("ACME INDUSTRIAL SU")
    )


def test_a_missing_register_entry_says_it_is_not_evidence_of_anything(box):
    """
    The register is deliberately incomplete, so `found=False` must not read as "this
    payer is unauthorised". An investigator that treated absence as disproof would
    manufacture confident wrong conclusions from a gap in reference data.
    """
    _, _, tb = box
    rel = tb.lookup_payer_relationship("Definitely Not On The Register Pvt Ltd")
    assert rel.found is False
    assert "not evidence" in rel.note


def test_the_register_is_absent_gracefully(box):
    """A batch generated before side D existed is still a valid batch."""
    batch, out, _ = box
    bare = Toolbox(batch.inputs, out, ())
    rel = bare.lookup_payer_relationship("Anyone")
    assert rel.found is False
    assert "no authorised-payer register" in rel.note


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------
def test_the_ledger_is_append_only():
    """
    A second, different fact on the same channel would make the verdict change
    unattributable -- which is the one thing the ledger exists to preserve.
    """
    led = EvidenceLedger()
    first = EvidenceProposal(
        "bank_txn_0001", EvidenceField.AUTHORISED_PAYER_FOR, "Acme Ltd", "register"
    )
    second = EvidenceProposal(
        "bank_txn_0001", EvidenceField.AUTHORISED_PAYER_FOR, "Other Ltd", "register"
    )
    from recon.agent import EvidenceReceipt

    assert led.add(EvidenceReceipt(True, first)).accepted is True
    replaced = led.add(EvidenceReceipt(True, second))
    assert replaced.accepted is False
    assert "append-only" in replaced.error
    # The map carries the FIRST fact and only the first. Each entry is
    # `{value, amount_paise}` rather than a bare string because five of the eight
    # channels carry money and the amount has to travel with the token that makes it a
    # deduction; `match._facts_for` still accepts the flat form, so a ledger written
    # before that change replays unchanged.
    assert led.as_evidence_map() == {
        "bank_txn_0001": {
            "authorised_payer_for": {"value": "Acme Ltd", "amount_paise": None}
        }
    }


def test_rejections_are_kept_not_dropped():
    """
    A rejected proposal is the most interesting row in the ledger: the agent tried to
    assert something the boundary would not carry. A run that discarded those would hide
    exactly the behaviour worth auditing.
    """
    from recon.agent import EvidenceReceipt

    led = EvidenceLedger()
    led.add(EvidenceReceipt(False, error="value looks like a record identifier"))
    assert len(led.rejected) == 1
    assert led.as_evidence_map() == {}


def test_an_empty_ledger_is_the_null_control_arm(box):
    """
    `{}` must reach the engine as "no evidence" so the null-agent arm is a
    byte-identical run rather than a nearly identical one.
    """
    batch, out, _ = box
    assert EvidenceLedger().as_evidence_map() == {}
    a = match_once(batch.inputs)
    b = match_once(batch.inputs, evidence=EvidenceLedger().as_evidence_map())
    assert a.assignment_map == b.assignment_map
    assert [r.bank_txn_id for r in a.refusals] == [r.bank_txn_id for r in b.refusals]


def test_the_ledger_round_trips_through_disk(tmp_path):
    """The agent makes network calls, so a run killed halfway must not lose paid-for evidence."""
    from recon.agent import EvidenceReceipt

    led = EvidenceLedger()
    led.add(EvidenceReceipt(True, EvidenceProposal(
        "bank_txn_0007", EvidenceField.AUTHORISED_PAYER_FOR, "Acme Ltd",
        "register hit", ("get_exception(bank_txn_0007)",),
    )))
    path = led.write(tmp_path / "evidence.json")

    reloaded = EvidenceLedger.read(path)
    assert reloaded.as_evidence_map() == led.as_evidence_map()
    assert reloaded.accepted[0].tool_calls == ("get_exception(bank_txn_0007)",)
    assert EvidenceLedger.read(tmp_path / "absent.json").as_evidence_map() == {}


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------
def test_the_agent_is_blocked_from_the_ground_truth_directory():
    """
    The audit hook was written as deny-by-default over everything under `recon.` except
    the generator, so it covered `recon.agent` before that package existed. Proven by
    planting a probe rather than by reading the rule.
    """
    from recon import _frame_is_forbidden

    assert _frame_is_forbidden("recon.agent")
    assert _frame_is_forbidden("recon.agent.tools")

    probe = cfg.ROOT / "src" / "recon" / "agent" / "_probe.py"
    probe.write_text(
        "import config as cfg\n"
        "def read_it():\n"
        "    return (cfg.TRUTH_DIR / 'ground_truth.json').read_text()\n",
        encoding="utf-8",
    )
    try:
        import importlib

        mod = importlib.import_module("recon.agent._probe")
        with pytest.raises(PermissionError, match="isolation violated"):
            mod.read_it()
    finally:
        probe.unlink(missing_ok=True)


def test_the_register_is_not_reachable_from_the_engine():
    """
    Side D is reference data, but the engine still must not go and get it: evidence
    arrives asserted, through `match_once(evidence=...)`. A matcher that read the
    register itself would dissolve the boundary the whole agentic design rests on.
    """
    import inspect

    from recon.engine import fellegi_sunter, match, tier1_reference, tier2_amount_date

    for module in (match, fellegi_sunter, tier1_reference, tier2_amount_date):
        source = inspect.getsource(module)
        assert "payer_directory" not in source, module.__name__
        assert "load_payer_directory" not in source, module.__name__
