"""
The debit half of the bank statement -- money leaving, against settlements already made.

`match_once` iterated `t for t in inputs.bank_txns if t.is_credit` for the life of this
project. Every debit -- a chargeback, a reversal, a bank fee, a payout -- was invisible
to it: not matched, not refused, and not counted anywhere. The gap survived scrutiny for
a simple reason recorded in `ARCHITECTURE.md`: **the generated statement contained no
debits at all**, so the engine had never been shown the half of a statement it ignores
by construction, and nothing could reveal the omission.

**A debit is not a credit with the sign flipped**, which is why this is a separate
module rather than a widened loop. A credit asks *which payments account for this
money arriving?* A chargeback asks a different question -- *which settlement is this
money leaving against?* -- and the answer is a bank transaction the engine has already
posted, not a set of payments it has yet to find. The two searches share no machinery.

**Conservation across time, not within a batch.** `ARCHITECTURE.md` named this as the
reason a reversal would need a different engine: MR4 and MR5 balance the books inside
one batch, and a claw-back is a second event against money the first event already
accounted for. The resolution is that a reversal does NOT undo the assignment it
reverses. Both events happened. The assignment keeps its single verdict and the payment
keeps its single claim -- MR5 is untouched -- and the reversal is recorded as a later
entry against the same credit, so the batch reports a gross reconciled total and a net
one. Un-posting would have required the engine to describe a batch that never occurred.

**What identifies a reversal, and what deliberately does not.**

    amount     the debit equals the credit it reverses, to the paisa
    reference  the reversed credit's reference appears in the debit's own
               reference or narration -- a chargeback carries the ARN/RRN of
               the settlement it claws back
    ordering   the debit is dated on or after the credit
    uniqueness exactly one posted credit satisfies all three

**No vocabulary test.** The obvious rule -- look for CHARGEBACK, REVERSAL, RETURN in the
narration -- was rejected for the same reason `_NOISE` token lists keep being rejected
here: it is a dictionary fitted to the statements in front of us, it would not survive
the next bank's wording, and it invites the reader to believe the engine understands
what it is only pattern-matching. Reference plus amount plus ordering is a structural
argument that holds whatever the line is called, and a bank fee does not carry the
settlement's UTR.

Anything that fails those tests is an `UnexplainedDebit`, reported with its candidates.
"Money left the account and this engine cannot say against what" is a finding an
operator can act on. Silence -- which is what the engine did before -- is not.
"""

from __future__ import annotations

import re
from datetime import date

from ..schemas import BankTxn, Payment
from .results import Assignment, Reversal, SettlementGroup, UnexplainedDebit

# Alphanumeric runs of six or more: UTRs, ARNs, RRNs and settlement ids all take this
# shape, and shorter runs collide with dates, amounts and branch codes.
_TOKEN = re.compile(r"[A-Z0-9]{6,}")


def _tokens(*values: str) -> set[str]:
    return set(_TOKEN.findall(" ".join(v.upper() for v in values if v)))


def _refers_to(debit: BankTxn, credit_ref: str) -> bool:
    if not credit_ref:
        return False
    ref = credit_ref.upper()
    return ref in _tokens(debit.ref_no, debit.narration)


def resolve(
    bank_txns: tuple[BankTxn, ...],
    assignments: tuple[Assignment, ...],
    groups: tuple[SettlementGroup, ...],
    by_id: dict[str, Payment],
) -> tuple[list[Reversal], list[UnexplainedDebit]]:
    """
    Tie each debit to the settlement it reverses, or report it as unexplained.

    Deterministic and order-independent: candidates are gathered by predicate over the
    whole batch and sorted by id before any decision, so nothing depends on statement
    order. Only credits the engine actually POSTED are eligible -- a debit against a
    credit the engine refused is reported unexplained rather than being used to
    retroactively justify a match the engine declined to make.
    """
    posted: dict[str, tuple[str, ...]] = {
        a.bank_txn_id: a.payment_ids for a in assignments
    }
    for g in groups:
        for txn_id in g.bank_txn_ids:
            posted[txn_id] = g.payment_ids

    by_txn = {t.id: t for t in bank_txns}
    debits = sorted(
        (t for t in bank_txns if not t.is_credit and t.debit > 0), key=lambda t: t.id
    )

    reversals: list[Reversal] = []
    unexplained: list[UnexplainedDebit] = []

    for d in debits:
        d_date = date.fromisoformat(d.txn_date)
        by_amount = [
            by_txn[tid] for tid in sorted(posted) if by_txn[tid].credit == d.debit
        ]
        matched = [
            c
            for c in by_amount
            if _refers_to(d, c.ref_no) and date.fromisoformat(c.txn_date) <= d_date
        ]

        if len(matched) == 1:
            c = matched[0]
            reversals.append(
                Reversal(
                    bank_txn_id=d.id,
                    settled_by=c.id,
                    payment_ids=posted[c.id],
                    debit_paise=d.debit,
                    reason=(
                        f"reverses {c.id}: same amount ({d.debit}p), carries its "
                        f"reference {c.ref_no}, and is dated {d.txn_date} against the "
                        f"settlement's {c.txn_date}"
                    ),
                    evidence=("amount_equal", "reference_carried", "dated_after"),
                )
            )
            continue

        if len(matched) > 1:
            # Two posted settlements answer to the same reference and amount -- which is
            # exactly what the duplicate-UTR defect produces. Choosing between them would
            # be picking, and the money is real, so it is an exception rather than a
            # guess.
            unexplained.append(
                UnexplainedDebit(
                    d.id,
                    d.debit,
                    f"{len(matched)} posted settlements match this debit on amount, "
                    "reference and ordering. The evidence does not identify which one "
                    "was reversed, so none is",
                    tuple(sorted(c.id for c in matched)),
                )
            )
            continue

        near = tuple(sorted(c.id for c in by_amount)[:5])
        unexplained.append(
            UnexplainedDebit(
                d.id,
                d.debit,
                (
                    f"{len(by_amount)} posted settlements share this debit's amount but "
                    "none carries a reference tying it to this line"
                    if by_amount
                    else "no posted settlement in this batch matches this debit's "
                    "amount, so the money left against something the engine did not "
                    "reconcile -- a bank fee, a payout, or a claw-back on an earlier "
                    "statement"
                ),
                near,
            )
        )
    return reversals, unexplained
