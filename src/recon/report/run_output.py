"""
Serialise a run into the JSON the API and the UI consume.

This is the last point inside the engine's package, and it carries one rule: **the
payload contains no ground truth.** Everything here is computed from `ReconInputs` and
the engine's own output, so the exception list a merchant sees is exactly what the
engine could justify without an answer key. Scoring lives in `src/scorer/` and its
numbers are attached separately, by `run.py`, only when a run is scored.

Exceptions are ranked by RUPEES AT RISK, not by confidence or by category. That is a
substantive choice: a reconciliation analyst's scarce resource is attention, and the
right order to spend it in is descending exposure. A tidy grouping by category would
read better and waste more of their day.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import config as cfg

from ..engine.results import MatchOutput
from ..schemas import ReconInputs
from . import banks as _banks
from . import routing as _routing
from . import transactions as _transactions

SCHEMA_VERSION = 2

# Plain-language explanations of each refusal category. These are DETERMINISTIC and
# live here, not in the LLM tier: the reason an exception exists is a fact about the
# engine's decision, and a merchant should get the same explanation every time. The LLM
# may only elaborate on this text, never replace it.
_WHY: dict[str, str] = {
    "order_dependent_assignment": (
        "The match changed depending on the order the records were read in, which means "
        "it was decided by processing order rather than by the data."
    ),
    "multiple_candidates": (
        "More than one set of payments explains this credit equally well. The amounts "
        "cannot single one out."
    ),
    "solution_cap_reached": (
        "So many different payment combinations fit this credit that the amount is not "
        "evidence for any particular one."
    ),
    "pool_exceeded": (
        "Too many payments settled in this window for the engine to test every "
        "combination, so it declined to guess rather than search part of the range."
    ),
    "no_subset_fits": (
        "Every combination of payments in this settlement window was tested and none "
        "of them adds up to this credit. The money is not accounted for by anything "
        "the engine can see."
    ),
    "amount_name_conflict": (
        "The amounts reconcile but the counterparty does not. Either the payer was "
        "renamed, or this is a coincidental amount match to the wrong customer."
    ),
    "unexplained_residual": (
        "The counterparty is right but the money is not. Likely a partial payment or a "
        "deduction the ledger does not record."
    ),
}

_NEXT_STEP: dict[str, str] = {
    "order_dependent_assignment": "Confirm which payments belong to this credit before posting.",
    "multiple_candidates": "Pick the correct set from the candidates listed, or ask the payer for a remittance advice.",
    "solution_cap_reached": "Request a remittance advice; the amount alone cannot resolve this.",
    "pool_exceeded": "Narrow the settlement window, or supply a remittance advice naming the payments this covers.",
    "no_subset_fits": "Look for a missing payment, an unrecorded refund, or a deduction that is not on the invoice.",
    "amount_name_conflict": "Confirm the customer identity; do not post on the amount alone.",
    "unexplained_residual": "Establish what the shortfall is -- partial settlement, TDS, or a fee not modelled.",
}


@dataclass(frozen=True, slots=True)
class ExceptionRow:
    bank_txn_id: str
    category: str
    rupees_at_risk: float
    txn_date: str
    narration: str
    reference: str
    why: str
    next_step: str
    engine_reason: str
    candidates: list[dict] = field(default_factory=list)
    confidence: float | None = None
    # Which desk, which owner, by when. Computed at REPORT time rather than in the
    # engine, because a due time needs a clock and `match_once` deliberately has none --
    # MR1 depends on it being a pure function of its inputs. See report/routing.py.
    routing: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AssignmentRow:
    bank_txn_id: str
    payment_ids: list[str]
    invoice_nos: list[str]
    tier: str
    rupees: float
    residual_paise: int
    residual_tightness: float
    uniqueness_margin: float | None
    fs_weight: float | None
    # Whether the gateway fee came from Razorpay's own field (tight) or from the rate
    # band (wide). It was absent from this payload while the UI rendered a row claiming
    # to report it -- so `row.certain_fee` was `undefined`, read as falsy, and every
    # match was labelled "bounded by the rate band" including the majority where the fee
    # is known to the paisa. The transcript, reading the same underlying flag correctly,
    # said the opposite two lines further down the same card.
    certain_fee: bool
    permutation_stability: float
    confidence: float | None
    # The bank line's own particulars, carried on the assignment row rather than looked
    # up per card. A reconciled row used to render an amount and `bank_txn_0077`, which
    # names nothing a person recognises -- the date, the counterparty and the bank that
    # sent the money are what makes a posted match checkable at a glance. The exception
    # rows have carried exactly these three since they existed; the success side was the
    # half nobody had to justify, so it never got them.
    txn_date: str = ""
    reference: str = ""
    # DERIVED from `reference`, never supplied by the statement -- see report/banks.py.
    # `bank_provenance` travels with it so the page can say which it is.
    bank_name: str = ""
    bank_provenance: str = ""
    customers: list[str] = field(default_factory=list)


def _customers_of(payment_ids, pay) -> list[str]:
    """
    The distinct ledger customers behind a set of payments, sorted, blanks dropped.

    One helper rather than the same set-comprehension in three places: the exception
    rows, the assignment rows and the transaction list all answer "who is this money
    from" and they must answer it identically, or a filter on the transaction list will
    disagree with the card it opens.
    """
    return sorted(
        {pay[p].notes.get("customer_name", "") for p in payment_ids if p in pay} - {""}
    )


def build(
    inputs: ReconInputs,
    out: MatchOutput,
    seed: int,
    elapsed_s: float | None = None,
    relations=None,
    ensemble=None,
    llm=None,
    recorder=None,
) -> dict:
    """
    Assemble the run payload. Contains no ground truth and no scoring.

    The payload records WHICH LLM tier produced it. The metrics block already prints
    that, but the block is transient and this file is the artefact the API and the UI
    serve -- and it is the thing that outlives the terminal it was printed in. A
    recorded run and a live one differ in their tier attribution (the live model is
    non-deterministic about which references it recovers, so a credit can be claimed by
    tier 1 in one run and tier 2 in the next) even though, measured over five runs, they
    do not differ in a single verdict. An artefact that does not say which tier made it
    cannot be reproduced on purpose.
    """
    txn = {t.id: t for t in inputs.bank_txns}
    pay = {p.id: p for p in inputs.payments}
    debits = [t for t in inputs.bank_txns if t.debit]
    _reversal_by_txn = {
        r.bank_txn_id: {
            "settled_by": r.settled_by,
            "payment_ids": list(r.payment_ids),
            "reason": r.reason,
            "partial": r.partial,
        }
        for r in out.reversals
    }
    _unexplained_by_txn = {
        u.bank_txn_id: {
            "reason": u.reason,
            "category": u.category.value,
            "depends_on": u.depends_on,
        }
        for u in out.unexplained_debits
    }
    # Lines that reached no verdict at all, by subtraction. See the `not_examined` block.
    _seen = (
        {a.bank_txn_id for a in out.assignments}
        | set(out.grouped_txn_ids)
        | {r.bank_txn_id for r in out.refusals}
        | set(out.no_candidate)
        | set(_reversal_by_txn)
        | set(_unexplained_by_txn)
    )
    _unverdicted = [t for t in inputs.bank_txns if t.id not in _seen]

    exceptions: list[ExceptionRow] = []
    for r in out.refusals:
        t = txn.get(r.bank_txn_id)
        cands = []
        for c in r.candidates:
            cands.append(
                {
                    "payment_ids": list(c.payment_ids),
                    "rupees": round(
                        sum(
                            (pay[p].amount - (pay[p].fee or 0))
                            for p in c.payment_ids
                            if p in pay
                        )
                        / 100,
                        2,
                    ),
                    "residual_paise": c.residual_paise,
                    "customers": sorted(
                        {
                            pay[p].notes.get("customer_name", "")
                            for p in c.payment_ids
                            if p in pay
                        }
                        - {""}
                    ),
                }
            )
        exceptions.append(
            ExceptionRow(
                bank_txn_id=r.bank_txn_id,
                category=r.category.value,
                rupees_at_risk=round(r.paise_at_risk / 100, 2),
                txn_date=t.txn_date if t else "",
                narration=t.narration if t else "",
                reference=t.ref_no if t else "",
                why=_WHY.get(r.category.value, ""),
                next_step=_NEXT_STEP.get(r.category.value, ""),
                engine_reason=r.reason,
                candidates=cands,
                routing=_routing.route(r.category.value, r.paise_at_risk).as_dict(),
            )
        )

    # Credits nothing could account for are exceptions too -- silently dropping them
    # would understate the work left on a human's desk, which is the one number this
    # page exists to be honest about.
    for txn_id in out.no_candidate:
        t = txn.get(txn_id)
        if not t:
            continue
        exceptions.append(
            ExceptionRow(
                bank_txn_id=txn_id,
                category="no_candidate",
                rupees_at_risk=round(t.credit / 100, 2),
                txn_date=t.txn_date,
                narration=t.narration,
                reference=t.ref_no,
                why="Nothing in the settlement window accounts for this credit at all.",
                next_step="Check for a payment recorded outside the window, or a credit that is not settlement-related.",
                engine_reason="no candidate found by any tier",
                routing=_routing.route("no_candidate", t.credit).as_dict(),
            )
        )

    exceptions.sort(key=lambda e: -e.rupees_at_risk)

    assignments = [
        AssignmentRow(
            bank_txn_id=a.bank_txn_id,
            payment_ids=list(a.payment_ids),
            invoice_nos=list(a.invoice_nos),
            tier=a.tier,
            rupees=round((txn[a.bank_txn_id].credit if a.bank_txn_id in txn else 0) / 100, 2),
            residual_paise=a.residual_paise,
            residual_tightness=round(a.residual_tightness, 4),
            uniqueness_margin=a.uniqueness_margin,
            fs_weight=round(a.fs_weight, 3) if a.fs_weight is not None else None,
            certain_fee=a.certain_fee,
            permutation_stability=a.permutation_stability,
            confidence=round(a.confidence, 4) if a.confidence is not None else None,
            txn_date=txn[a.bank_txn_id].txn_date if a.bank_txn_id in txn else "",
            reference=txn[a.bank_txn_id].ref_no if a.bank_txn_id in txn else "",
            bank_name=_banks.bank_of_reference(
                txn[a.bank_txn_id].ref_no if a.bank_txn_id in txn else ""
            )[0],
            bank_provenance=_banks.bank_of_reference(
                txn[a.bank_txn_id].ref_no if a.bank_txn_id in txn else ""
            )[1],
            customers=_customers_of(a.payment_ids, pay),
        )
        for a in sorted(
            out.assignments,
            key=lambda x: -(txn[x.bank_txn_id].credit if x.bank_txn_id in txn else 0),
        )
    ]

    credits = [t for t in inputs.bank_txns if t.is_credit]

    # Hoisted out of the payload literal so `transactions.build` can be handed the SAME
    # rows the debits block ships rather than a second construction of them. Two
    # renderings of one debit that disagree about whether it was a reversal is precisely
    # the drift the flat list exists to prevent.
    _debit_rows = [
        {
            "bank_txn_id": t.id,
            "txn_date": t.txn_date,
            "narration": t.narration,
            "ref_no": t.ref_no,
            "rupees": round(t.debit / 100, 2),
            "reverses": _reversal_by_txn.get(t.id, {}).get("settled_by"),
            "payment_ids": _reversal_by_txn.get(t.id, {}).get("payment_ids", []),
            "status": (
                ("partial reversal" if _reversal_by_txn[t.id]["partial"] else "reversal")
                if t.id in _reversal_by_txn
                else "unexplained"
            ),
            "detail": (
                _reversal_by_txn.get(t.id, {}).get("reason")
                or _unexplained_by_txn.get(t.id, {}).get("reason", "")
            ),
            "category": _unexplained_by_txn.get(t.id, {}).get("category", ""),
            "depends_on": _unexplained_by_txn.get(t.id, {}).get("depends_on", ""),
        }
        for t in sorted(debits, key=lambda x: -x.debit)[:50]
    ]

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "density": inputs.payments_per_window,
        "llm_tier": getattr(llm, "name", "disabled"),
        "explanations": _explanations(inputs, recorder),
        "totals": {
            "payments": len(inputs.payments),
            "captured_payments": sum(1 for p in inputs.payments if p.captured),
            "bank_credits": len(credits),
            "invoices": len(inputs.invoices),
            "assigned": len(out.assignments),
            "refused": len(out.refusals),
            "no_candidate": len(out.no_candidate),
            "rupees_at_risk": round(sum(e.rupees_at_risk for e in exceptions), 2),
        },
        # The debit half of the statement, and what the engine made of it.
        #
        # This block used to be called `not_examined`, and it existed because
        # `rupees_at_risk` counts refused CREDITS only: a merchant reading "Rs 800 at
        # risk" while Rs 1,66,732 left the account on lines nobody looked at is being
        # misled by omission. That disclosure did its job -- and the right end state for
        # a disclosure of this kind is that the engine goes and reads them, which is
        # what Layer 2c does. The key is kept so an operator who learned to look here
        # still finds the same money; what changed is that each line now says what it
        # was, and `not_examined` counts only what remains genuinely unexplained.
        "debits": {
            "lines": len(debits),
            "rupees": round(sum(t.debit for t in debits) / 100, 2),
            "reversals_identified": len(out.reversals),
            "reversals_partial": sum(1 for r in out.reversals if r.partial),
            "rupees_reversed": round(out.reversed_paise / 100, 2),
            "unexplained": len(out.unexplained_debits),
            "unexplained_by_category": {
                k: sum(
                    1 for u in out.unexplained_debits if u.category.value == k
                )
                for k in sorted({u.category.value for u in out.unexplained_debits})
            },
            "rupees_unexplained": round(
                sum(u.debit_paise for u in out.unexplained_debits) / 100, 2
            ),
            "reason": (
                "Money leaving the account. Each debit is tied to the settlement it "
                "reverses -- same amount, carrying that settlement's reference, dated "
                "after it, and uniquely so -- or classified as one of four kinds of "
                "unresolvable, because 'cannot say' is equally true of a bank fee and "
                "of a claw-back on last month's settlement and those are different "
                "next steps. A reversal does not undo the settlement it reverses: both "
                "events happened, so the reconciled total is reported gross and net."
            ),
            "rows": _debit_rows,
        },
        # Kept, and now almost always zero: bank lines that reached no verdict at all.
        # A disclosure that vanishes when it reads zero is one nobody can check.
        "not_examined": {
            "lines": len(_unverdicted),
            "rupees": round(sum(abs(t.amount) for t in _unverdicted) / 100, 2),
            "reason": (
                "Bank lines that reached no verdict of any kind. Derived by subtraction "
                "from what was actually decided, so a future blind spot appears here "
                "without anyone having to suspect one."
            ),
        },
        # ---- settlement groups, for the operator-facing view ----
        "settlement_groups": [
            {
                "bank_txn_ids": list(g.bank_txn_ids),
                "payment_ids": list(g.payment_ids),
                "invoice_nos": list(g.invoice_nos),
                "rupees": round(g.credit_paise / 100, 2),
                "residual_paise": g.residual_paise,
                "permutation_stability": g.permutation_stability,
            }
            for g in out.groups
        ],
        "tolerances": {
            "tol_abs_paise": cfg.TOL_ABS_PAISE,
            "tol_rel_bps": cfg.TOL_REL_BPS,
            "mdr_rate_band": list(cfg.MDR_RATE_BAND),
            "lookback_days": cfg.LOOKBACK_DAYS,
            "max_pool": cfg.MAX_POOL,
            "max_subset_k": cfg.MAX_SUBSET_K,
            "materiality_rupees": cfg.MATERIALITY_PAISE / 100,
        },
        "tier_counts": dict(out.tier_counts),
        "exceptions": [asdict(e) for e in exceptions],
        # The same exceptions, aggregated by desk. Served alongside rather than derived
        # in the client, so the CLI, the API and the UI cannot disagree about how many
        # rows are on whose plate -- three implementations of one group-by is how a
        # worklist and its summary drift apart.
        "worklist": _worklist(exceptions),
        # The success side, first in the payload because it is first on the page.
        "reconciled": _reconciled(inputs, out, len(debits)),
        "assignments": [asdict(a) for a in assignments],
        # Every bank line, credit and debit, on one axis. Built from the blocks above
        # rather than beside them -- see report/transactions.py for why the client is not
        # asked to stitch four lists together itself.
        "transactions": _transactions.build(
            inputs, out, exceptions, assignments, _debit_rows, _customers_of
        ),
        "verification": _verification_block(relations, ensemble),
        "throughput_records_per_s": (
            round(
                (len(inputs.payments) + len(inputs.bank_txns) + len(inputs.invoices))
                / elapsed_s
            )
            if elapsed_s
            else None
        ),
    }
    return payload


def _explanations(inputs, recorder) -> dict:
    """
    Why each credit got the verdict it got, keyed by bank transaction id.

    Empty when the run was not recorded, which the UI must handle -- an explanation view
    that assumed this was populated would break on exactly the runs where something had
    already gone wrong.

    Note what is NOT here: ground truth, scores, or any judgement of whether the verdict
    was right. This payload is what the engine could justify without an answer key,
    which is the same standard the rest of the file is held to.
    """
    if recorder is None or not recorder.records:
        return {}
    from recon.explain import Explainer

    ex = Explainer(inputs)
    return {
        txn_id: ex.explain(rec).as_dict()
        for txn_id, rec in recorder.records.items()
    }


def _verification_block(relations, ensemble) -> dict:
    """
    What the four layers actually reported, or an explicit statement that they did not run.

    **The status field exists because its absence was a live defect.** `run.py match`
    without `--verify` produced `{"relations": [], "permutation_gate": null}`, and the UI
    read that as "nothing to show" and rendered no Verification section at all. The
    project's central claim disappeared from the artefact the demo serves, silently --
    not a crash, which is worse, because nothing looks wrong until someone asks where
    the verification is.

    Empty containers cannot distinguish "we checked and found nothing" from "we never
    checked". A status can, and every consumer is now obliged to handle it.
    See REVIEW.md P0-1.
    """
    block: dict = {
        "relations": [],
        "permutation_gate": None,
        "status": "verified" if (relations or ensemble is not None) else "not_run",
        "note": (
            ""
            if (relations or ensemble is not None)
            else "This run was produced WITHOUT --verify, so the metamorphic relations "
            "and the permutation refusal gate did not run. The assignments below are "
            "the matcher's single-pass output and have not been checked for "
            "order-dependence. Re-run `python run.py match --verify`."
        ),
    }
    if relations:
        block["relations"] = [
            {
                "name": r.name,
                "kind": r.kind,
                "statement": r.statement,
                "checked": r.checked,
                "violations": len(r.violations),
                "passed": r.passed,
            }
            for r in relations
        ]
    if ensemble is not None:
        block["permutation_gate"] = ensemble.summary()
    return block


def write(payload: dict, path: Path | None = None) -> Path:
    p = path or (cfg.REPORTS / "run_output.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return p


def _reconciled(inputs: ReconInputs, out: MatchOutput, debit_lines: int) -> dict:
    """
    What the run actually settled — the success side, computed from the engine's output.

    **Why this is a block in the payload and not arithmetic in the client.** The CLI, the
    API and the page all want "how much money got reconciled", and three implementations
    of one sum is how a headline and its own detail view come to disagree. Same reasoning
    as `_worklist`.

    **Every figure here is engine-side and needs no ground truth.** Match rate and
    precision are NOT here — they require an answer key and live in `reports/scorecard.json`
    behind its own route, which is the separation that lets a reader open this file and
    find nothing scored in it.

    A note on `settlements_merged`, because the word invites a wrong reading: nothing is
    merged in the sense of records being combined or rewritten. It counts credits that one
    payment could not explain — a single bank line covering several payments, which the
    subset-sum layer had to decompose. That is the hard case this engine exists for, so it
    is worth surfacing; it is not an edit to anybody's ledger.
    """
    assignments = out.assignments
    invoices_reconciled = {no for a in assignments for no in a.invoice_nos} | {
        no for g in out.groups for no in g.invoice_nos
    }
    multi = [a for a in assignments if len(a.payment_ids) > 1]
    # Counted ONCE per payment, groups included. A part-settlement moves one payment
    # however many bank lines it arrived on, and summing per credit would report the
    # engine reconciling more payments than the batch contains.
    payments_reconciled = len(
        {pid for a in assignments for pid in a.payment_ids}
        | {pid for g in out.groups for pid in g.payment_ids}
    )
    captured = sum(1 for p in inputs.payments if p.captured)
    credits = [t for t in inputs.bank_txns if t.is_credit]
    paise = sum(t.credit for t in credits if t.id in out.assignment_map)

    return {
        # Everything the run read, including the debit lines it deliberately does not
        # match -- counting only what it acted on would overstate the throughput.
        "records_processed": (
            len(inputs.payments) + len(credits) + len(inputs.invoices) + debit_lines
        ),
        "records_breakdown": {
            "payments": len(inputs.payments),
            "bank_credits": len(credits),
            "invoices": len(inputs.invoices),
            # Renamed from `bank_debits_not_examined`, and the rename is the change.
            # The engine reads them now: each is either tied to the settlement it
            # reverses or reported as an unexplained debit.
            "bank_debits": debit_lines,
        },
        "credits_reconciled": len(assignments) + len(out.grouped_txn_ids),
        "credits_total": len(credits),
        "payments_reconciled": payments_reconciled,
        "payments_capturable": captured,
        "invoices_reconciled": len(invoices_reconciled),
        "invoices_total": len(inputs.invoices),
        "paise_reconciled": paise,
        "rupees_reconciled": round(paise / 100, 2),
        # One bank line covering several payments. See the docstring on the word.
        "settlements_merged": len(multi),
        "payments_inside_merged_settlements": sum(len(a.payment_ids) for a in multi),
        # Assignments that survived every shuffled pass of the permutation gate. Equal to
        # `credits_reconciled` on a healthy run: anything that moved under reordering was
        # refused before it reached this list.
        "verified_stable": sum(
            1 for a in assignments if (a.permutation_stability or 0) >= 1.0
        ) + sum(
            len(g.bank_txn_ids)
            for g in out.groups
            if (g.permutation_stability or 0) >= 1.0
        ),
        "exceptions": len(out.refusals) + len(out.no_candidate),
        # ---- Layer 2b: one payment set settled across several credits ----
        "settlement_groups": len(out.groups),
        "credits_in_groups": len(out.grouped_txn_ids),
        "payments_in_groups": len({pid for g in out.groups for pid in g.payment_ids}),
        "paise_in_groups": sum(g.credit_paise for g in out.groups),
        # ---- Layer 2c: the debit half ----
        #
        # Reported next to the reconciled totals rather than inside them. A reversal does
        # not undo the settlement it reverses -- both events happened -- so the batch has
        # a gross figure and a net one, and collapsing them into a single "reconciled"
        # number would quietly net Rs 1,66,732 of clawed-back money out of the headline.
        "reversals": len(out.reversals),
        "paise_reversed": out.reversed_paise,
        "rupees_reversed": round(out.reversed_paise / 100, 2),
        "paise_reconciled_net": paise - out.reversed_paise,
        "rupees_reconciled_net": round((paise - out.reversed_paise) / 100, 2),
        "unexplained_debits": len(out.unexplained_debits),
        "paise_unexplained_debits": sum(u.debit_paise for u in out.unexplained_debits),
    }


def _worklist(exceptions: list[ExceptionRow]) -> dict:
    """
    The exception list aggregated by desk: what is on whose plate, and how much money.

    Ordered by SLA rather than by volume or exposure. A worklist sorted by rupees puts
    the biggest number first and tells a team nothing about what will be late; sorted by
    the clock it is a rota. Within a queue the rows stay in the exception list's own
    order, which is descending exposure -- so the board answers "which desk first" and
    each desk answers "which row first", and neither has to re-sort the other's.

    Queues with nothing on them are INCLUDED, with zeroes. A desk that has no work today
    is a fact about today; omitting it makes the board silently change shape from run to
    run, and a reader cannot tell an empty queue from a queue that stopped existing.
    """
    by_queue: dict[str, list[ExceptionRow]] = {q.key: [] for q in _routing.queues()}
    for e in exceptions:
        key = e.routing.get("queue")
        if key is not None:
            by_queue.setdefault(key, []).append(e)

    rows = []
    for q in _routing.queues():
        items = by_queue.get(q.key, [])
        rows.append(
            {
                "queue": q.key,
                "label": q.label,
                "owner": q.owner,
                "sla_hours": q.sla_hours,
                "action": q.action,
                "rationale": q.rationale,
                "count": len(items),
                "material_count": sum(1 for e in items if e.routing.get("material")),
                "rupees_at_risk": round(sum(e.rupees_at_risk for e in items), 2),
                "categories": sorted({e.category for e in items}),
                "bank_txn_ids": [e.bank_txn_id for e in items],
            }
        )
    return {
        "queues": rows,
        "total_exceptions": len(exceptions),
        "total_rupees_at_risk": round(sum(e.rupees_at_risk for e in exceptions), 2),
        "note": (
            "Category, exposure and candidates are MEASURED by the engine. Queue, owner "
            "and SLA are configured defaults -- nothing here fits or validates them "
            "against an org chart. Materiality (PCAOB AS 2315) halves the clock and is "
            "the one input that is not a choice."
        ),
    }
