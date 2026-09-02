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

Five more, added later. The first four are ORDINARY -- every merchant sees them monthly
-- and the batch was unrealistically clean without them. The fifth is where the engine's
own model runs out.

10. `overpayment`      -- the customer pays MORE than the invoice, usually by rounding
                          up or by clearing an old balance in the same transfer. The
                          mirror of `partial_payment`, and the invoice ends
                          over-settled rather than open.
11. `advance_payment`  -- money arrives against no invoice at all (payment on account).
                          Every payment in this batch used to carry an invoice number,
                          which made tier 1 available far more often than reality
                          allows. With no invoice there is no reference and no TDS, and
                          the amount channel has to stand alone.
12. `bank_charge`      -- the BANK deducts its own NEFT/RTGS handling fee from the
                          credit. Unlike MDR this appears nowhere: not on the payment,
                          not in the ledger, and not in the narration. At Rs 5-50
                          against a Rs 1 tolerance it is arithmetically unmatchable, so
                          this defect is labelled `refuse` -- it is a case the engine
                          SHOULD decline, and it exists to prove the engine declines it
                          rather than absorbing it by widening a band.
13. `third_party_payer` -- a parent company settles a subsidiary's invoice, so the
                          bank narration carries a name that legitimately does not
                          match the invoice customer. The amount channel is right and
                          the name channel is wrong, which is exactly the disagreement
                          Layer 3 must not resolve by vetoing a correct match.
Two more that stress the ENGINE'S MODEL rather than its arithmetic. Both are labelled
`refuse`, and in both cases refusing is the correct output -- but the coverage they cost
is real and is reported rather than absorbed.

15. `split_settlement`  -- one payment is settled across TWO bank credits. Razorpay does
                          this for on-demand settlements and when a batch crosses a
                          limit. The engine cannot represent it: a payment is claimed
                          once, and each credit needs a SUBSET of payments that sums to
                          it, so half a payment has nowhere to go. Refusing is right --
                          posting a part-settlement against a whole payment would be
                          worse -- but the relation is genuinely outside the model, and
                          `docs/ARCHITECTURE.md` names it as a limitation rather than
                          letting a correct-looking refusal hide it.
16. `chargeback_debit`  -- a settled payment is clawed back later by a DEBIT line. The
                          engine reads credits only, so the money leaving is invisible
                          to it: not matched, not refused, not counted. The statement
                          carried no debits at all before this defect existed, which is
                          why nobody noticed. The metrics block now reports what the
                          engine did not examine.

14. `weekend_bunching` -- Friday, Saturday and Sunday payments all settle on Monday, so
                          realised drift reaches 3 days on top of the settlement window.
                          It is the ordinary reason a lookback has to be generous, and
                          it stresses LOOKBACK_DAYS rather than any tier.
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
    merchant_ref: str | None = None,
) -> Narration:
    """
    Build a realistic Indian bank narration string.

    Styles differ in how much they reveal, which is the point: a matcher must cope
    with narrations that carry a full payer name, ones that carry a truncated name,
    and ones that carry no name at all. The last kind is where amount evidence has to
    stand alone -- and where the ambiguity case lives.

    `merchant_ref` is the payer quoting the invoice number in the remittance, which
    real payers do a good deal of the time. It is what makes an EXACT-reference match
    possible at all. Without it there is no reference linkage between the bank side
    and the payments side, tier 1 can never fire, and the duplicate-UTR defect has
    nothing to corrupt -- the statement would look realistic while being trivially
    easier than reality in the one dimension that matters most.
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

    ref_part = f"-{merchant_ref}" if merchant_ref else ""
    if style == "neft":
        return Narration(f"NEFT-{utr}-{name}{ref_part}-CR", style, True, True)
    if style == "rtgs":
        return Narration(f"RTGS-{utr}-{name}{ref_part}-CR", style, True, True)
    if style == "imps":
        return Narration(f"IMPS/{utr[5:]}/{name}{'/' + merchant_ref if merchant_ref else ''}", style, True, True)
    # upi
    short = name[:12].strip()
    tail = f"/{merchant_ref}" if merchant_ref else ""
    return Narration(f"UPI/{utr[5:]}/PAYMENT/{short}{tail}", style, True, True)


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


# --------------------------------------------------------------------------
# Messy narrations -- the ones the regex tier genuinely cannot parse
# --------------------------------------------------------------------------
# Real bank statements are not uniformly formatted. Older rails, clearing-house
# entries, mobile-banking transfers and cash-management systems each write their own
# shape, and plenty of them jam the reference into the payer name with no delimiter at
# all. A generator that only emits tidy NEFT/RTGS/UPI strings makes the parsing problem
# look solved and leaves the LLM tier with nothing to do -- which would make the
# LLM-on/LLM-off precision comparison vacuous.
#
# These templates are deliberately built to defeat the regex tier: no recognisable
# style prefix, references without the XX-NNNN-NNNN shape it looks for, and names run
# together with digits. They are the residue the deterministic parser is allowed to
# fail on.
_MESSY_TEMPLATES = (
    "TRF FRM {acct} {name_jammed} {ref_jammed} CR",
    "BY CLG/{digits}/{name_short}",
    "MB:{contact}-{name_short}-{ref_slash}",
    "CMS/{name_jammed}/{ref_slash}/CR",
    "{name_short} FUND TRF {digits} {ref_jammed}",
    "INW REM {digits} {name_short} {ref_jammed}",
)


def messy_narration(
    rng: random.Random, customer: Customer, merchant_ref: str
) -> Narration:
    """
    Emit a narration the regex tier cannot fully parse.

    The payer and the reference ARE present -- a human could read them, and so can an
    LLM -- but not in any shape the deterministic patterns match. That is the honest
    division of labour the trust boundary describes: regex handles what is structured,
    the LLM reads what is merely legible, and neither of them decides a match.
    """
    tmpl = rng.choice(_MESSY_TEMPLATES)
    name = (customer.bank_hint or customer.canonical_name).upper()
    text = tmpl.format(
        acct=f"{rng.randint(10**10, 10**11 - 1)}",
        digits=f"{rng.randint(10**5, 10**6 - 1)}",
        contact=customer.contact[-10:],
        name_jammed=name.replace(" ", "")[:20],
        name_short=name[:18],
        ref_jammed=merchant_ref.replace("-", "")[:14],
        ref_slash=merchant_ref.replace("-", "/"),
    )
    return Narration(text, "messy", carries_name=True, carries_ref=True)


# --------------------------------------------------------------------------
# Bank-side charges
# --------------------------------------------------------------------------
def bank_charge_for(rng: random.Random) -> int:
    """
    The bank's own handling fee on an inbound transfer, in paise.

    NEFT and RTGS charges are small, banded by amount, and levied by the RECEIVING
    bank -- so unlike MDR they appear on no Razorpay object and in no ledger the
    merchant controls. Rs 5-50 covers the published retail bands.

    The number matters because of what it is measured against. It is 5-50x the Rs 1
    matching tolerance, so a credit carrying one cannot be reconciled to its payment by
    arithmetic. That is the point of including it: an engine that quietly widened its
    tolerance to absorb bank charges would also start absorbing genuine coincidences,
    and the whole subset-sum uniqueness argument rests on tolerance staying far below
    the smallest payment.
    """
    return rng.choice([500, 1_000, 1_180, 2_360, 3_000, 5_000])
