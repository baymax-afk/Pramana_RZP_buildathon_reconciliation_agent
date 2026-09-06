"""
Everything crossing the agent boundary, as typed dataclasses.

**No tool returns free text the model has to re-parse.** Every result below serialises
to JSON with named fields, so a model that misreads one produces a validation error
rather than a plausible-looking mistake three steps downstream. The project already got
this right for the narration tier (`llm/interface.py`); this is the same discipline one
level up, where the surface is much wider.

**And exactly one type can be written.** `EvidenceProposal` is the only thing an agent
can hand back that changes anything, and what it can carry is deliberately narrow --
see its docstring for why an enum rather than a string, and why the value is validated
against the shape of a payment id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class EvidenceField(Enum):
    """
    What an agent is permitted to assert.

    **An enum, not a string.** A free-text field name would let an agent invent a
    channel the engine never agreed to weigh, and the failure would be silent -- the
    engine would ignore it and the agent would report success. Adding a member here is a
    deliberate code change with a test, which is the point.

    **Eight channels, and they are not equally consequential.** `AUTHORISED_PAYER_FOR`
    enters the Fellegi-Sunter name comparison, where it can at most stop a contradiction
    veto from firing on a credit whose arithmetic ALREADY balanced. The five deduction
    fields are different in kind: they enter `fees.known_deductions`, which changes what
    the engine expects the bank to have credited, and therefore changes what the subset
    search can find. That is the amount channel -- the one this engine treats as primary
    and the one `docs/AGENTIC.md` is most careful about.

    They are admitted anyway, because the alternative is worse and the project has
    already proved it: `DEFECT_LOG` 2026-09-02-05 item 4 is a batch where the money was
    deducted and recorded NOWHERE, five credits were refused on arithmetic, and the fix
    was not to loosen the tolerance but to tell the engine where the money went. A
    deduction an agent has gone and found, with a source, is the same fix arriving by a
    different route.

    What makes it safe is not the field list. It is that every one of these must survive
    `agent/validate.py` -- which checks the asserted amount against the ledger's own
    figure wherever the ledger has one -- and that the engine then re-runs and reaches
    its own verdict. An asserted deduction cannot post a match; it can only change what
    the arithmetic is asked to explain.
    """

    AUTHORISED_PAYER_FOR = "authorised_payer_for"
    REFUND_STATUS = "refund_status"
    TDS_CONFIRMED = "tds_confirmed"
    CREDIT_NOTE_CONFIRMED = "credit_note_confirmed"
    SETTLEMENT_DATE_CONFIRMED = "settlement_date_confirmed"
    BANK_CHARGE_CONFIRMED = "bank_charge_confirmed"
    CHARGEBACK_STATUS = "chargeback_status"
    INVOICE_PART_PAYMENT = "invoice_part_payment"


class SourceType(Enum):
    """
    Where a fact came from.

    **Required, and `MODEL_ASSERTION` is a real option rather than a loophole.** An agent
    that concluded something from the exception and the pool alone may say so -- what it
    may not do is have that indistinguishable from a register lookup. `agent/sources.py`
    already makes this argument for the per-source table; this makes it a field on the
    proposal so the claim travels with the fact instead of being reconstructed from the
    tool calls afterwards.
    """

    PAYER_REGISTER = "payer_register"
    INVOICE_LEDGER = "invoice_ledger"
    PAYMENT_RECORD = "payment_record"
    BANK_STATEMENT = "bank_statement"
    MODEL_ASSERTION = "model_assertion"


@dataclass(frozen=True, slots=True)
class FieldRule:
    """
    What one channel's value is allowed to look like.

    `kind` is what `value` carries; `amount_paise` carries money, always, and never
    `value`. That split is not tidiness -- it is what makes "reject a value that looks
    like a score" a one-line check instead of a judgement call, because a channel whose
    value is drawn from a fixed token set cannot smuggle a number through at all.
    """

    kind: str                                   # "name" | "token" | "date"
    tokens: frozenset[str] = frozenset()
    amount_required_for: frozenset[str] = frozenset()
    # Tokens that make this a DECLARED DEDUCTION -- money the bank did not credit
    # because somebody kept it. `fees.expected_credit_interval` subtracts the amount.
    deduction_for: frozenset[str] = frozenset()
    note: str = ""


_FIELD_RULES: dict[EvidenceField, FieldRule] = {
    EvidenceField.AUTHORISED_PAYER_FOR: FieldRule(
        kind="name",
        note="the LEDGER's spelling of the customer this payer is authorised to settle for",
    ),
    EvidenceField.REFUND_STATUS: FieldRule(
        kind="token",
        tokens=frozenset({"none", "partial", "full"}),
        amount_required_for=frozenset({"partial", "full"}),
        deduction_for=frozenset({"partial", "full"}),
        note="a refund netted out of the settlement before the bank credited it",
    ),
    EvidenceField.TDS_CONFIRMED: FieldRule(
        kind="token",
        tokens=frozenset({"withheld", "not_withheld"}),
        amount_required_for=frozenset({"withheld"}),
        deduction_for=frozenset({"withheld"}),
        note="tax deducted at source by the payer, confirmed against the invoice",
    ),
    EvidenceField.CREDIT_NOTE_CONFIRMED: FieldRule(
        kind="token",
        tokens=frozenset({"issued", "none"}),
        amount_required_for=frozenset({"issued"}),
        deduction_for=frozenset({"issued"}),
        note="a credit note issued after the invoice was cut",
    ),
    EvidenceField.SETTLEMENT_DATE_CONFIRMED: FieldRule(
        kind="date",
        note="the date the gateway actually settled, when it is outside the window",
    ),
    EvidenceField.BANK_CHARGE_CONFIRMED: FieldRule(
        kind="token",
        tokens=frozenset({"levied", "none"}),
        amount_required_for=frozenset({"levied"}),
        deduction_for=frozenset({"levied"}),
        note="a bank charge deducted from the credit, outside the gateway fee model",
    ),
    EvidenceField.CHARGEBACK_STATUS: FieldRule(
        kind="token",
        tokens=frozenset({"none", "raised", "won", "lost"}),
        note="the state of a dispute; read by the reversal ledger, not by the matcher",
    ),
    EvidenceField.INVOICE_PART_PAYMENT: FieldRule(
        kind="token",
        tokens=frozenset({"paid_in_full", "short_paid"}),
        amount_required_for=frozenset({"short_paid"}),
        deduction_for=frozenset({"short_paid"}),
        note="a customer settling less than the invoice, confirmed against the ledger",
    ),
}


def rule_for(field: EvidenceField) -> FieldRule:
    """The contract for one channel. `KeyError` is correct: an unruled field is a bug."""
    return _FIELD_RULES[field]


def deduction_paise(field: EvidenceField, value: str, amount_paise: int | None) -> int:
    """
    What this fact says the bank kept back, in paise. Zero for anything that is not a
    deduction, so a caller can sum over a whole bundle without asking which is which.
    """
    rule = _FIELD_RULES[field]
    if value in rule.deduction_for and amount_paise:
        return int(amount_paise)
    return 0


# Shapes a value must never have. Used to REJECT, not to find: see
# EvidenceProposal.validate.
#
# The first four are record identifiers, and the 2026-09-03 audit §5 is why they are here
# rather than left to the absence of an id field -- an invoice number is a payment
# identifier with one hop of indirection, so "carries no payment id" is not the same as
# "cannot name one".
#
# The rest were added with the eight-channel expansion. A wider surface is a wider set of
# ways to say something the boundary does not carry: a verdict word smuggles in a
# decision, and a bare number smuggles in a score or an amount that belongs in
# `amount_paise` where it can be checked against the ledger.
_ID_SHAPES = (
    re.compile(r"^pay_", re.IGNORECASE),
    re.compile(r"^order_", re.IGNORECASE),
    re.compile(r"^bank_txn", re.IGNORECASE),
    re.compile(r"^setl_", re.IGNORECASE),
    re.compile(r"^cand", re.IGNORECASE),
    re.compile(r"^[a-z]{4}[a-z0-9]{8,}$", re.IGNORECASE),   # UTR-shaped
)

# Words that are a decision rather than a fact. An agent that writes one of these into an
# evidence channel is not widening the engine's evidence, it is trying to answer for it.
_VERDICT_WORDS = frozenset(
    {
        "assign", "assigned", "refuse", "refused", "refusal", "no_candidate",
        "match", "matched", "unmatched", "approve", "approved", "reject", "rejected",
        "post", "posted", "confirm_match", "accept", "accepted",
    }
)

# A value that is only a number. Money belongs in `amount_paise`, where the validation
# layer can check it against the ledger; a bare number in a free-text channel is either
# an amount nobody checked or a score the engine never agreed to read.
_BARE_NUMBER = re.compile(r"^[-+]?[0-9][0-9,._]*%?$")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class EvidenceProposal:
    """
    The only write in the agent's whole surface.

    **It carries no payment id, no candidate, no score and no verdict** -- the same
    structural argument `NarrationFields` makes for the narration tier, applied one
    level up where it matters more, because an investigator sees far more of the batch
    than a narration parser does.

    That argument needed strengthening here, and an external audit is why. It showed
    the narration tier's boundary claim was one hop from false: a `merchant_ref` reaches
    a payment id through `ReferenceIndex`, and tier 1 outranks everything in
    `evidence_key`, so a plausible invoice number selects a payment and wins contested
    money. The lesson is that "carries no payment id" is not the same as "cannot name
    one". So `validate` rejects a value shaped like any identifier in the batch, and
    `authorised_payer_for` is compared against CUSTOMER NAMES only -- a channel that
    cannot index a payment even indirectly.

    `tool_calls` records what the agent looked at to reach this. It is not decoration:
    it is what makes a verdict change attributable to named evidence rather than to an
    agent's opinion, and it is what `run.py agent` reports.
    """

    bank_txn_id: str
    field: EvidenceField
    value: str
    rationale: str
    tool_calls: tuple[str, ...] = ()
    # Money, when the channel carries any. Separate from `value` on purpose -- see
    # `FieldRule` -- so an amount is always an integer the validation layer can compare
    # against the ledger, and `value` can never be a number at all.
    amount_paise: int | None = None
    # WHERE the fact came from, and WHICH record. An agent that cannot cite one is
    # asserting from nothing, and `agent/validate.py` refuses it: the whole claim this
    # architecture makes is "this named piece of evidence changed this verdict", and a
    # fact with no source cannot support the first half of that sentence.
    source_type: SourceType = SourceType.MODEL_ASSERTION
    source_ref: str = ""
    # When the source was read, ISO date. Checked against the batch's own dates rather
    # than a wall clock -- see `agent/validate.py` for why a clock here would make the
    # suite race.
    retrieved_at: str = ""

    def validate(self) -> None:
        """
        Raise if this proposal is trying to do something it is not allowed to do.

        **Context-free checks only.** Whether the value is well-formed for its channel,
        and whether it is shaped like something the boundary refuses to carry. Whether it
        is TRUE of this batch -- that the transaction exists, that the cited invoice is
        reachable from this credit, that the asserted TDS matches the ledger's -- needs
        the inputs, and lives in `agent/validate.py`. Split so that a proposal arriving
        from a replayed ledger gets the same shape checks as one arriving from a tool.
        """
        if not isinstance(self.field, EvidenceField):
            raise ValueError(
                f"field must be an EvidenceField, got {self.field!r}. A free-text "
                f"channel the engine never agreed to weigh would be ignored silently."
            )
        rule = _FIELD_RULES[self.field]
        value = (self.value or "").strip()
        if not value:
            raise ValueError("an evidence proposal with no value asserts nothing")
        if len(value) > 120:
            raise ValueError(f"value is {len(value)} chars; cap is 120")

        for shape in _ID_SHAPES:
            if shape.match(value):
                raise ValueError(
                    f"value {value!r} looks like a record identifier. An identifier "
                    f"would let the agent name a specific record, which is the one "
                    f"thing the trust boundary exists to prevent -- an invoice number "
                    f"resolves to a payment in one hop, so a value that merely LOOKS "
                    f"like one can select a record without ever carrying its id. Cite "
                    f"the record in `source_ref` instead, where it is checked rather "
                    f"than weighed."
                )
        if value.casefold().replace(" ", "_") in _VERDICT_WORDS:
            raise ValueError(
                f"value {value!r} is a verdict, not a fact. An agent widens the "
                f"engine's evidence; it does not answer for it. Assert what you found, "
                f"and let the engine re-run and decide."
            )
        if _BARE_NUMBER.match(value):
            raise ValueError(
                f"value {value!r} is a bare number. Money goes in `amount_paise`, where "
                f"it is checked against the ledger; a number in this field is either an "
                f"amount nobody verified or a score the engine never agreed to read."
            )

        # ---- per-channel value format ----
        if rule.kind == "token":
            if value not in rule.tokens:
                raise ValueError(
                    f"{value!r} is not a value {self.field.value} carries. Allowed: "
                    f"{', '.join(sorted(rule.tokens))}. A channel with a fixed "
                    f"vocabulary is one the engine can act on without re-parsing it."
                )
        elif rule.kind == "date":
            if not _ISO_DATE.match(value):
                raise ValueError(
                    f"{self.field.value} carries an ISO date (YYYY-MM-DD), got {value!r}"
                )
        elif rule.kind == "name":
            if not any(c.isalpha() for c in value):
                raise ValueError(
                    f"{self.field.value} carries a counterparty name, got {value!r}"
                )

        # ---- the amount ----
        if value in rule.amount_required_for:
            if self.amount_paise is None:
                raise ValueError(
                    f"{self.field.value}={value!r} asserts that money was kept back and "
                    f"does not say how much. An unquantified deduction cannot be "
                    f"checked against the ledger and cannot be subtracted, so it would "
                    f"be accepted and then ignored."
                )
            if not isinstance(self.amount_paise, int) or isinstance(
                self.amount_paise, bool
            ):
                raise ValueError("amount_paise must be an integer number of paise")
            if self.amount_paise <= 0:
                raise ValueError(
                    f"amount_paise is {self.amount_paise}; a deduction is a positive "
                    f"quantity of money and its direction is fixed by the channel"
                )
        elif self.amount_paise is not None:
            raise ValueError(
                f"{self.field.value}={value!r} carries no amount, but "
                f"amount_paise={self.amount_paise} was supplied. A number attached to a "
                f"channel that does not read it is a number nothing will check."
            )

        if not isinstance(self.source_type, SourceType):
            raise ValueError(
                f"source_type must be a SourceType, got {self.source_type!r}"
            )
        if not self.rationale.strip():
            raise ValueError(
                "a proposal must say why. An unexplained verdict change is exactly "
                "what this architecture is arguing against."
            )

    @property
    def declared_deduction_paise(self) -> int:
        """What this fact says the bank kept back. Zero unless it is a deduction."""
        return deduction_paise(self.field, self.value, self.amount_paise)

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "field": self.field.value,
            "value": self.value,
            "amount_paise": self.amount_paise,
            "rationale": self.rationale,
            "source_type": self.source_type.value,
            "source_ref": self.source_ref,
            "retrieved_at": self.retrieved_at,
            "tool_calls": list(self.tool_calls),
        }

    @classmethod
    def from_dict(cls, row: dict) -> "EvidenceProposal":
        """
        Rebuild one from its serialised form -- the ledger's replay path.

        Deliberately NOT lenient about the enums: a hand-edited ledger naming a channel
        this engine does not weigh should fail loudly on read rather than resurrect as a
        proposal nothing acts on.
        """
        return cls(
            bank_txn_id=row["bank_txn_id"],
            field=EvidenceField(row["field"]),
            value=row["value"],
            rationale=row.get("rationale", ""),
            tool_calls=tuple(row.get("tool_calls", ())),
            amount_paise=row.get("amount_paise"),
            source_type=SourceType(row.get("source_type", "model_assertion")),
            source_ref=row.get("source_ref", ""),
            retrieved_at=row.get("retrieved_at", ""),
        )


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    """What the ledger gives back: accepted, or rejected with the reason."""

    accepted: bool
    proposal: EvidenceProposal | None = None
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "proposal": self.proposal.as_dict() if self.proposal else None,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Read-only tool results
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ExceptionView:
    bank_txn_id: str
    txn_date: str
    rupees: float
    narration: str
    reference: str
    category: str
    engine_reason: str
    candidate_payment_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "txn_date": self.txn_date,
            "rupees": self.rupees,
            "narration": self.narration,
            "reference": self.reference,
            "category": self.category,
            "engine_reason": self.engine_reason,
            "candidate_payment_ids": list(self.candidate_payment_ids),
        }


@dataclass(frozen=True, slots=True)
class PoolPayment:
    payment_id: str
    rupees: float
    settled_on: str
    customer_name: str
    invoice_no: str


@dataclass(frozen=True, slots=True)
class PoolView:
    bank_txn_id: str
    window_from: str
    window_to: str
    payments: tuple[PoolPayment, ...]

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "window_from": self.window_from,
            "window_to": self.window_to,
            "payments": [
                {
                    "payment_id": p.payment_id,
                    "rupees": p.rupees,
                    "settled_on": p.settled_on,
                    "customer_name": p.customer_name,
                    "invoice_no": p.invoice_no,
                }
                for p in self.payments
            ],
        }


@dataclass(frozen=True, slots=True)
class SubsetVerdict:
    """
    The ENGINE's answer to "would these payments account for this credit?".

    Computed by `fees.expected_credit_interval` and `fees.fits` -- the matcher's own
    code, not a reimplementation of it. The agent may ask; only the engine answers, and
    it answers with the same arithmetic it would use to post the match. A second
    implementation here would be a second source of truth about money.
    """

    bank_txn_id: str
    payment_ids: tuple[str, ...]
    credit_paise: int
    expected_lo_paise: int
    expected_hi_paise: int
    residual_paise: int
    tolerance_paise: int
    fits: bool
    fee_known_exactly: bool

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "payment_ids": list(self.payment_ids),
            "credit_paise": self.credit_paise,
            "expected_lo_paise": self.expected_lo_paise,
            "expected_hi_paise": self.expected_hi_paise,
            "residual_paise": self.residual_paise,
            "tolerance_paise": self.tolerance_paise,
            "fits": self.fits,
            "fee_known_exactly": self.fee_known_exactly,
        }


@dataclass(frozen=True, slots=True)
class RegisterEntry:
    """
    One row of the register: this customer, under this relationship.

    **A record rather than two parallel tuples, and that is a bug fix.** The first
    version returned `authorised_for` and `relationships` as separate sequences, so a
    caller that selected a customer by one criterion and then read `relationships[0]`
    cited a DIFFERENT row's label. It happened immediately: one payer had three register
    entries, the investigator correctly chose the customer that matched its candidate
    payment, and the rationale it wrote attributed the decision to an unrelated entry.
    An audit trail that names the wrong evidence is worse than one that names none, so
    the pairing is now structural instead of positional.
    """

    customer: str
    relationship: str


@dataclass(frozen=True, slots=True)
class PayerRelation:
    """
    What the authorised-payer register says about one name.

    `found` false is a real answer and the investigator must be able to act on it: the
    register is deliberately incomplete, so "no entry" is the correct outcome for some
    genuine relationships and the honest response is to decline rather than to assert
    anyway.
    """

    queried: str
    found: bool
    matched_payer_name: str = ""
    entries: tuple[RegisterEntry, ...] = ()
    note: str = ""

    @property
    def customers(self) -> tuple[str, ...]:
        return tuple(e.customer for e in self.entries)

    def entry_for(self, customer: str) -> RegisterEntry | None:
        """The row naming this customer -- the only safe way to read a relationship."""
        for e in self.entries:
            if e.customer == customer:
                return e
        return None

    def as_dict(self) -> dict:
        return {
            "queried": self.queried,
            "found": self.found,
            "matched_payer_name": self.matched_payer_name,
            "entries": [
                {"customer": e.customer, "relationship": e.relationship}
                for e in self.entries
            ],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """
    The gateway's record for one payment, as an investigator may see it.

    Note what is NOT here: no confidence, no Fellegi-Sunter weight, no uniqueness margin,
    no permutation stability. All four exist on `Assignment` and none is projected by any
    tool. An investigator that could read how sure the engine was would be investigating
    the engine's opinion instead of the merchant's records, and the first thing it would
    learn is which credits are worth arguing about.
    """

    payment_id: str
    rupees: float
    status: str
    captured: bool
    method: str
    settled_on: str
    customer_name: str
    invoice_no: str
    fee_paise: int | None
    amount_refunded_paise: int
    refund_status: str

    def as_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "rupees": self.rupees,
            "status": self.status,
            "captured": self.captured,
            "method": self.method,
            "settled_on": self.settled_on,
            "customer_name": self.customer_name,
            "invoice_no": self.invoice_no,
            "fee_paise": self.fee_paise,
            "amount_refunded_paise": self.amount_refunded_paise,
            "refund_status": self.refund_status,
        }


@dataclass(frozen=True, slots=True)
class BankLineView:
    """
    One statement line, verbatim, plus the lines sharing its reference.

    `shares_reference_with` is the duplicate-UTR read. It is information, never a lever:
    a repeated reference is what makes a reference resolve to two unclaimed payments, and
    the engine's answer to that is `multiple_candidates` -- a tie, which no agent may
    break. Seeing the duplicates helps an investigator explain a credit; there is no path
    by which noticing them resolves one.
    """

    bank_txn_id: str
    txn_date: str
    value_date: str
    narration: str
    reference: str
    credit_paise: int
    debit_paise: int
    shares_reference_with: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "txn_date": self.txn_date,
            "value_date": self.value_date,
            "narration": self.narration,
            "reference": self.reference,
            "credit_paise": self.credit_paise,
            "debit_paise": self.debit_paise,
            "shares_reference_with": list(self.shares_reference_with),
        }


@dataclass(frozen=True, slots=True)
class InvoiceView:
    invoice_no: str
    customer_name: str
    rupees: float
    tds_rupees: float
    invoice_date: str
    status: str

    def as_dict(self) -> dict:
        return {
            "invoice_no": self.invoice_no,
            "customer_name": self.customer_name,
            "rupees": self.rupees,
            "tds_rupees": self.tds_rupees,
            "invoice_date": self.invoice_date,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class InvoiceSearchResult:
    query: str
    matches: tuple[InvoiceView, ...]

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "matches": [
                {
                    "invoice_no": i.invoice_no,
                    "customer_name": i.customer_name,
                    "rupees": i.rupees,
                    "tds_rupees": i.tds_rupees,
                    "invoice_date": i.invoice_date,
                    "status": i.status,
                }
                for i in self.matches
            ],
        }


@dataclass(slots=True)
class InvestigationTrace:
    """One exception's investigation: what was called, what was concluded."""

    bank_txn_id: str
    steps: list[dict] = field(default_factory=list)
    proposals: list[EvidenceProposal] = field(default_factory=list)
    outcome: str = "not_started"   # "proposed" | "insufficient_evidence" | "budget_exhausted" | "error"
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "steps": self.steps,
            "proposals": [p.as_dict() for p in self.proposals],
            "outcome": self.outcome,
            "note": self.note,
        }
