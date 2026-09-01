"""
The GENERATOR's exact fee schedule.

This is a deliberate duplicate of nothing. `recon.engine.fees` knows only a rate band
(`MDR_RATE_BAND`) and never learns any individual record's true rate; this module
knows the exact rate and the exact rounding. Keeping them separate is what makes MR4
(conservation) a real test instead of an identity -- if the engine inverted this
function, it would reconcile perfectly by construction and prove nothing.

The model is measured, not invented. Across all 18 genuinely captured test-mode
payments in data/real_payments.json:

    base = fee - tax = 0.022 * amount        exactly 2.200%, every record

    base = round(0.022 * amount)
    tax  = round(0.18 * base)
    fee  = base + tax

predicts the true fee within [-1, +2] paise. The exact GST rounding rule is NOT
recoverable from 18 observations -- floor misses 5, ceil 7, round 6 -- and no attempt
is made here to claim otherwise. See docs/DEFECT_LOG.md 2026-09-01-01, which records
concluding "floor" from a single discriminating observation and then being falsified.

The residual matters because it is the honest uncertainty this project is about:
2 paise against a 100-paise matching tolerance is a 50x margin, so the ambiguity is
absorbed rather than resolved by assertion.
"""

from __future__ import annotations

# Measured on 18 real captured payments (netbanking and wallet, Rs 215 - Rs 18,700).
MDR_RATE_EXACT = 0.022
GST_RATE_EXACT = 0.18

# The measured bound of this model against real API output. Asserted in tests.
KNOWN_RESIDUAL_PAISE = (-1, 2)


def mdr_base(amount_paise: int) -> int:
    """The gateway fee before GST, in paise."""
    return round(MDR_RATE_EXACT * amount_paise)


def gst_on(base_paise: int) -> int:
    """GST on the gateway fee, in paise."""
    return round(GST_RATE_EXACT * base_paise)


def fee_and_tax(amount_paise: int) -> tuple[int, int]:
    """
    Return (fee, tax) in paise for a gross payment amount.

    `fee` is INCLUSIVE of `tax`, matching how Razorpay reports it.
    """
    base = mdr_base(amount_paise)
    tax = gst_on(base)
    return base + tax, tax


def net_settled(amount_paise: int) -> int:
    """What the merchant actually receives for one payment, before any TDS."""
    fee, _ = fee_and_tax(amount_paise)
    return amount_paise - fee


def gross_for_target_net(target_net_paise: int) -> int:
    """
    Invert the fee model: find the gross amount whose net settles to exactly
    `target_net_paise`.

    Needed by the ambiguity case, which is specified in NET space -- the two candidate
    subsets must collide to the paisa after fees, or the collision is not exact and
    the engine could separate them on amount alone.

    Fee rounding is not injective, so this searches a small neighbourhood around the
    analytic estimate rather than trusting a closed form. Raises if no gross produces
    the requested net, which fails generation loudly instead of silently emitting an
    ambiguity case that is not actually ambiguous.
    """
    approx = int(round(target_net_paise / (1 - MDR_RATE_EXACT * (1 + GST_RATE_EXACT))))
    for delta in range(-64, 65):
        candidate = approx + delta
        if candidate > 0 and net_settled(candidate) == target_net_paise:
            return candidate
    raise ValueError(
        f"No gross amount settles to exactly {target_net_paise} paise under the "
        f"measured fee model. The ambiguity case cannot be built as specified."
    )
