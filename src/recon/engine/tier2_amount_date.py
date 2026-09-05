"""
Tier 2 -- amount plus date-window matching, one payment to one credit.

Fires when no reference resolves. A payment is a candidate when its settled-amount
interval covers the credit within tolerance AND it falls inside the credit's lookback
window `[D - LOOKBACK_DAYS, D]`, where the lookback covers the settlement window
plus the maximum drift a credit can carry.

Two design decisions carry most of the weight here:

**Date is a window, not a key.** Settlement drift (T+1/T+2) is one of the nine injected
defects, so requiring an exact date match would miss most real settlements. The window
is what makes the pool bounded -- and the pool being bounded is what makes the whole
search tractable.

**Multiple fits are a refusal, not a ranking.** If three payments in the window all
match the credit within tolerance, the amount channel has not identified one; it has
identified three. Tier 2 does not break the tie by picking the closest residual,
because a tie broken by an arbitrarily small difference is not evidence -- it is noise
dressed as a decision. It refuses and hands all three to a human.

That is the same principle Layer 2 applies to subset-sum, applied one tier earlier and
far more cheaply.
"""

from __future__ import annotations

from datetime import date, timedelta

import config as cfg

from ..schemas import BankTxn, Invoice, Payment, date_of
from . import fees
from .results import Candidate, RefusalCategory

TIER = "tier2_amount_date"


def payment_date(p: Payment) -> date:
    """UTC, via the shared helper -- see schemas.date_of for why this is centralised."""
    return date_of(p.created_at)


def window_for(
    txn: BankTxn, extra_days: int = 0, settled_on: date | None = None
) -> tuple[date, date]:
    """
    The inclusive date range a credit may draw payments from.

    `extra_days` widens it beyond the nominal settlement window. It exists for the
    high-density sweep arm, where drift can push a credit further from its payments,
    and it is a CONFIG-level decision rather than a per-record one -- widening the
    window for a stubborn credit would be exactly the sort of per-record tuning
    `docs/METRICS.md` forbids.

    **`settled_on` moves the window's ANCHOR and never its WIDTH, and the distinction is
    the whole argument for letting it exist.** The window is `LOOKBACK_DAYS` counted back
    from the date the money reached the bank, which is a proxy: what actually bounds
    which payments could be in a settlement is the date the gateway SETTLED it. Those are
    normally the same day and occasionally are not.

    So an investigator that produces an external record saying "this batch settled on the
    14th, it merely credited on the 21st" is not asking for a bigger search. It is
    correcting which day the same-sized search is counted from. The bound is untouched,
    the pool does not grow, and a credit that had the wrong seven days looked at now has
    the right seven.

    Widening would be tuning: keep failing, add a day, try again. Re-anchoring cannot be
    used that way, because moving the window forwards discards as many days as it gains
    -- and `agent/validate.py` will not accept a settlement date later than the credit
    or further back than `LOOKBACK_DAYS + EVIDENCE_WINDOW_SLACK_DAYS`. A caller who
    passes `settled_on` on a hunch gets a different wrong answer, not a better chance.
    """
    d = date.fromisoformat(txn.txn_date)
    anchor = settled_on or d
    return anchor - timedelta(days=cfg.LOOKBACK_DAYS + extra_days), d


def candidate_pool(
    txn: BankTxn,
    payments: tuple[Payment, ...],
    claimed: set[str],
    settled_on: date | None = None,
) -> list[Payment]:
    """
    Every unclaimed, captured payment that could possibly belong to this credit.

    This is THE quantity the density invariant governs. Its size drives search cost and,
    far more importantly, the rate at which unrelated subsets land within tolerance by
    coincidence. `run.py` reports the worst realised pool alongside the metrics for
    exactly that reason.

    `settled_on` re-anchors the window without widening it; see `window_for`.
    """
    lo, hi = window_for(txn, settled_on=settled_on)
    return [
        p
        for p in payments
        if p.captured and p.id not in claimed and lo <= payment_date(p) <= hi
    ]


def match(
    txn: BankTxn,
    payments: tuple[Payment, ...],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
    declared_paise: int = 0,
    settled_on: date | None = None,
) -> tuple[list[Candidate], RefusalCategory | None, str]:
    """
    Try to match one bank credit to exactly one payment on amount and date.

    Returns (candidates, refusal_category, reason). Zero candidates means fall through
    to tier 3 (subset-sum); it is not a refusal.

    `declared_paise` is money an investigator evidenced as kept back that no side of
    this batch records. Zero by default -- every reported number is produced by the
    default -- and it reaches `fees.expected_credit_interval` unchanged. See
    `agent/validate.py` for what it has to survive before it gets here.
    """
    pool = candidate_pool(txn, payments, claimed, settled_on)
    if not pool:
        return [], None, ""

    tol = fees.tolerance_for(txn.credit)
    hits: list[Candidate] = []
    for p in pool:
        interval = fees.expected_credit_interval([p], invoices_by_no, declared_paise)
        resid = fees.residual(txn.credit, interval)
        if abs(resid) <= tol:
            hits.append(
                Candidate(
                    payment_ids=(p.id,),
                    residual_paise=resid,
                    tier=TIER,
                    interval_lo=interval.lo,
                    interval_hi=interval.hi,
                    certain=interval.certain,
                )
            )

    if not hits:
        return [], None, ""

    if len(hits) == 1:
        return hits, None, ""

    # Several payments in the window each account for this credit on their own. The
    # amount channel has identified a set, not an answer. Refuse and show all of them,
    # ranked by residual so a human sees the closest first -- but the engine itself
    # does NOT treat "closest" as "correct".
    ranked = sorted(hits, key=lambda c: (abs(c.residual_paise), c.payment_ids))
    return (
        ranked,
        RefusalCategory.MULTIPLE_CANDIDATES,
        f"{len(ranked)} payments in the settlement window each match this credit "
        f"within {tol}p; amount and date alone do not identify one",
    )


def sort_key(txn: BankTxn) -> tuple:
    """
    A total, data-derived ordering for bank transactions.

    The matcher processes credits in a deterministic order so that a single pass is
    reproducible. That is NOT the same as being order-independent: whether the *result*
    depends on this ordering is precisely what the MR1 permutation ensemble measures at
    runtime, and any assignment that changes when the order changes is refused.
    """
    return (txn.txn_date, txn.credit, txn.id)
