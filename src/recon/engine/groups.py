"""
Layer 2b -- settlement groups: one payment set settled across SEVERAL bank credits.

Razorpay splits a settlement for on-demand payouts and when a batch crosses a limit, so
one payment's net can arrive as two separate credits. For the life of this project that
relation was **outside the model, not merely hard**: `claimed` is a set so a payment is
taken exactly once, and every tier asks the same question -- *which subset of payments
sums to this credit?* Neither half of a split settlement has an answer to it, so both
halves were refused. Refusing was the correct output, and it cost real coverage.

**The claim unit was the problem, and it was not the unit anyone thought it was.**
`docs/ARCHITECTURE.md` recorded that lifting this would require the claim unit to become
`(payment, fraction)` and Layer 2's uniqueness test to enumerate over PARTITIONS rather
than subsets -- a strictly larger search whose uniqueness question is harder again.
That was written as a reason not to attempt it, and it was wrong in an instructive way.

A part-settlement is a **group** relation and the group balances exactly:

    credit_1 + credit_2  ==  net(payment)      -- to the paisa, within fee tolerance

Fractions are only needed to post HALF a payment, and the same document already argues
that posting a part-settlement against a whole payment is a wrong answer rather than a
partial one. Nobody wants the fraction. Raising the claim unit from one credit to a
group of credits expresses the relation exactly, keeps every amount an integer, and
turns the "harder again" uniqueness question back into the one Layer 2 already answers:
enumerate every grouping that balances, and refuse unless exactly one does.

**This resolver runs on the RESIDUE, after the matcher has reached its fixpoint.**
That ordering is not an optimisation. A credit is only offered to group resolution once
the single-credit model has failed on it, so grouping can never pre-empt a simpler
explanation, and the search space is the handful of credits nothing accounted for
rather than the whole statement.

Three tests every candidate group must pass, and they are the whole of the layer:

  1. **Balance.** The summed credit falls inside the summed settled interval of the
     payment set, within the tolerance for the summed credit.
  2. **Irreducibility.** No proper, non-empty sub-group of the credits balances against
     a proper, non-empty subset of the payments. A "group" that decomposes into two
     smaller balancing halves is two ordinary assignments the matcher should have found
     on its own, and accepting it as a group would let one arbitrary carve-up of a
     larger coincidence be posted as though it were a settlement.
  3. **Uniqueness.** Across all candidate groups, each credit and each payment appears
     in exactly one. A credit that fits two groupings has not been explained by either.

Failing 1 is silence -- the group is simply not a candidate. Failing 2 is silence for
the same reason. Failing 3 is an EXCEPTION, `ambiguous_grouping`, because two balancing
groupings is a finding an operator can act on and a fact the engine must not resolve by
picking.
"""

from __future__ import annotations

from datetime import date
from itertools import combinations

import config as cfg

from ..schemas import BankTxn, Invoice, Payment
from . import fees, tier2_amount_date, tier3_subsetsum
from .results import Refusal, RefusalCategory, SettlementGroup


def _span_days(txns: list[BankTxn]) -> int:
    days = [date.fromisoformat(t.txn_date) for t in txns]
    return (max(days) - min(days)).days


def _pool_for(
    txns: list[BankTxn], payments: tuple[Payment, ...], claimed: set[str]
) -> list[Payment]:
    """
    The union of the members' candidate pools, deduplicated and ordered by id.

    Union rather than intersection: a settlement split across two days has parts whose
    lookback windows differ, and requiring a payment to be reachable from BOTH would
    exclude exactly the drifted case this exists to catch. Ordered by id so the search
    sees a set, not a traversal -- the same discipline `tier3_subsetsum` applies for the
    same reason.
    """
    seen: dict[str, Payment] = {}
    for t in txns:
        for p in tier2_amount_date.candidate_pool(t, payments, claimed):
            seen[p.id] = p
    return [seen[k] for k in sorted(seen)]


def _balances(
    credit_paise: int, payments: list[Payment], invoices_by_no: dict[str, Invoice]
) -> bool:
    interval = fees.expected_credit_interval(payments, invoices_by_no)
    return fees.fits(credit_paise, interval, fees.tolerance_for(credit_paise))


def _is_reducible(
    txns: list[BankTxn], payments: list[Payment], invoices_by_no: dict[str, Invoice]
) -> bool:
    """
    True when some proper non-empty sub-group of the credits balances against some
    proper non-empty subset of the payments.

    This is the test that stops group resolution from inventing structure. Without it, a
    credit that genuinely belongs to payment A and a second, unrelated credit that
    genuinely belongs to payment B would be posted as one group {A, B} -- the sums
    agree, so balance alone cannot tell the two situations apart. The matcher would
    normally have taken those two on its own; if it did not (because each was refused
    for its own reason) then grouping them is a guess wearing arithmetic.

    Enumerated exhaustively rather than sampled, which the bounds make cheap: at most
    `MAX_GROUP_CREDITS` credits and `MAX_SUBSET_K` payments, so the worst case is a few
    hundred interval sums.
    """
    n = len(txns)
    for k in range(1, n):
        for sub in combinations(range(n), k):
            sub_credit = sum(txns[i].credit for i in sub)
            for m in range(1, len(payments)):
                for pay_sub in combinations(payments, m):
                    if _balances(sub_credit, list(pay_sub), invoices_by_no):
                        return True
    return False


def resolve(
    credits: list[BankTxn],
    payments: tuple[Payment, ...],
    claimed: set[str],
    invoices_by_no: dict[str, Invoice],
    by_id: dict[str, Payment],
) -> tuple[list[SettlementGroup], list[Refusal], bool]:
    """
    Find settlement groups among unsettled credits.

    Returns `(groups, refusals, truncated)`. `credits` is the residue -- every credit the
    matcher left refused or without a candidate. Deterministic: combinations are drawn in
    sorted order and every decision depends on the SET of members, never on their arrival
    order, so the permutation gate confirms order-independence here rather than having to
    repair it.

    **A truncated search grants nothing**, and `truncated` says so. Uniqueness is the
    whole of this layer, it cannot be established over a partial enumeration, and
    granting the groups found before the bound was reached would post precisely the
    answers whose rivals had not been looked for yet.

    **Group resolution only ever upgrades a verdict.** It grants groups, and it raises
    `ambiguous_grouping` where several groupings balance -- which is genuinely more
    informative than the "nothing fits" the credit was carrying. It never replaces an
    accurate verdict with a worse one: an earlier draft raised a per-credit refusal when
    the search bound was hit, and on a batch with no payments at all that turned 140
    correct `no_candidate` verdicts into a report about the engine's own search bound.
    """
    residue = sorted(credits, key=tier2_amount_date.sort_key)
    if len(residue) < 2:
        return [], [], False
    if len(residue) > cfg.MAX_GROUP_RESIDUE:
        # Declined to search at all. C(n,3) grows fast enough that enumerating a large
        # residue would cost more than the whole rest of the match, and the answer would
        # be discarded anyway under the truncation rule above.
        return [], [], True

    candidates: list[tuple[SettlementGroup, list[Payment]]] = []
    combos = 0

    for size in range(2, cfg.MAX_GROUP_CREDITS + 1):
        for members in combinations(residue, size):
            combos += 1
            if combos > cfg.MAX_GROUP_COMBOS:
                return [], [], True
            group = list(members)
            if _span_days(group) > cfg.GROUP_SPAN_DAYS:
                continue
            target = sum(t.credit for t in group)
            pool = _pool_for(group, payments, claimed)
            if not pool or len(pool) > cfg.MAX_POOL:
                continue

            result = tier3_subsetsum.search(
                target, pool, invoices_by_no, tolerance=fees.tolerance_for(target)
            )
            # More than one payment subset balances against this grouping: the grouping
            # itself is not identified, so it is not a candidate at all. It is not an
            # exception either -- an exception here would report a group the engine has
            # no reason to believe in.
            if len(result.solutions) != 1:
                continue

            sol = result.solutions[0]
            members_pay = [by_id[pid] for pid in sol.payment_ids if pid in by_id]
            if len(members_pay) != len(sol.payment_ids):
                continue
            if _is_reducible(group, members_pay, invoices_by_no):
                continue

            interval = fees.expected_credit_interval(members_pay, invoices_by_no)
            candidates.append(
                (
                    SettlementGroup(
                        bank_txn_ids=tuple(sorted(t.id for t in group)),
                        payment_ids=tuple(sorted(sol.payment_ids)),
                        invoice_nos=tuple(
                            sorted(
                                {
                                    inv
                                    for p in members_pay
                                    if (inv := p.notes.get("invoice_no"))
                                }
                            )
                        ),
                        credit_paise=target,
                        residual_paise=fees.residual(target, interval),
                        residual_tightness=fees.residual_tightness(target, interval),
                        certain_fee=sol.certain,
                        uniqueness_margin=1.0,
                    ),
                    members_pay,
                )
            )

    return (*_apply_uniqueness(candidates, residue), False)


def _apply_uniqueness(
    candidates: list[tuple[SettlementGroup, list[Payment]]],
    residue: list[BankTxn],
) -> tuple[list[SettlementGroup], list[Refusal]]:
    """
    Grant only groups whose credits and payments are wanted by nothing else.

    A credit in two balancing groupings has been explained by neither, and a payment in
    two has been claimed twice. Both are the same failure Layer 2 already refuses one
    level down, so both are refused the same way rather than resolved by preferring the
    tighter residual: preferring anything here would be picking, and the evidence that
    would justify the pick is exactly what is missing.
    """
    txn_uses: dict[str, int] = {}
    pay_uses: dict[str, int] = {}
    for g, _ in candidates:
        for t in g.bank_txn_ids:
            txn_uses[t] = txn_uses.get(t, 0) + 1
        for pid in g.payment_ids:
            pay_uses[pid] = pay_uses.get(pid, 0) + 1

    granted: list[SettlementGroup] = []
    contested: list[SettlementGroup] = []
    for g, _ in candidates:
        clean = all(txn_uses[t] == 1 for t in g.bank_txn_ids) and all(
            pay_uses[p] == 1 for p in g.payment_ids
        )
        (granted if clean else contested).append(g)

    refusals: list[Refusal] = []
    credit_of = {t.id: t.credit for t in residue}
    seen: set[str] = set()
    for g in contested:
        for txn_id in g.bank_txn_ids:
            if txn_id in seen:
                continue
            seen.add(txn_id)
            rivals = sorted(
                {
                    other.bank_txn_ids
                    for other in contested
                    if txn_id in other.bank_txn_ids
                    or set(other.payment_ids) & set(g.payment_ids)
                }
            )
            refusals.append(
                Refusal(
                    txn_id,
                    RefusalCategory.AMBIGUOUS_GROUPING,
                    f"this credit balances as part of {len(rivals)} different settlement "
                    f"groups -- "
                    + "; ".join("+".join(r) for r in rivals[:3])
                    + ". Each grouping accounts for the money and the evidence does not "
                    "separate them, so none is posted",
                    credit_of.get(txn_id, 0),
                )
            )

    return granted, refusals
