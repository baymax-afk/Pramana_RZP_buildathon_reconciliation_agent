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
    """

    AUTHORISED_PAYER_FOR = "authorised_payer_for"


# A payment id, a bank transaction id, or an order id. Used to REJECT values, not to
# find them: see EvidenceProposal.validate.
_ID_SHAPES = (
    re.compile(r"^pay_", re.IGNORECASE),
    re.compile(r"^order_", re.IGNORECASE),
    re.compile(r"^bank_txn", re.IGNORECASE),
    re.compile(r"^[a-z]{4}[a-z0-9]{8,}$", re.IGNORECASE),   # UTR-shaped
)


@dataclass(frozen=True, slots=True)
class EvidenceProposal:
    """
    The only write in the agent's whole surface.

    **It carries no payment id, no candidate, no score and no verdict** -- the same
    structural argument `NarrationFields` makes for the narration tier, applied one
    level up where it matters more, because an investigator sees far more of the batch
    than a narration parser does.

    That argument needed strengthening here, and the audit is why. `REVIEW.md` §5 showed
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

    def validate(self) -> None:
        """Raise if this proposal is trying to do something it is not allowed to do."""
        if not isinstance(self.field, EvidenceField):
            raise ValueError(
                f"field must be an EvidenceField, got {self.field!r}. A free-text "
                f"channel the engine never agreed to weigh would be ignored silently."
            )
        value = (self.value or "").strip()
        if not value:
            raise ValueError("an evidence proposal with no value asserts nothing")
        if len(value) > 120:
            raise ValueError(f"value is {len(value)} chars; cap is 120")
        for shape in _ID_SHAPES:
            if shape.match(value):
                raise ValueError(
                    f"value {value!r} looks like a record identifier. This channel "
                    f"carries COUNTERPARTY NAMES only: an identifier would let the "
                    f"agent name a specific record, which is the one thing the trust "
                    f"boundary exists to prevent (see REVIEW.md section 5)."
                )
        if not self.rationale.strip():
            raise ValueError(
                "a proposal must say why. An unexplained verdict change is exactly "
                "what this architecture is arguing against."
            )

    def as_dict(self) -> dict:
        return {
            "bank_txn_id": self.bank_txn_id,
            "field": self.field.value,
            "value": self.value,
            "rationale": self.rationale,
            "tool_calls": list(self.tool_calls),
        }


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
class InvoiceView:
    invoice_no: str
    customer_name: str
    rupees: float
    tds_rupees: float
    invoice_date: str
    status: str


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
