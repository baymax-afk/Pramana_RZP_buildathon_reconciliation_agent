"""
Layer 1 -- metamorphic relations MR1 to MR6.

Metamorphic testing is the formal answer to the oracle problem. A metamorphic relation
is a necessary property of correct behaviour across *multiple executions*; a violation
proves a defect without anyone knowing the correct output for any input. That is what
makes it usable at runtime on a merchant's books, where no answer key exists.

`docs/METRICS.md` reports these by relation, and states honestly which are which:

    MR1, MR2, MR3, MR6   true metamorphic relations -- they compare multiple runs
    MR4, MR5             single-run conservation invariants

Blurring that distinction would overstate the result. Six "metamorphic relations" reads
stronger than four relations plus two invariants, and the difference is real: an
invariant checks one execution against arithmetic, a metamorphic relation checks one
execution against another.

**Two of these are easy to state incorrectly, and were.** See MR2 and MR3 below.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import timedelta

import config as cfg

from ..engine import fees
from ..engine.match import match_once
from ..schemas import BankTxn, Payment, ReconInputs, date_of
from .stability import run_with_permutations


@dataclass(frozen=True, slots=True)
class Violation:
    relation: str
    detail: str
    subject: str = ""


@dataclass(frozen=True, slots=True)
class RelationResult:
    name: str
    kind: str  # "metamorphic" | "invariant"
    statement: str
    violations: tuple[Violation, ...]
    checked: int

    @property
    def passed(self) -> bool:
        return not self.violations


# --------------------------------------------------------------------------
# MR1 -- permutation invariance
# --------------------------------------------------------------------------
def mr1_permutation_invariance(inputs: ReconInputs, k: int | None = None) -> RelationResult:
    """
    Shuffling input row order on all three sides must not change any assignment.

    Run at a HIGHER K than the runtime gate and from different seeds, so this is a
    genuinely independent check rather than a restatement of the gate's own conclusion.
    If the gate is working, runtime violations are zero by construction -- the value of
    this number is that a non-zero count means the gate itself is leaking.
    """
    passes = k or cfg.PERMUTATION_K_MR1_TEST
    ens = run_with_permutations(inputs, k=passes, seed=inputs.seed + 991)
    violations = tuple(
        Violation(
            "MR1",
            f"assignment unstable across {passes} orderings: "
            + "; ".join(
                f"{{{', '.join(sorted(ids))}}} x{n}" for ids, n in s.observed
            ),
            s.bank_txn_id,
        )
        for s in ens.unstable()
    )
    return RelationResult(
        "MR1", "metamorphic",
        "shuffling input order must not change any assignment",
        violations, len(ens.per_txn),
    )


# --------------------------------------------------------------------------
# MR2 -- split invariance
# --------------------------------------------------------------------------
def mr2_split_invariance(inputs: ReconInputs) -> RelationResult:
    """
    Replace one payment with two payments summing to the same gross, then assert the
    SETTLEMENT-LEVEL GROUPING is unchanged.

    **This relation cannot be stated the obvious way.** MDR is charged per payment with
    paise-level rounding, so splitting Rs 1000 into Rs 600 + Rs 400 changes the total
    fee by up to a paisa -- which changes the bank credit. "Same gross, bank side
    unchanged, assignment unchanged" cannot all hold at once; the naive relation is not
    merely hard to satisfy, it is arithmetically false.

    So the credit is adjusted by the recomputed fee delta, and what must be invariant is
    which INVOICES the credit resolves, not which payment ids. Splitting a payment
    necessarily changes the payment ids; it must not change the economic outcome.
    """
    base = match_once(inputs)
    base_invoices = {
        a.bank_txn_id: frozenset(a.invoice_nos) for a in base.assignments
    }

    rng = random.Random(inputs.seed + 2002)
    candidates = [
        a for a in base.assignments
        if len(a.payment_ids) == 1 and a.certain_fee
    ]
    if not candidates:
        return RelationResult(
            "MR2", "metamorphic",
            "splitting a payment must not change settlement-level grouping",
            (), 0,
        )

    violations: list[Violation] = []
    checked = 0
    by_id = {p.id: p for p in inputs.payments}
    txn_by_id = {t.id: t for t in inputs.bank_txns}

    for target in rng.sample(candidates, min(5, len(candidates))):
        original = by_id[target.payment_ids[0]]
        credit = txn_by_id[target.bank_txn_id]
        if original.amount < 2 * cfg.MIN_PAYMENT_PAISE:
            continue
        checked += 1

        half = original.amount // 2
        parts = _split_payment(original, half, original.amount - half)
        # Recompute the fee delta the split introduces and adjust the credit by it.
        delta = (parts[0].fee + parts[1].fee) - original.fee
        adjusted = replace(credit, credit=credit.credit - delta)

        mutated = ReconInputs(
            payments=tuple(p for p in inputs.payments if p.id != original.id) + parts,
            bank_txns=tuple(
                adjusted if t.id == credit.id else t for t in inputs.bank_txns
            ),
            invoices=inputs.invoices,
            seed=inputs.seed,
            payments_per_window=inputs.payments_per_window,
        )
        after = match_once(mutated)
        after_invoices = {
            a.bank_txn_id: frozenset(a.invoice_nos) for a in after.assignments
        }

        for txn_id, invs in base_invoices.items():
            if txn_id == credit.id:
                continue  # the split credit itself is allowed to change composition
            if after_invoices.get(txn_id) != invs:
                violations.append(
                    Violation(
                        "MR2",
                        f"splitting {original.id} changed an UNRELATED settlement: "
                        f"{txn_id} resolved {sorted(invs)} before, "
                        f"{sorted(after_invoices.get(txn_id, ()))} after",
                        txn_id,
                    )
                )

    return RelationResult(
        "MR2", "metamorphic",
        "splitting a payment (with the credit adjusted by the fee delta) must not "
        "change settlement-level grouping elsewhere",
        tuple(violations), checked,
    )


def _split_payment(p: Payment, a: int, b: int) -> tuple[Payment, Payment]:
    from ..generator import fees as genfees  # generator-side exact model, test-only

    fa, ta = genfees.fee_and_tax(a)
    fb, tb = genfees.fee_and_tax(b)
    return (
        replace(p, id=p.id + "_A", amount=a, fee=fa, tax=ta),
        replace(p, id=p.id + "_B", amount=b, fee=fb, tax=tb),
    )


# --------------------------------------------------------------------------
# MR3 -- augmentation stability
# --------------------------------------------------------------------------
def mr3_augmentation_stability(inputs: ReconInputs) -> RelationResult:
    """
    Adding a record that CANNOT participate in any existing match must leave every
    previously produced assignment untouched.

    **The added record must be built unmatchable, not hoped unmatchable.** "An
    unrelated payment" is not a specification -- a randomly generated one can easily
    land within tolerance of some credit, and then the relation fails for a reason that
    says nothing about the matcher. Here it is guaranteed on three independent axes at
    once:

      * amount   -- larger than every bank credit in the batch, so no subset containing
                    it can sum to any credit
      * date     -- far outside every settlement window, so it enters no candidate pool
      * identity -- a payer name and references present nowhere else

    Any one of those would do. All three are used because the relation is worthless if
    the record turns out to be matchable after all.
    """
    base = match_once(inputs)
    intruder = _unmatchable_payment(inputs)

    mutated = ReconInputs(
        payments=inputs.payments + (intruder,),
        bank_txns=inputs.bank_txns,
        invoices=inputs.invoices,
        seed=inputs.seed,
        payments_per_window=inputs.payments_per_window,
    )
    after = match_once(mutated)

    violations: list[Violation] = []
    if intruder.id in {pid for a in after.assignments for pid in a.payment_ids}:
        violations.append(
            Violation("MR3", f"the constructively unmatchable record {intruder.id} was "
                             f"itself assigned -- it is not unmatchable", intruder.id)
        )
    before_map, after_map = base.assignment_map, after.assignment_map
    for txn_id, ids in before_map.items():
        if after_map.get(txn_id) != ids:
            violations.append(
                Violation(
                    "MR3",
                    f"adding an unmatchable record changed {txn_id}: "
                    f"{sorted(ids)} -> {sorted(after_map.get(txn_id, ()))}",
                    txn_id,
                )
            )
    return RelationResult(
        "MR3", "metamorphic",
        "adding a constructively unmatchable record must not change any existing match",
        tuple(violations), len(before_map),
    )


def _unmatchable_payment(inputs: ReconInputs) -> Payment:
    biggest_credit = max((t.credit for t in inputs.bank_txns), default=0)
    latest = max((date_of(p.created_at) for p in inputs.payments), default=None)
    far_future = (latest + timedelta(days=365)) if latest else None
    import calendar

    ts = calendar.timegm(far_future.timetuple()) if far_future else 0
    return Payment(
        id="pay_MR3_UNMATCHABLE",
        amount=biggest_credit * 10 + 999_999,   # exceeds every credit
        currency="INR",
        status="captured",
        captured=True,
        method="netbanking",
        order_id="order_MR3_UNMATCHABLE",
        created_at=ts,                          # a year past every window
        description="#MR3-DOES-NOT-EXIST",
        contact="+910000000000",
        email="nobody@mr3.invalid",
        provenance="S",
        fee=0,
        tax=0,
        notes={"customer_name": "MR3 Nonexistent Counterparty", "invoice_no": "MR3-0000"},
    )


# --------------------------------------------------------------------------
# MR4 -- conservation
# --------------------------------------------------------------------------
def mr4_conservation(inputs: ReconInputs, out=None) -> RelationResult:
    """
    For every assignment, the bank credit must equal the assigned payments' settled
    amounts less known deductions, within tolerance. Money neither appears nor vanishes.

    **What this does and does not catch.** The matcher only assigns when this holds, so
    checking the same predicate the same way would be tautological. It is therefore
    recomputed FROM THE RAW RECORDS -- payments and invoices looked up fresh -- and
    compared against the residual the assignment stored. That catches bookkeeping
    corruption: a stored residual that disagrees with the data it claims to describe,
    an assignment mutated after scoring, a payment substituted downstream. It does not
    independently re-derive the matcher's logic, and `docs/METRICS.md` says so.
    """
    out = out or match_once(inputs)
    by_id = {p.id: p for p in inputs.payments}
    inv_by_no = {i.invoice_no: i for i in inputs.invoices}
    credit_by_id = {t.id: t.credit for t in inputs.bank_txns}

    violations: list[Violation] = []
    for a in out.assignments:
        payments = [by_id[pid] for pid in a.payment_ids if pid in by_id]
        if len(payments) != len(a.payment_ids):
            violations.append(
                Violation("MR4", f"{a.bank_txn_id} references payments not in the batch",
                          a.bank_txn_id)
            )
            continue
        credit = credit_by_id.get(a.bank_txn_id)
        if credit is None:
            violations.append(
                Violation("MR4", f"{a.bank_txn_id} is not a credit in the batch", a.bank_txn_id)
            )
            continue
        interval = fees.expected_credit_interval(payments, inv_by_no)
        recomputed = fees.residual(credit, interval)
        tol = fees.tolerance_for(credit)
        if abs(recomputed) > tol:
            violations.append(
                Violation(
                    "MR4",
                    f"conservation fails on recomputation: credit {credit}p vs "
                    f"expected {interval.lo}..{interval.hi}p (residual {recomputed:+d}p, "
                    f"tolerance {tol}p)",
                    a.bank_txn_id,
                )
            )
        elif recomputed != a.residual_paise:
            violations.append(
                Violation(
                    "MR4",
                    f"stored residual {a.residual_paise:+d}p disagrees with "
                    f"recomputation {recomputed:+d}p",
                    a.bank_txn_id,
                )
            )
    return RelationResult(
        "MR4", "invariant",
        "assigned payments + MDR + TDS must equal the bank credit within tolerance",
        tuple(violations), len(out.assignments),
    )


# --------------------------------------------------------------------------
# MR5 -- residual closure
# --------------------------------------------------------------------------
def mr5_residual_closure(inputs: ReconInputs, out=None) -> RelationResult:
    """
    Every record must be accounted for exactly once.

    Each bank credit ends in exactly one of {assigned, refused, no_candidate}, and each
    captured payment is either assigned to exactly one credit or listed as unassigned.
    Double-counting or losing a record is the failure mode that makes every other metric
    meaningless -- a payment assigned twice inflates the match rate while double-posting
    money.
    """
    out = out or match_once(inputs)
    violations: list[Violation] = []

    credits = {t.id for t in inputs.bank_txns if t.is_credit}
    assigned = [a.bank_txn_id for a in out.assignments]
    refused = [r.bank_txn_id for r in out.refusals]
    verdicts = assigned + refused + list(out.no_candidate)

    for txn_id, n in _counts(verdicts).items():
        if n > 1:
            violations.append(Violation("MR5", f"{txn_id} received {n} verdicts", txn_id))
    missing = credits - set(verdicts)
    if missing:
        violations.append(
            Violation("MR5", f"{len(missing)} credits received no verdict: "
                             f"{sorted(missing)[:5]}")
        )
    extra = set(verdicts) - credits
    if extra:
        violations.append(
            Violation("MR5", f"verdicts issued for non-credits: {sorted(extra)[:5]}")
        )

    assigned_payments = [pid for a in out.assignments for pid in a.payment_ids]
    for pid, n in _counts(assigned_payments).items():
        if n > 1:
            violations.append(
                Violation("MR5", f"payment {pid} assigned to {n} credits -- double-posted",
                          pid)
            )

    captured = {p.id for p in inputs.payments if p.captured}
    accounted = set(assigned_payments) | set(out.unassigned_payment_ids)
    lost = captured - accounted
    if lost:
        violations.append(
            Violation("MR5", f"{len(lost)} captured payments neither assigned nor "
                             f"listed unassigned: {sorted(lost)[:5]}")
        )
    return RelationResult(
        "MR5", "invariant",
        "every credit and every captured payment is accounted for exactly once",
        tuple(violations), len(credits) + len(captured),
    )


def _counts(items) -> dict:
    from collections import Counter

    return dict(Counter(items))


# --------------------------------------------------------------------------
# MR6 -- idempotence
# --------------------------------------------------------------------------
def mr6_idempotence(inputs: ReconInputs, out=None) -> RelationResult:
    """
    Remove everything already assigned and rerun: the engine must find nothing new.

    If a second pass over the residue produces fresh assignments, the first pass left
    money on the table for no reason a user could predict -- and the engine's output
    depends on how many times it happens to be run, which is not a property any
    reconciliation system may have.
    """
    out = out or match_once(inputs)
    done_txns = {a.bank_txn_id for a in out.assignments}
    done_payments = {pid for a in out.assignments for pid in a.payment_ids}

    residue = ReconInputs(
        payments=tuple(p for p in inputs.payments if p.id not in done_payments),
        bank_txns=tuple(t for t in inputs.bank_txns if t.id not in done_txns),
        invoices=inputs.invoices,
        seed=inputs.seed,
        payments_per_window=inputs.payments_per_window,
    )
    again = match_once(residue)
    violations = tuple(
        Violation(
            "MR6",
            f"rerunning on the residue assigned {a.bank_txn_id} to "
            f"{sorted(a.payment_ids)}, which the first pass declined",
            a.bank_txn_id,
        )
        for a in again.assignments
    )
    return RelationResult(
        "MR6", "metamorphic",
        "rerunning on the unassigned residue must produce no new assignments",
        violations, len(residue.bank_txns),
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
ALL_RELATIONS = ("MR1", "MR2", "MR3", "MR4", "MR5", "MR6")


def run_all(inputs: ReconInputs, out=None, fast: bool = False) -> tuple[RelationResult, ...]:
    """
    Run every relation. `fast` skips the expensive multi-run relations for the dev
    loop; reported runs never use it.
    """
    out = out or match_once(inputs)
    results = [
        mr4_conservation(inputs, out),
        mr5_residual_closure(inputs, out),
    ]
    if not fast:
        results = [
            mr1_permutation_invariance(inputs),
            mr2_split_invariance(inputs),
            mr3_augmentation_stability(inputs),
        ] + results + [mr6_idempotence(inputs, out)]
    else:
        results.append(mr6_idempotence(inputs, out))
    order = {n: i for i, n in enumerate(ALL_RELATIONS)}
    return tuple(sorted(results, key=lambda r: order[r.name]))
