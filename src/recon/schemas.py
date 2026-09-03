"""
Dataclasses for all three reconciliation sides, plus ground truth.

Money is integer PAISE everywhere in memory. The bank statement and invoice ledger
are rupee strings with 2dp on disk -- as real Indian bank exports are -- and are
converted at ingest. Floats never touch a monetary value after parsing.

These types are the engine's ONLY input. `run.py` loads the three sides and passes
these objects in; no engine function ever receives a path. That is the primary
enforcement of the ground-truth isolation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Literal

Provenance = Literal["R1", "R2", "S"]
# "split" is one payment settled across MANY credits -- the inverse of many_to_one, and
# a relation the engine cannot represent (see ARCHITECTURE.md, "Two named limitations").
# It was being constructed without being declared here, so it reached ground_truth.json
# and the scorer's relation buckets as a value the type does not admit.
Relation = Literal["one_to_one", "many_to_one", "partial", "split", "unmatched"]
Verdict = Literal["assign", "refuse"]


@lru_cache(maxsize=None)
def date_of(unix_ts: int) -> date:
    """
    The calendar date of a unix timestamp, in UTC.

    Every module must agree on this. The generator originally used local time while the
    engine used UTC, which shifts a payment across a day boundary for anyone east or
    west of Greenwich -- and a payment that moves one day can fall out of a credit's
    lookback entirely. A timezone bug here presents as an unmatched payment, not as an
    error, so it is centralised rather than left to each caller.

    **Memoised, and safe to memoise.** It is a pure function of one integer, so the
    cache cannot change an answer -- only how often the answer is recomputed. That
    matters because it is the single hottest call in the engine: candidate pooling
    re-derives the same few hundred payment dates for every credit in every round, and
    profiling put `datetime.fromtimestamp` at the top of the profile with ~380k calls
    over ~200 distinct timestamps. Caching it does not weaken the purity argument
    `match_once` rests on: the mapping is total, deterministic, and carries nothing
    from one batch into the next.
    """
    return datetime.fromtimestamp(unix_ts, UTC).date()


def rupees_to_paise(s: str | Decimal) -> int:
    """
    Parse a rupee-denominated string ('1,234.56') to integer paise.

    Uses Decimal, never float: float('0.07') * 100 is 7.000000000000001, and a
    systematic sub-paisa error in ingest would surface later as unexplained
    conservation residual -- a defect in the checker masquerading as a defect in the
    data. Handles thousands separators, which real bank exports contain.

    **Every malformed input raises ValueError naming the offending text.** This used to
    leak whatever the Decimal machinery happened to throw, which was three different
    exception types with no context: `InvalidOperation` for "- 100" or "(500)",
    `ValueError` for "NaN", and `OverflowError` for "Infinity". A loader traceback
    ending in `decimal.InvalidOperation` tells an operator nothing about which column of
    which row of which file is bad, and a caller cannot even catch it in one clause.

    The non-finite cases deserve their own mention: `Decimal("NaN")` and
    `Decimal("Infinity")` are perfectly valid Decimals. Anything validating by "does
    this parse as a Decimal" waves them straight through, and they fail much later,
    somewhere that looks like an arithmetic bug rather than a bad input row.
    """
    if isinstance(s, Decimal):
        d = s
    else:
        cleaned = str(s).strip().replace(",", "").replace("\u20b9", "").strip()
        if not cleaned or cleaned in {"-", "0.00"}:
            return 0
        try:
            d = Decimal(cleaned)
        except InvalidOperation:
            raise ValueError(f"not a rupee amount: {str(s)!r}") from None
    if not d.is_finite():
        raise ValueError(f"rupee amount is not finite: {str(s)!r}")
    return int((d * 100).to_integral_value())


def paise_to_rupees(p: int) -> str:
    """Render integer paise as a 2dp rupee string, for CSV output."""
    sign = "-" if p < 0 else ""
    p = abs(p)
    return f"{sign}{p // 100}.{p % 100:02d}"


# --------------------------------------------------------------------------
# Side A -- Razorpay payments
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Payment:
    """
    A Razorpay payment. Field names and semantics mirror the live API response so
    that genuinely captured records (tier R1) drop in unchanged.

    `fee` is INCLUSIVE of `tax`, as Razorpay returns it: fee = mdr_base + gst.
    Both are None for anything not captured -- which is every R2 record, since an
    uncaptured order is not a payment.
    """

    id: str
    amount: int  # paise, gross
    currency: str
    status: str  # "captured" | "failed" | "created"
    captured: bool
    method: str  # "netbanking" | "wallet" | "card" | "upi"
    order_id: str | None
    created_at: int  # unix seconds
    description: str
    contact: str
    email: str
    provenance: Provenance
    fee: int | None = None  # paise, inclusive of tax
    tax: int | None = None  # paise, the GST component of fee
    bank: str | None = None
    wallet: str | None = None
    bank_transaction_id: str | None = None
    error_reason: str | None = None
    invoice_id: str | None = None
    # A refund netted against this payment, in paise. Razorpay records refunds on the
    # payment, which makes them a KNOWN deduction the engine may subtract before
    # searching -- exactly like ledger-side TDS, and unlike anything it has to solve for.
    amount_refunded: int = 0
    refund_status: str | None = None
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def net_paise(self) -> int | None:
        """Amount actually settled to the merchant, when the true fee is known."""
        if self.fee is None:
            return None
        return self.amount - self.fee

    @property
    def customer_name(self) -> str | None:
        return self.notes.get("customer_name")

    @property
    def invoice_no(self) -> str | None:
        return self.notes.get("invoice_no")


# --------------------------------------------------------------------------
# Side B -- bank statement
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BankTxn:
    """
    One line of an Indian bank statement export.

    `narration` is the raw description string exactly as the bank wrote it, including
    payer-name truncation to the bank's field width. That truncation is a genuine
    Fellegi-Sunter signal (partial name agreement), not noise to be cleaned away, so
    it is preserved verbatim and parsed non-destructively.
    """

    id: str  # our own handle, e.g. "bank_txn_042"
    txn_date: str  # ISO date
    value_date: str  # ISO date; differs from txn_date under settlement drift
    narration: str
    ref_no: str  # UTR-shaped
    credit: int  # paise, 0 if this is a debit
    debit: int  # paise, 0 if this is a credit
    balance: int  # paise, running balance

    @property
    def is_credit(self) -> bool:
        return self.credit > 0

    @property
    def amount(self) -> int:
        """Signed movement in paise: positive for credits, negative for debits."""
        return self.credit - self.debit


# --------------------------------------------------------------------------
# Side C -- invoice / ERP ledger
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PayerAuthorisation:
    """
    One row of the merchant's authorised-payer register (side D).

    Says a name that appears on the BANK STATEMENT is on record as permitted to settle
    for a name that appears in the INVOICE LEDGER. It is a join between two published
    fields -- a customer master record -- and it does not say which credit settles which
    payment. See `config.PAYER_DIRECTORY_COVERAGE` for the full argument.

    The ENGINE never reads this. It reaches Layer 3 only as an asserted fact supplied
    through `match_once(evidence=...)`, gathered by `recon.agent`. That separation is
    the point: the engine weighs evidence, it does not go looking for it.
    """

    payer_name: str
    authorised_for_customer: str
    relationship: str          # "parent" | "group_treasury" | "affiliate"
    on_record_since: str       # ISO date


@dataclass(frozen=True, slots=True)
class Invoice:
    """
    An open receivable.

    `tds_amount` is a LEDGER-SIDE FACT, known before matching begins. That matters to
    the subset-sum search: TDS is deducted from the bank credit up front rather than
    being searched for as a free variable, which keeps the search space bounded.
    """

    invoice_no: str
    customer_name: str
    customer_gstin: str
    invoice_date: str  # ISO date
    due_date: str  # ISO date
    gross_amount: int  # paise
    tds_amount: int  # paise, 0 when no TDS applies
    currency: str
    status: str  # "open" | "settled" | "part_settled"
    po_reference: str

    @property
    def net_receivable(self) -> int:
        return self.gross_amount - self.tds_amount


# --------------------------------------------------------------------------
# The three sides, as handed to the engine
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReconInputs:
    """
    The complete engine input. This is the boundary object: `run.py` builds it from
    disk and the engine receives nothing else. No paths, no truth, no config lookups
    that resolve to a filesystem location.
    """

    payments: tuple[Payment, ...]
    bank_txns: tuple[BankTxn, ...]
    invoices: tuple[Invoice, ...]
    seed: int
    payments_per_window: int

    def shuffled(self, rng) -> "ReconInputs":
        """
        Return the same inputs with all three sides independently reordered.

        This is the operation MR1 is built on, and it is used at RUNTIME, not only in
        tests: the engine matches over K shuffled orderings and refuses any assignment
        that is not stable across all of them, because such an assignment was decided
        by iteration order rather than by the data.
        """
        p, b, i = list(self.payments), list(self.bank_txns), list(self.invoices)
        rng.shuffle(p)
        rng.shuffle(b)
        rng.shuffle(i)
        return ReconInputs(
            payments=tuple(p),
            bank_txns=tuple(b),
            invoices=tuple(i),
            seed=self.seed,
            payments_per_window=self.payments_per_window,
        )


# --------------------------------------------------------------------------
# Ground truth -- written by the generator, read ONLY by src/scorer
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TruthLink:
    """
    What actually happened for one bank transaction.

    `expected_verdict` is load-bearing. For the hand-placed ambiguity case it is
    "refuse", so refusing scores as CORRECT and assigning either candidate subset
    scores as a false match. Without this, an engine that refuses appropriately would
    be penalised for it, and the metric would reward guessing.
    """

    bank_txn_id: str
    payment_ids: tuple[str, ...]
    invoice_nos: tuple[str, ...]
    defect_labels: tuple[str, ...]
    relation: Relation
    expected_verdict: Verdict


DEFECT_LABELS = (
    "mdr_fee",
    "tds_deduction",
    "settlement_drift",
    "many_to_one",
    "partial_payment",
    "duplicate_utr",
    "near_duplicate_name",
    "paisa_rounding",
    "refund_netted",
)
