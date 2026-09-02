"""
The ENGINE's fee model -- a band, not a formula.

This module is deliberately weaker than `recon.generator.fees`. The generator knows the
exact rate and the exact rounding; the engine knows only that the rate lies somewhere
in `MDR_RATE_BAND`. That asymmetry is the entire reason MR4 (conservation) means
anything: if the engine inverted the generator's function, it would reconcile perfectly
by construction and the check would prove nothing about the matcher.

There is one legitimate exception. Razorpay returns a real `fee` field on captured
payments, and that is genuine API output available at runtime on a merchant's own
books. Where it is present the engine uses it and the net collapses to a point
(plus or minus a paisa for the bank's own rounding). Where it is absent -- an
uncaptured record, or a payment the gateway has not yet priced -- the engine falls
back to the band and carries a wider interval.

So a payment's net is an INTERVAL, never a number, and every downstream comparison is
interval arithmetic. That is not defensive coding; it is the honest representation of
what is known.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import config as cfg

from ..schemas import Invoice, Payment


@dataclass(frozen=True, slots=True)
class NetInterval:
    """
    The range of amounts a payment could have settled for, in paise.

    `certain` records whether this came from Razorpay's own fee field (tight) or from
    the rate band (wide). Downstream code uses it to explain WHY a residual is loose,
    which is the difference between "this match is weak" and "we could not price the
    fee on this record".
    """

    lo: int
    hi: int
    certain: bool

    @property
    def width(self) -> int:
        return self.hi - self.lo

    @property
    def mid(self) -> int:
        return (self.lo + self.hi) // 2

    def __add__(self, other: "NetInterval") -> "NetInterval":
        return NetInterval(
            self.lo + other.lo, self.hi + other.hi, self.certain and other.certain
        )


ZERO = NetInterval(0, 0, True)

# Bank-side rounding slack when the true fee is known. The measured fee-model residual
# against real API output is [-1, +2] paise; +/-2 covers it with the sign symmetric.
#
# This is the SAME measured quantity as cfg.FEE_MODEL_MAX_RESIDUAL_PAISE, so it is read
# from there rather than restated. A local literal silently duplicating a config
# constant means widening the band in config.py would leave this path unchanged -- the
# fee model and the interval it produces would disagree about their own error bar.
_KNOWN_FEE_SLACK = cfg.FEE_MODEL_MAX_RESIDUAL_PAISE


def net_interval(payment: Payment) -> NetInterval:
    """
    What this payment could have settled for.

    Tight when Razorpay priced it; wide when the engine has to infer from the band.
    """
    if payment.fee is not None:
        net = payment.amount - payment.fee
        return NetInterval(net - _KNOWN_FEE_SLACK, net + _KNOWN_FEE_SLACK, True)

    rate_lo, rate_hi = cfg.MDR_RATE_BAND
    gross_up = 1.0 + cfg.GST_RATE
    # Largest plausible fee -> smallest plausible net, and vice versa.
    max_fee = math.ceil(rate_hi * payment.amount * gross_up)
    min_fee = math.floor(rate_lo * payment.amount * gross_up)
    return NetInterval(payment.amount - max_fee, payment.amount - min_fee, False)


def sum_intervals(payments: tuple[Payment, ...] | list[Payment]) -> NetInterval:
    total = ZERO
    for p in payments:
        total = total + net_interval(p)
    return total


def tolerance_for(credit_paise: int) -> int:
    """
    The matching tolerance for a credit of this size, in paise.

    Absolute plus a relative term, both fixed in config before the run and never tuned
    per record. The relative term exists because a fixed rupee tolerance is
    proportionally far tighter on a large settlement batch than on a small one.
    """
    return cfg.TOL_ABS_PAISE + (credit_paise * cfg.TOL_REL_BPS) // 10_000


def residual(credit_paise: int, interval: NetInterval) -> int:
    """
    Signed distance from the credit to the nearest edge of the interval, in paise.

    Zero means the credit falls inside the interval -- consistent with what is known,
    which is the strongest statement available when the fee is uncertain. The sign
    tells a human which way the money went: positive means the bank credited more than
    the payments account for.
    """
    if interval.lo <= credit_paise <= interval.hi:
        return 0
    if credit_paise < interval.lo:
        return credit_paise - interval.lo
    return credit_paise - interval.hi


def fits(credit_paise: int, interval: NetInterval, tolerance: int | None = None) -> bool:
    tol = tolerance if tolerance is not None else tolerance_for(credit_paise)
    return abs(residual(credit_paise, interval)) <= tol


def residual_tightness(credit_paise: int, interval: NetInterval) -> float:
    """
    How well conservation holds, in [0, 1]. 1.0 is exact to the paisa; 0.0 sits at the
    tolerance edge. This is the Layer 1 input to the composite confidence score.

    A wide interval is penalised even when the residual is zero: landing inside a band
    the engine could not narrow is weaker evidence than landing on a point it could.
    Without that penalty, an unpriced payment would score identically to a priced one,
    and the confidence number would be quietly overstating what is known.
    """
    tol = tolerance_for(credit_paise)
    if tol <= 0:
        return 0.0
    gap = abs(residual(credit_paise, interval))
    base = max(0.0, 1.0 - gap / tol)
    if interval.certain:
        return base
    uncertainty = min(1.0, interval.width / (2.0 * tol))
    return base * (1.0 - 0.5 * uncertainty)


def known_deductions(
    payments: tuple[Payment, ...] | list[Payment],
    invoices_by_no: dict[str, Invoice],
) -> int:
    """
    Deductions the LEDGER already knows about, in paise. Currently TDS.

    This is the single most important thing separating a tractable search from an
    intractable one. TDS is withheld by the payer and recorded on the invoice, so it is
    a FACT available before matching begins -- not an unknown to be solved for. Treating
    it as unknown would add a free variable per payment and make subset-sum
    combinatorially far worse while producing weaker answers.

    So the engine subtracts what it knows, and searches only over what it does not.
    """
    total = 0

    # TDS is a property of the INVOICE, not of each payment against it. Summing per
    # payment double-counts whenever a many-to-one batch contains two payments settling
    # the same invoice -- the engine then expects a credit smaller than the bank
    # actually sent, and a correct decomposition fails conservation for a reason that
    # has nothing to do with the matching.
    seen_invoices: set[str] = set()
    for p in payments:
        inv_no = p.notes.get("invoice_no")
        if inv_no and inv_no not in seen_invoices:
            inv = invoices_by_no.get(inv_no)
            if inv:
                seen_invoices.add(inv_no)
                total += inv.tds_amount

        # A refund netted inside a settlement batch. Razorpay records it on the payment
        # itself, so it is a KNOWN quantity exactly like TDS -- not an unknown to solve
        # for. Before this was accounted for, every refund-netted credit was
        # arithmetically unmatchable: the gap was Rs 50-500 against a Rs 1 tolerance, so
        # the engine refused all of them while ground truth expected an assignment.
        total += p.amount_refunded or 0

    return total


def expected_credit_interval(
    payments: tuple[Payment, ...] | list[Payment],
    invoices_by_no: dict[str, Invoice],
) -> NetInterval:
    """
    What the bank should have credited for these payments: settled amounts less the
    deductions the ledger already accounts for.
    """
    gross = sum_intervals(payments)
    tds = known_deductions(payments, invoices_by_no)
    if not tds:
        return gross
    return NetInterval(gross.lo - tds, gross.hi - tds, gross.certain)
