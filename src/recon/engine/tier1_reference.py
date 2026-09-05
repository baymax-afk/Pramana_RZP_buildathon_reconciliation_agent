"""
Tier 1 -- exact reference matching.

The strongest available evidence: a bank reference that exactly equals a payment id,
an order id, or an invoice number. When it fires, no search is needed and the
uniqueness margin is 1.0 by construction.

The trap this tier must not fall into: **references are not guaranteed unique.**
Duplicate UTRs occur in real statements (it is one of the nine injected defects), and
a matcher that trusts references blindly will assign the same payment twice, or assign
two different credits to one payment -- double-posting money. So this tier indexes
references to *sets*, and refuses on collision rather than picking one.

A collision here is genuinely ambiguous: two credits carrying the same reference is a
bank-side defect, and the correct response is to surface both, not to guess.
"""

from __future__ import annotations

import config as cfg

from collections import defaultdict

from ..schemas import BankTxn, Invoice, Payment
from . import fees
from .normalize import ParsedNarration
from .results import Candidate, RefusalCategory

TIER = "tier1_reference"


class ReferenceIndex:
    """
    Maps every reference-ish token on the payments/invoices side to the records
    carrying it. Values are SETS: the whole point is that a reference may be ambiguous,
    and an index that silently kept the last writer would hide exactly the defect this
    tier exists to catch.
    """

    __slots__ = ("_by_ref", "_invoice_by_no")

    def __init__(self, payments: tuple[Payment, ...], invoices: tuple[Invoice, ...]):
        self._by_ref: dict[str, set[str]] = defaultdict(set)
        self._invoice_by_no = {i.invoice_no: i for i in invoices}

        for p in payments:
            if not p.captured:
                continue
            for token in self._tokens_for(p):
                self._by_ref[token].add(p.id)

    @staticmethod
    def _tokens_for(p: Payment) -> set[str]:
        tokens: set[str] = {p.id.upper()}
        if p.order_id:
            tokens.add(p.order_id.upper())
        if p.bank_transaction_id:
            tokens.add(str(p.bank_transaction_id).upper())
        inv = p.notes.get("invoice_no")
        if inv:
            tokens.add(inv.upper())
        # The gateway writes the receipt into `description` as "#RECEIPT".
        if p.description and p.description.startswith("#"):
            tokens.add(p.description[1:].upper())
        return {t for t in tokens if t}

    def lookup(self, token: str | None) -> set[str]:
        if not token:
            return set()
        return set(self._by_ref.get(token.upper(), ()))


def match(
    txn: BankTxn,
    parsed: ParsedNarration,
    index: ReferenceIndex,
    payments_by_id: dict[str, Payment],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
    declared_paise: int = 0,
) -> tuple[list[Candidate], RefusalCategory | None, str]:
    """
    Try to match one bank credit on reference alone.

    Returns (candidates, refusal_category, reason). Zero candidates means this tier had
    nothing to say and the caller should fall through to tier 2 -- it is NOT a refusal.
    A refusal here is reserved for a reference that genuinely resolves to more than one
    payment, which is a defect worth surfacing rather than resolving.
    """
    tokens = {
        parsed.merchant_ref,   # the payer quoting our invoice -- the strongest link
        parsed.reference,      # UTR or numeric rail reference
        txn.ref_no,
        parsed.settlement_id,
    }
    hits: set[str] = set()
    for token in tokens:
        hits |= index.lookup(token)

    hits -= claimed
    if not hits:
        return [], None, ""

    if len(hits) > 1:
        # The same reference points at several unclaimed payments. Real cause: a
        # duplicate UTR. Surface every candidate rather than choosing one.
        candidates = []
        for pid in sorted(hits):
            interval = fees.expected_credit_interval(
                [payments_by_id[pid]], invoices_by_no, declared_paise
            )
            candidates.append(
                Candidate(
                    payment_ids=(pid,),
                    residual_paise=fees.residual(txn.credit, interval),
                    tier=TIER,
                    interval_lo=interval.lo,
                    interval_hi=interval.hi,
                    certain=interval.certain,
                )
            )
        return (
            candidates,
            RefusalCategory.MULTIPLE_CANDIDATES,
            f"reference {txn.ref_no!r} resolves to {len(hits)} unclaimed payments "
            f"({', '.join(sorted(hits))}); a duplicate reference cannot identify one",
        )

    pid = next(iter(hits))
    payment = payments_by_id[pid]
    # TDS is a ledger-side fact, not an unknown -- deduct what is already known before
    # declaring the amount inconsistent. See fees.known_deductions.
    interval = fees.expected_credit_interval([payment], invoices_by_no, declared_paise)
    resid = fees.residual(txn.credit, interval)

    candidate = Candidate(
        payment_ids=(pid,),
        residual_paise=resid,
        tier=TIER,
        interval_lo=interval.lo,
        interval_hi=interval.hi,
        certain=interval.certain,
    )

    # A reference match whose AMOUNT does not hold is not a match -- it is two
    # independent evidence channels disagreeing, and the amount channel is the
    # stronger one. Surface it rather than letting the reference override conservation.
    if not fees.fits(txn.credit, interval):
        return (
            [candidate],
            RefusalCategory.UNEXPLAINED_RESIDUAL,
            f"reference matches payment {pid} but the amount does not: credit "
            f"{txn.credit}p vs expected {interval.lo}..{interval.hi}p "
            f"(residual {resid:+d}p)"
            + _implied_rate_note(txn.credit, payment.amount),
        )

    return [candidate], None, ""


def _implied_rate_note(credit: int, gross: int) -> str:
    """
    Say what deduction rate WOULD reconcile this credit, and whether the engine allows it.

    A residual is a symptom; the rate it implies is a diagnosis. "Credit 643537p vs
    expected 644715..644719p" tells an operator the numbers disagree and leaves them to
    work out why. "The credit implies a 2.77% deduction, above the 1.8-2.5% this engine
    assumes -- consistent with a charge levied on top of the gateway fee" names the thing
    to go and look for.

    Measured on the reported batch: all three `unexplained_residual` refusals imply
    2.61-3.15%, every one above the band, and every one is a `bank_charge` -- the
    receiving bank taking its own NEFT/RTGS fee on top of MDR. The engine cannot widen
    its band to absorb that without also absorbing genuine coincidences, which is the
    argument `config.py` makes for keeping the band narrow. What it can do is say which
    it is looking at.

    Silent when the rate is inside the band: there the residual is not a rate problem and
    a note about rates would point the wrong way.
    """
    if gross <= 0:
        return ""
    implied = (gross - credit) / gross
    lo, hi = cfg.MDR_RATE_BAND
    if implied > hi:
        return (
            f". The credit implies a {implied:.2%} deduction from gross, above the "
            f"{lo:.1%}-{hi:.1%} this engine assumes -- consistent with a charge levied "
            f"on top of the gateway fee, such as a receiving-bank NEFT/RTGS fee"
        )
    if implied < lo:
        return (
            f". The credit implies a {implied:.2%} deduction from gross, below the "
            f"{lo:.1%}-{hi:.1%} this engine assumes -- more arrived than a fee-bearing "
            f"settlement of this payment would produce"
        )
    return ""
