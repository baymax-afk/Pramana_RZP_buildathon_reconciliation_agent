"""
The deterministic evidence-validation layer: is this fact even about this batch?

`EvidenceProposal.validate` asks whether a proposal is well-FORMED. This asks whether it
is well-FOUNDED, which needs the inputs: does the transaction exist, is the cited record
reachable from it, does the asserted amount agree with what the ledger already says.

**Why the split matters.** A proposal that says `tds_confirmed = withheld, 87,300 paise`
is perfectly well-formed and may still be nonsense -- the invoice it cites may not exist,
may belong to a customer who has nothing to do with this credit, or may record a different
TDS figure. Every one of those is checkable against data the engine already holds, and
none of them is checkable from the proposal alone. Shape checks that pass are not
evidence; they are the absence of one kind of mistake.

**What this module must never do, and how that is enforced.** It never reaches a verdict.
It does not decide whether a match should be posted, does not read an assignment map, does
not compare candidates, and does not import the matcher. `tests/test_agent_specialists.py`
asserts that by parsing this module's AST -- the same technique `tests/test_isolation.py`
uses on the boundary -- because a validation layer that started making decisions would be
a second decision-maker with none of the first one's verification behind it.

**The clock.** Staleness is measured against the batch's own latest bank date, not
`datetime.now()`. Two reasons, and the second is the important one: a wall clock would
make every test race the calendar, and -- more to the point -- "is this evidence stale"
means "was it read after the money moved", which is a question about the batch. A register
lookup performed a year after this statement closed is stale relative to the statement no
matter what today's date happens to be.

**A rejection is an answer, not an error.** Every path returns an `EvidenceReceipt` with
the reason, the ledger keeps it, and the run reports it. A rejected proposal is the most
interesting row in the ledger: it is the agent having tried to assert something the
boundary would not carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field
from datetime import date, timedelta

import config as cfg

from ..engine import tier2_amount_date
from ..engine.results import MatchOutput
from ..schemas import PayerAuthorisation, ReconInputs
from .schemas import EvidenceField, EvidenceProposal, EvidenceReceipt, SourceType

# How far before a credit an external record may have been read and still describe it.
# Generous on purpose: this is a guard against evidence gathered against a different
# period, not a freshness SLA. A payer register updated last quarter is fine; one read
# before the invoice existed is not.
EVIDENCE_MAX_AGE_DAYS = 400

# How far outside the configured lookback a confirmed settlement date may reach. Small on
# purpose: the point of the channel is to explain ONE credit whose settlement drifted, not
# to give an agent a way to widen `LOOKBACK_DAYS` a day at a time -- which would be tuning
# a threshold, the second of the five things `docs/AGENTIC.md` says an agent may never do.
EVIDENCE_WINDOW_SLACK_DAYS = 10


@dataclass(slots=True)
class EvidenceContext:
    """
    Everything the checks below are allowed to read.

    Constructed from `ReconInputs`, the baseline `MatchOutput` and the payer register --
    the same three things the toolbox holds, and no more. In particular it holds no
    ground truth and no scoring; `recon.agent` sits inside the isolation boundary and the
    audit hook in `recon/__init__.py` enforces it whether this docstring says so or not.
    """

    inputs: ReconInputs
    out: MatchOutput
    directory: tuple[PayerAuthorisation, ...] = ()
    _txn: dict = _field(default_factory=dict, init=False)
    _pay: dict = _field(default_factory=dict, init=False)
    _inv: dict = _field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._txn = {t.id: t for t in self.inputs.bank_txns}
        self._pay = {p.id: p for p in self.inputs.payments}
        self._inv = {i.invoice_no: i for i in self.inputs.invoices}

    @property
    def latest_bank_date(self) -> date | None:
        """
        The batch's own horizon, or `None` when the batch dates nothing.

        `None` rather than `date.today()`, and the difference matters more than it looks.
        A fallback to the wall clock would mean that on a batch with no dates -- an empty
        one, or a malformed statement -- staleness would suddenly be measured against the
        day the test happened to run, which is precisely the race this module is built to
        avoid. A horizon that does not exist is a question that cannot be asked, and
        `_check_freshness` says so instead of substituting a different question.
        """
        dates = [
            date.fromisoformat(t.txn_date) for t in self.inputs.bank_txns if t.txn_date
        ]
        return max(dates) if dates else None

    def txn(self, txn_id: str):
        return self._txn.get(txn_id)

    def invoice(self, invoice_no: str):
        return self._inv.get(invoice_no)

    def payment(self, payment_id: str):
        return self._pay.get(payment_id)

    def pool_ids(self, txn_id: str) -> set[str]:
        """
        The payments the engine itself considered for this credit.

        `tier2_amount_date.candidate_pool` rather than a re-implementation, for the same
        reason `tools.py` uses it: a second definition of which payments belong to a
        credit is a second source of truth about money.
        """
        t = self.txn(txn_id)
        if t is None:
            return set()
        return {p.id for p in tier2_amount_date.candidate_pool(t, self.inputs.payments, set())}

    def invoices_for(self, txn_id: str) -> set[str]:
        """Invoice numbers reachable from this credit through its candidate pool."""
        out = set()
        for pid in self.pool_ids(txn_id):
            p = self._pay.get(pid)
            no = p.notes.get("invoice_no") if p else None
            if no:
                out.add(no)
        return out


def _reject(proposal: EvidenceProposal, reason: str) -> EvidenceReceipt:
    return EvidenceReceipt(False, proposal=proposal, error=reason)


def validate_proposal(
    proposal: EvidenceProposal,
    ctx: EvidenceContext,
    *,
    already: set[tuple[str, str]] | None = None,
) -> EvidenceReceipt:
    """
    Every check, in order, cheapest and most fundamental first.

    `already` is the set of `(bank_txn_id, field)` pairs the ledger has accepted. Passed
    in rather than read off a ledger so this function stays a pure predicate over its
    arguments -- the same property that makes `match_once` testable.
    """
    # 1 -- shape. Delegated, so a proposal replayed from disk gets the same checks as one
    #      arriving from a tool call.
    try:
        proposal.validate()
    except ValueError as e:
        return _reject(proposal, str(e))

    # 2 -- the transaction exists.
    txn = ctx.txn(proposal.bank_txn_id)
    if txn is None:
        return _reject(
            proposal,
            f"no bank transaction {proposal.bank_txn_id!r} in this batch. Evidence "
            f"about a credit that is not here cannot change anything and would sit in "
            f"the ledger looking like it had.",
        )

    # 3 -- duplicate assertion on the same channel.
    if already and (proposal.bank_txn_id, proposal.field.value) in already:
        return _reject(
            proposal,
            f"{proposal.bank_txn_id} already has a {proposal.field.value} assertion. "
            f"The ledger is append-only: a second, different fact about the same "
            f"channel would make the verdict change unattributable.",
        )

    # 4 -- a source, and one that resolves.
    receipt = _check_source(proposal, ctx)
    if receipt is not None:
        return receipt

    # 5 -- read at a time that could describe this batch.
    receipt = _check_freshness(proposal, ctx)
    if receipt is not None:
        return receipt

    # 6 -- consistent with what the inputs already say.
    receipt = _check_consistency(proposal, ctx, txn)
    if receipt is not None:
        return receipt

    return EvidenceReceipt(True, proposal=proposal)


def _check_source(
    proposal: EvidenceProposal, ctx: EvidenceContext
) -> EvidenceReceipt | None:
    """
    An agent may not assert a fact without saying where it came from.

    `MODEL_ASSERTION` is permitted and is not a way round this: it is a citation that says
    "nothing external", it is reported in its own row by `agent/sources.py`, and it is
    barred from the channels where an uncited claim would move money. A model may
    recognise that a spelling variant names the same company. It may not conclude, from
    nothing, that ₹600 of TDS was withheld.
    """
    if proposal.source_type is SourceType.MODEL_ASSERTION:
        if proposal.declared_deduction_paise:
            return _reject(
                proposal,
                "a declared deduction needs an external source. This one cites none, so "
                "there is no record anybody could re-read to check it -- and it would "
                "change what the arithmetic is asked to explain. Cite the payment, the "
                "invoice or the statement line the figure came from.",
            )
        return None

    ref = (proposal.source_ref or "").strip()
    if not ref:
        return _reject(
            proposal,
            f"source_type is {proposal.source_type.value!r} and source_ref is empty. "
            f"Naming the kind of source without naming the record is an unfalsifiable "
            f"citation.",
        )

    # The cited record must exist AND be reachable from this credit. Existing is not
    # enough: a true fact about an unrelated invoice is evidence about a different pair,
    # which `fellegi_sunter` already refuses to weigh and this refuses to record.
    if proposal.source_type is SourceType.INVOICE_LEDGER:
        if ctx.invoice(ref) is None:
            return _reject(proposal, f"no invoice {ref!r} in the ledger")
        if ref not in ctx.invoices_for(proposal.bank_txn_id):
            return _reject(
                proposal,
                f"invoice {ref!r} exists but is not reachable from "
                f"{proposal.bank_txn_id} -- no payment in this credit's candidate pool "
                f"settles it. That is evidence about a different pair.",
            )
    elif proposal.source_type is SourceType.PAYMENT_RECORD:
        if ctx.payment(ref) is None:
            return _reject(proposal, f"no payment {ref!r} in this batch")
        if ref not in ctx.pool_ids(proposal.bank_txn_id):
            return _reject(
                proposal,
                f"payment {ref!r} exists but is not in {proposal.bank_txn_id}'s "
                f"candidate pool, so a fact about it cannot be a fact about this credit.",
            )
    elif proposal.source_type is SourceType.BANK_STATEMENT:
        refs = {t.ref_no for t in ctx.inputs.bank_txns if t.ref_no}
        if ref not in refs:
            return _reject(proposal, f"no statement line carries the reference {ref!r}")
    elif proposal.source_type is SourceType.PAYER_REGISTER:
        names = {row.payer_name for row in ctx.directory}
        if ref not in names:
            return _reject(
                proposal,
                f"{ref!r} is not a payer in the authorised-payer register. The register "
                f"is not exhaustive, so absence is not disproof -- but it does mean this "
                f"citation names no row anybody can re-read.",
            )
    return None


def _check_freshness(
    proposal: EvidenceProposal, ctx: EvidenceContext
) -> EvidenceReceipt | None:
    if proposal.source_type is SourceType.MODEL_ASSERTION and not proposal.retrieved_at:
        return None
    if not proposal.retrieved_at:
        return _reject(
            proposal,
            "an external citation with no retrieval date cannot be told apart from one "
            "read against a different period.",
        )
    try:
        read_on = date.fromisoformat(proposal.retrieved_at)
    except ValueError:
        return _reject(
            proposal, f"retrieved_at {proposal.retrieved_at!r} is not an ISO date"
        )
    horizon = ctx.latest_bank_date
    if horizon is None:
        # Nothing on the statement is dated, so there is no period to be stale against.
        # The citation still had to exist and resolve; this one check has nothing to
        # compare, and inventing a comparison would be worse than skipping it.
        return None
    if read_on < horizon - timedelta(days=EVIDENCE_MAX_AGE_DAYS):
        return _reject(
            proposal,
            f"read on {read_on.isoformat()}, more than {EVIDENCE_MAX_AGE_DAYS} days "
            f"before this batch closed on {horizon.isoformat()}. A record that old "
            f"describes a different period.",
        )
    return None


def _corroborate(
    proposal: EvidenceProposal, ctx: EvidenceContext, declared: int
) -> EvidenceReceipt | None:
    """
    Does the cited record STATE this figure, and is the engine not already using it?

    Two rules, and the project learned both the expensive way inside one afternoon.

    **The record must state the AMOUNT, not merely the direction.** The first version of
    the invoice specialist read the engine's residual on a refused candidate and asserted
    it as a short payment. Every step was defensible; the composite was circular, because
    a figure taken from the gap will always close the gap. It bought four payments of
    coverage and two wrong postings -- precision 1.0000 -> 0.9854.

    The obvious patch was to require the ledger to corroborate: only assert a shortfall
    against an invoice the ledger marks `part_settled`. That is a real constraint and it
    was still not enough. `part_settled` says a customer short-paid; it does not say by
    how much, so the amount was still coming from the residual. The holdout batch caught
    what the primary one had stopped showing: precision 1.0000 -> 0.9913, one wrong
    posting, same mechanism wearing a corroborating status flag.

    So the rule is the strict one. A deduction is admissible when a record NAMES the
    figure. `Invoice.tds_amount` names one. `Payment.amount_refunded` names one. Nothing
    else in these three sides does -- there is no settled-to-date column, no credit-note
    table, no bank-charge field.

    **And a figure the engine already subtracts is a restatement, not evidence.** Both
    fields that name an amount are read by `fees.known_deductions` already, so asserting
    either would subtract it twice and manufacture a gap rather than close one.

    **The consequence, stated rather than engineered around: on these batches no
    deduction channel can be accepted.** The machinery is built, validated and tested; the
    data cannot feed it. That is a fact about the generator's ledger, not a fault in the
    channel, and it changes the moment a real ERP export carries a credit-note line or a
    settled-to-date column. Reporting it is worth more than a coverage number bought by
    relaxing this function.
    """
    if proposal.source_type is SourceType.INVOICE_LEDGER:
        inv = ctx.invoice(proposal.source_ref)
        if inv is None:  # pragma: no cover -- existence checked in _check_source
            return _reject(proposal, f"no invoice {proposal.source_ref!r}")
        if proposal.field is EvidenceField.TDS_CONFIRMED:
            return _reject(
                proposal,
                f"invoice {inv.invoice_no} records {inv.tds_amount}p of TDS and the "
                f"engine already subtracts it -- `fees.known_deductions` reads this "
                f"field. Asserting it again would deduct it twice.",
            )
        return _reject(
            proposal,
            f"the invoice ledger records {inv.invoice_no} as {inv.status!r} with a "
            f"{inv.gross_amount}p gross and {inv.tds_amount}p TDS, and states no other "
            f"amount. A deduction needs a figure a record NAMES: a status tells you a "
            f"shortfall happened, not how large it was, so an amount asserted alongside "
            f"it comes from the gap it closes rather than from the ledger.",
        )

    if proposal.source_type is SourceType.PAYMENT_RECORD:
        pay = ctx.payment(proposal.source_ref)
        if pay is None:  # pragma: no cover -- existence checked in _check_source
            return _reject(proposal, f"no payment {proposal.source_ref!r}")
        recorded = pay.amount_refunded or 0
        if not recorded:
            return _reject(
                proposal,
                f"payment {pay.id} records no refund, so the gateway record does not "
                f"corroborate money being netted out of this settlement.",
            )
        return _reject(
            proposal,
            f"payment {pay.id} records a {recorded}p refund and the engine already "
            f"subtracts it -- `fees.known_deductions` reads `amount_refunded`. "
            f"Asserting it again would deduct it twice; asserting a different figure "
            f"would contradict the record.",
        )

    # The statement carries a date, a narration, a reference and two amounts. It has no
    # column for a bank charge, so a charge asserted against it cites a record that
    # cannot speak to it.
    return _reject(
        proposal,
        f"a {proposal.source_type.value} record names no deducted amount, so it cannot "
        f"corroborate one. A deduction is admissible when a record states the figure.",
    )


def _check_consistency(
    proposal: EvidenceProposal, ctx: EvidenceContext, txn
) -> EvidenceReceipt | None:
    """
    Cross-check against what the inputs already say, wherever they say anything.

    **This is the check that makes the deduction channels safe to admit at all.** TDS is
    on the invoice. A refund is on the payment. Both are figures the engine already reads,
    so an agent asserting a third figure is either confirming what is known -- harmless,
    and the engine would have used it anyway -- or contradicting it, which is the case
    worth refusing loudly. Only where the ledger genuinely records nothing (a credit note
    cut after the invoice, a bank charge outside the fee model) does an assertion add
    information, and there the residual bounds it.
    """
    field = proposal.field
    amount = proposal.amount_paise
    declared = proposal.declared_deduction_paise

    if field is EvidenceField.TDS_CONFIRMED and amount is not None:
        inv = ctx.invoice(proposal.source_ref)
        if inv is not None and inv.tds_amount != amount:
            return _reject(
                proposal,
                f"invoice {inv.invoice_no} records TDS of {inv.tds_amount}p and this "
                f"asserts {amount}p. The ledger already carries this figure and the "
                f"engine already subtracts it; a different one is a contradiction, not "
                f"new evidence.",
            )

    if field is EvidenceField.REFUND_STATUS and amount is not None:
        pay = ctx.payment(proposal.source_ref)
        if pay is not None and (pay.amount_refunded or 0) not in (0, amount):
            return _reject(
                proposal,
                f"payment {pay.id} records a refund of {pay.amount_refunded}p and this "
                f"asserts {amount}p. The engine already subtracts the recorded figure.",
            )
        if pay is not None and amount > pay.amount:
            return _reject(
                proposal,
                f"a refund of {amount}p exceeds the {pay.amount}p payment it is "
                f"refunding.",
            )

    if field is EvidenceField.SETTLEMENT_DATE_CONFIRMED:
        try:
            settled = date.fromisoformat(proposal.value)
        except ValueError:  # pragma: no cover -- shape check already caught this
            return _reject(proposal, f"{proposal.value!r} is not an ISO date")
        credited = date.fromisoformat(txn.txn_date)
        if settled > credited:
            return _reject(
                proposal,
                f"a settlement dated {settled.isoformat()} cannot explain a credit that "
                f"arrived on {credited.isoformat()}: money does not reach the bank "
                f"before it is settled.",
            )
        if (credited - settled).days > cfg.LOOKBACK_DAYS + EVIDENCE_WINDOW_SLACK_DAYS:
            return _reject(
                proposal,
                f"a settlement dated {settled.isoformat()} is "
                f"{(credited - settled).days} days before this credit. Widening the "
                f"window that far is not evidence about one credit, it is a change to "
                f"the engine's search bound -- which an agent may not make.",
            )

    # ---- corroboration: the cited record must ITSELF say money was kept back ----
    #
    # This is the check the eight-channel expansion turns on, and it was added because
    # the first version of the invoice specialist failed without it -- in exactly the way
    # `docs/AGENTIC.md` predicts.
    #
    # That specialist asked the engine for the residual on a refused candidate and
    # asserted it as a short payment. Every individual step was defensible and the
    # composite was circular: it manufactured precisely the deduction needed to make the
    # refusal go away, cited an invoice that recorded nothing of the kind, and bought
    # four payments of coverage at the cost of two wrong postings. Precision went
    # 1.0000 -> 0.9854. That is "explain your way past a refusal", the fourth of the five
    # prohibitions, arriving as arithmetic rather than as prose.
    #
    # The rule that stops it: a deduction is a claim about a RECORD, so the record has to
    # corroborate it. Not "the invoice exists" -- the invoice must say it was
    # part-settled. Not "the payment exists" -- the payment must carry a refund. An agent
    # may go and find a fact the engine did not have; it may not derive one from the gap
    # it wants to close, because a figure derived from the residual will always close the
    # residual.
    if declared:
        receipt = _corroborate(proposal, ctx, declared)
        if receipt is not None:
            return receipt

    # A deduction may not exceed the credit it claims to explain. This is the crude bound
    # that stops an asserted figure from rewriting the arithmetic wholesale: whatever was
    # kept back, the bank still credited what it credited.
    if declared and declared >= txn.credit:
        return _reject(
            proposal,
            f"a deduction of {declared}p is not less than the {txn.credit}p credit it "
            f"claims to explain. A deduction accounts for the gap between what was owed "
            f"and what arrived; one this large is a different fact about a different "
            f"credit.",
        )

    return None

