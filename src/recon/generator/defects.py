"""
The nine defect categories, plus bank narration and UTR synthesis.

Every defect here is ground-truth labelled at the point of injection, so the label is
never inferred after the fact. A defect the generator did not deliberately create is a
bug in the generator, not a finding.

The nine, and why each is hard:

1. `mdr_fee`           -- the credit is short by a fee the bank statement never names.
2. `tds_deduction`     -- short again, by an amount only the ledger knows.
3. `settlement_drift`  -- the credit lands 1-2 days after the payment, so date is a
                          window, not a key.
4. `many_to_one`       -- one credit covers N payments; requires subset-sum, and is
                          where non-unique decompositions live.
5. `partial_payment`   -- the credit is less than the invoice, and the difference is
                          legitimate rather than an error.
6. `duplicate_utr`     -- the reference, normally the strongest signal, is no longer
                          unique. A matcher that trusts references blindly double-posts.
7. `near_duplicate_name` -- see customers.py: alias families must match, confusable
                          pairs must not, and only contact/GSTIN separate them.
8. `paisa_rounding`    -- sub-rupee drift from fee rounding, which is why tolerance
                          exists and why it must stay far below the smallest payment.
9. `refund_netted`     -- a refund is subtracted inside a settlement batch, so the
                          credit is smaller than the payments it covers and naive
                          subset-sum cannot close.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .customers import Customer, truncate_for_bank

# --------------------------------------------------------------------------
# UTR / reference synthesis
# --------------------------------------------------------------------------
_UTR_PREFIXES = ("AXISP", "HDFCN", "ICICR", "KKBKH", "UTIBP", "PUNBR")


def make_utr(rng: random.Random) -> str:
    """A UTR-shaped bank reference: 5-char bank prefix + 11 digits."""
    return rng.choice(_UTR_PREFIXES) + "".join(
        str(rng.randint(0, 9)) for _ in range(11)
    )


def make_settlement_id(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "setl_" + "".join(rng.choice(alphabet) for _ in range(14))


# --------------------------------------------------------------------------
# Bank narration synthesis
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Narration:
    text: str
    style: str
    carries_name: bool
    carries_ref: bool


def narrate(
    rng: random.Random,
    utr: str,
    customer: Customer | None,
    n_txns: int = 1,
    force_style: str | None = None,
) -> Narration:
    """
    Build a realistic Indian bank narration string.

    Styles differ in how much they reveal, which is the point: a matcher must cope
    with narrations that carry a full payer name, ones that carry a truncated name,
    and ones that carry no name at all. The last kind is where amount evidence has to
    stand alone -- and where the ambiguity case lives.
    """
    name = truncate_for_bank(customer.bank_hint or customer.canonical_name) if customer else ""
    style = force_style or rng.choice(
        ["neft", "neft", "rtgs", "upi", "imps", "settlement"]
    )

    if style == "settlement":
        # A gateway settlement batch. Deliberately carries NO payer name -- the batch
        # covers many payers, so there is no single name to print. This is realistic
        # and it is what makes many-to-one decomposition genuinely hard.
        return Narration(
            f"RAZORPAY SETTLEMENT {make_settlement_id(rng)} {n_txns} TXNS",
            style, carries_name=False, carries_ref=False,
        )
    if style == "neft":
        return Narration(f"NEFT-{utr}-{name}-CR", style, True, True)
    if style == "rtgs":
        return Narration(f"RTGS-{utr}-{name}-CR", style, True, True)
    if style == "imps":
        return Narration(f"IMPS/{utr[5:]}/{name}", style, True, True)
    # upi
    short = name[:12].strip()
    return Narration(
        f"UPI/{utr[5:]}/PAYMENT/{short}", style, True, True
    )


def anonymous_settlement_narration(rng: random.Random, n_txns: int) -> Narration:
    """
    A settlement narration with no payer name and a reference matching no payment.

    Used for the hand-placed ambiguity case. Tier 1 (exact reference) cannot fire, and
    the Fellegi-Sunter name channel has near-zero and EQUAL evidence for every
    candidate -- so nothing but the amounts is available, and the amounts collide.
    """
    return Narration(
        f"RAZORPAY SETTLEMENT {make_settlement_id(rng)} {n_txns} TXNS",
        "settlement", carries_name=False, carries_ref=False,
    )


# --------------------------------------------------------------------------
# Deduction defects
# --------------------------------------------------------------------------
def tds_for(gross_paise: int, rate: float = 0.02) -> int:
    """
    TDS withheld by the payer, in paise.

    Section 194Q-style withholding at 2% on the invoice gross. This is a LEDGER-side
    fact: it appears on the invoice, so the engine may deduct it before searching
    rather than treating it as an unknown. That distinction keeps the subset-sum
    search bounded -- an unknown deduction would add a free variable per payment.
    """
    return round(gross_paise * rate)


def apply_paisa_rounding(rng: random.Random, amount_paise: int) -> tuple[int, int]:
    """
    Nudge a credit by a few paise, as intermediary bank rounding does.

    Returns (adjusted, delta). The delta is deliberately tiny and deliberately real:
    it is why the matcher needs a tolerance at all, and why that tolerance must stay
    orders of magnitude below the smallest payment. If tolerance ever approached the
    payment scale, a subset and that-subset-plus-one-small-payment would both satisfy
    the constraint and every many-to-one result would be meaningless.
    """
    delta = rng.choice([-3, -2, -1, 1, 2, 3])
    return amount_paise + delta, delta
