"""
Every bank line, credit and debit, as one flat filterable list.

**Why a flat list at all.** The payload already describes every line -- but it describes
them in four disjoint places: `assignments`, `settlement_groups`, `exceptions` and
`debits.rows`. Each was built for the section of the page that renders it, and none of
them can answer the question an analyst actually starts with: *show me the money moving
in or out for this customer, or this bank, whatever the engine decided about it.* Four
lists cannot be filtered as one without the client stitching them together, and a client
that stitches is a fifth implementation of "what happened to this line" that will
eventually disagree with the other four.

So this module does the stitching once, on the server, from the same objects the other
four blocks are built from. It adds no facts: every field here is either copied from a
block that already carried it or derived by a named function in this package. What it
adds is a single axis -- `direction` and `status` -- along which the whole statement can
be sliced.

**`status` is a projection of the verdict, not a second opinion.** The engine emits three
verdicts for credits (assign / refuse / no_candidate) and the reversal ledger classifies
debits. Those are the source; the four user-facing states are a naming of them:

    assigned   the engine posted it -- singly, or inside a settlement group
    refused    the engine had candidates and declined to choose between them
    unmatched  nothing accounted for it: `no_candidate`, an unexplained debit, or a
               line that reached no verdict at all
    reversed   a debit the reversal ledger tied to the settlement it claws back

Nothing here recomputes a verdict, and a line that somehow reached none still appears --
as `unmatched`, by subtraction -- because a transaction list that silently omits rows is
worse than one that admits it does not know.

**One number here is deliberately larger than the one on the exception list.** Summing
`rupees_at_risk` over this list exceeds `totals.rupees_at_risk`, and the gap is exactly
the unexplained debits. That is not a discrepancy to reconcile away: `totals` counts
refused CREDITS, which is the right denominator for a match rate, while this list covers
both halves of the statement and an unexplained debit is money that left the account
without an explanation. Both figures are correct about different questions, so both are
reported and neither is quietly adjusted to agree with the other.
"""

from __future__ import annotations

from ..engine.results import MatchOutput
from ..schemas import ReconInputs
from . import banks as _banks

CREDIT = "credit"
DEBIT = "debit"

ASSIGNED = "assigned"
REFUSED = "refused"
UNMATCHED = "unmatched"
REVERSED = "reversed"

STATUSES = (ASSIGNED, REFUSED, UNMATCHED, REVERSED)
DIRECTIONS = (CREDIT, DEBIT)


def build(
    inputs: ReconInputs,
    out: MatchOutput,
    exceptions,
    assignments,
    debit_rows,
    customers_of,
) -> list[dict]:
    """
    One row per bank transaction, in descending amount.

    `exceptions` and `assignments` are the rows `run_output.build` has already
    constructed, and `debit_rows` the dicts it has already assembled -- passed in rather
    than rebuilt so this list cannot drift from the sections it summarises.
    `customers_of` is `run_output._customers_of`, injected for the same reason.
    """
    pay = {p.id: p for p in inputs.payments}
    by_exception = {e.bank_txn_id: e for e in exceptions}
    by_assignment = {a.bank_txn_id: a for a in assignments}
    by_debit = {d["bank_txn_id"]: d for d in debit_rows}

    # A credit inside a settlement group is assigned, but by the group rather than by a
    # row of its own -- Layer 2b claims a GROUP of credits, so no single `Assignment`
    # names it. Flattened here so a grouped credit reads as assigned rather than
    # dropping through to `unmatched`.
    group_of: dict[str, object] = {}
    for g in out.groups:
        for txn_id in g.bank_txn_ids:
            group_of[txn_id] = g

    rows: list[dict] = []
    for t in inputs.bank_txns:
        bank_name, bank_provenance = _banks.bank_of_reference(t.ref_no)
        row = {
            "bank_txn_id": t.id,
            "direction": CREDIT if t.is_credit else DEBIT,
            "txn_date": t.txn_date,
            "value_date": t.value_date,
            "rupees": round(abs(t.amount) / 100, 2),
            "narration": t.narration,
            "reference": t.ref_no,
            "bank_name": bank_name,
            "bank_provenance": bank_provenance,
            "status": UNMATCHED,
            "category": "",
            "detail": "",
            "tier": "",
            "payment_ids": [],
            "invoice_nos": [],
            "customers": [],
            "queue": "",
            "queue_label": "",
            "material": False,
            "rupees_at_risk": 0.0,
            "split": False,
        }

        if t.is_credit:
            _fill_credit(
                row, t, by_assignment, by_exception, group_of, pay, customers_of
            )
        else:
            _fill_debit(row, t, by_debit, pay, customers_of)

        rows.append(row)

    rows.sort(key=lambda r: (-r["rupees"], r["bank_txn_id"]))
    return rows


def _fill_credit(row, t, by_assignment, by_exception, group_of, pay, customers_of):
    if t.id in by_assignment:
        a = by_assignment[t.id]
        row.update(
            status=ASSIGNED,
            tier=a.tier,
            payment_ids=list(a.payment_ids),
            invoice_nos=list(a.invoice_nos),
            customers=list(a.customers),
            detail=f"residual {a.residual_paise:+d}p",
        )
        return
    if t.id in group_of:
        g = group_of[t.id]
        row.update(
            status=ASSIGNED,
            tier="layer2b_group",
            split=True,
            payment_ids=list(g.payment_ids),
            invoice_nos=list(g.invoice_nos),
            customers=customers_of(g.payment_ids, pay),
            detail=(
                f"one payment across {len(g.bank_txn_ids)} bank lines; "
                f"residual {g.residual_paise:+d}p"
            ),
        )
        return
    if t.id in by_exception:
        e = by_exception[t.id]
        # `no_candidate` is not a refusal -- the engine found nothing to weigh rather
        # than declining to weigh between things. It is carried in the exception list
        # because it is still money on somebody's desk, and it is named `unmatched` here
        # for the same reason: telling an analyst the engine "refused" a credit it never
        # had a candidate for would misdescribe the work in front of them.
        row.update(
            status=UNMATCHED if e.category == "no_candidate" else REFUSED,
            category=e.category,
            detail=e.engine_reason,
            rupees_at_risk=e.rupees_at_risk,
            payment_ids=sorted(
                {p for c in e.candidates for p in c.get("payment_ids", [])}
            ),
            customers=sorted(
                {c for cand in e.candidates for c in cand.get("customers", [])}
            ),
            queue=e.routing.get("queue", ""),
            queue_label=e.routing.get("queue_label", ""),
            material=bool(e.routing.get("material", False)),
        )
        return
    # No verdict of any kind. Reported rather than dropped -- the same argument the
    # `not_examined` block makes, applied per row.
    row.update(
        status=UNMATCHED,
        detail="this line reached no verdict of any kind",
        rupees_at_risk=round(t.credit / 100, 2),
    )


def _fill_debit(row, t, by_debit, pay, customers_of):
    d = by_debit.get(t.id)
    if d is None:
        # Debit rows are capped at 50 in the payload's own block. The cap is a display
        # decision about that block, not a claim that the 51st debit does not exist, so
        # this list still carries the line -- with the fields the cap withheld left
        # empty rather than guessed.
        row.update(status=UNMATCHED, detail="not summarised in the debit block")
        return
    status = REVERSED if d["status"] in ("reversal", "partial reversal") else UNMATCHED
    row.update(
        status=status,
        category=d.get("category", ""),
        detail=d.get("detail", ""),
        payment_ids=list(d.get("payment_ids", [])),
        customers=customers_of(d.get("payment_ids", []), pay),
        split=d["status"] == "partial reversal",
        rupees_at_risk=0.0 if status == REVERSED else round(t.debit / 100, 2),
    )


def facets(rows: list[dict]) -> dict:
    """
    Distinct values present, with counts, so a client can render filter chips without a
    second request and without inventing options for a batch that has none of them.
    """
    def _count(key):
        out: dict[str, int] = {}
        for r in rows:
            v = r.get(key) or ""
            if v:
                out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "direction": _count("direction"),
        "status": _count("status"),
        "category": _count("category"),
        "bank": _count("bank_name"),
    }
