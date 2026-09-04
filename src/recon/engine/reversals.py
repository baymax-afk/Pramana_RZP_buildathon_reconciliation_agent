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

from ..schemas import BankTxn, Invoice, Payment
from . import fees, tier3_subsetsum
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
    invoices_by_no: dict[str, Invoice] | None = None,
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
    # Payments already clawed back, so two debits against one settlement batch cannot
    # reverse the same receivable twice.
    reversed_payments: set[str] = set()

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
            reversed_payments.update(posted[c.id])
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

        # ---- PARTIAL: the debit claws back only part of a settlement -------
        #
        # A chargeback is raised against a TRANSACTION, and a settlement batch covers
        # several. Disputing one payment inside a batch of four produces a debit for
        # that payment's settled contribution, not for the whole credit -- and the
        # previous version of this, which required `debit == credit` exactly, reported
        # every one of them as an unexplained debit. "Money left the account and we
        # cannot say against what" is an honest answer; it is a poor one when the
        # statement says which settlement, and the arithmetic says which payment.
        #
        # Identified by bounded subset-sum over the payments in the referenced
        # settlement, which is Layer 2's own machinery pointed at a smaller pool:
        # exactly one subset whose expected settled amounts sum to the debit, or none is
        # posted. The pool is small (the payments of one credit) and the reference has
        # already narrowed it to one settlement, so this is cheap and strongly
        # constrained -- the debit is not being matched against the whole batch.
        if not matched and invoices_by_no is not None:
            referenced = [
                by_txn[tid]
                for tid in sorted(posted)
                if _refers_to(d, by_txn[tid].ref_no)
                and date.fromisoformat(by_txn[tid].txn_date) <= d_date
                and by_txn[tid].credit > d.debit
            ]
            if len(referenced) == 1:
                c = referenced[0]
                # Payments of that settlement not already clawed back by an earlier
                # debit. Two chargebacks against one batch must not reverse the same
                # receivable twice, and debits are walked in id order so this is
                # deterministic.
                pool = [
                    by_id[pid]
                    for pid in posted[c.id]
                    if pid in by_id and pid not in reversed_payments
                ]
                if pool:
                    found = tier3_subsetsum.search(
                        d.debit, pool, invoices_by_no,
                        tolerance=fees.tolerance_for(d.debit),
                    )
                    if len(found.solutions) == 1:
                        ids = tuple(sorted(found.solutions[0].payment_ids))
                        reversed_payments.update(ids)
                        reversals.append(
                            Reversal(
                                bank_txn_id=d.id,
                                settled_by=c.id,
                                payment_ids=ids,
                                debit_paise=d.debit,
                                reason=(
                                    f"reverses {len(ids)} of the {len(posted[c.id])} "
                                    f"payment(s) settled by {c.id}: it carries that "
                                    f"settlement's reference {c.ref_no}, is dated "
                                    f"{d.txn_date} against its {c.txn_date}, and "
                                    f"{d.debit}p is what exactly one subset of that "
                                    f"batch settled for"
                                ),
                                evidence=(
                                    "reference_carried",
                                    "dated_after",
                                    "unique_subset_of_the_settlement",
                                ),
                                partial=True,
                            )
                        )
                        continue
                    if len(found.solutions) > 1:
                        unexplained.append(
                            UnexplainedDebit(
                                d.id, d.debit,
                                f"{len(found.solutions)} different subsets of the "
                                f"settlement {c.id} each settled for this amount, so "
                                f"which receivable reopened is not identified",
                                tuple(sorted(
                                    p for sol in found.solutions
                                    for p in sol.payment_ids
                                )),
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
